#include "transcribe_shim.h"

#include "transcribe.h"
#include "transcribe/parakeet.h"

#include <cstdlib>
#include <cstring>
#include <string>

struct sc_transcribe_session {
    transcribe_session * session = nullptr;
    std::string committed_text;
    std::string tentative_text;
    std::string token_text;
    int32_t att_context_right = 0;
    bool accepts_parakeet_stream = false;
};

static void fill_update(sc_transcribe_session * wrapper,
                        const transcribe_stream_update & update,
                        sc_transcribe_update * out) {
    if (!out) return;

    struct transcribe_stream_text text;
    transcribe_stream_text_init(&text);
    (void) transcribe_stream_get_text(wrapper->session, &text);
    wrapper->committed_text = text.committed_text ? text.committed_text : "";
    wrapper->tentative_text = text.tentative_text ? text.tentative_text : "";

    out->result_changed = update.result_changed;
    out->is_final = update.is_final;
    out->revision = update.revision;
    out->input_received_ms = update.input_received_ms;
    out->audio_committed_ms = update.audio_committed_ms;
    out->buffered_ms = update.buffered_ms;
    out->committed_changed = update.committed_changed;
    out->tentative_changed = update.tentative_changed;
    out->committed_tokens = transcribe_stream_n_committed_tokens(wrapper->session);
    out->total_tokens = transcribe_n_tokens(wrapper->session);
    out->returned_timestamp_kind = transcribe_returned_timestamp_kind(wrapper->session);
    out->committed_text = wrapper->committed_text.c_str();
    out->tentative_text = wrapper->tentative_text.c_str();
}

static transcribe_status begin_stream(sc_transcribe_session * wrapper) {
    struct transcribe_run_params run_params;
    transcribe_run_params_init(&run_params);
    run_params.timestamps = TRANSCRIBE_TIMESTAMPS_TOKEN;

    struct transcribe_stream_params stream_params;
    transcribe_stream_params_init(&stream_params);

    struct transcribe_parakeet_stream_ext pkst;
    if (wrapper->accepts_parakeet_stream) {
        transcribe_parakeet_stream_ext_init(&pkst);
        pkst.att_context_right = wrapper->att_context_right;
        stream_params.family = &pkst.ext;
    }

    return transcribe_stream_begin(wrapper->session, &run_params, &stream_params);
}

const char * sc_transcribe_status_string(int32_t status) {
    return transcribe_status_string(status);
}

int32_t sc_transcribe_open_stream(const sc_transcribe_config * config,
                                  sc_transcribe_session ** out_session,
                                  sc_transcribe_capabilities * out_caps) {
    if (!config || !config->model_path || !out_session) {
        return TRANSCRIBE_ERR_INVALID_ARG;
    }
    *out_session = nullptr;

    static bool log_disabled = false;
    if (!log_disabled) {
        transcribe_log_set(nullptr, nullptr);
        log_disabled = true;
    }

    transcribe_session * session = nullptr;
    transcribe_status st = transcribe_open(config->model_path, nullptr, nullptr, &session);
    if (st != TRANSCRIBE_OK) {
        return st;
    }

    const transcribe_model * model = transcribe_get_model(session);
    struct transcribe_capabilities caps;
    transcribe_capabilities_init(&caps);
    st = transcribe_model_get_capabilities(model, &caps);
    if (st != TRANSCRIBE_OK) {
        transcribe_session_free(session);
        return st;
    }

    const bool accepts_pkst = transcribe_model_accepts_ext_kind(
        model, TRANSCRIBE_EXT_SLOT_STREAM, TRANSCRIBE_EXT_KIND_PARAKEET_STREAM);

    if (out_caps) {
        out_caps->native_sample_rate = caps.native_sample_rate;
        out_caps->supports_streaming = caps.supports_streaming;
        out_caps->max_timestamp_kind = caps.max_timestamp_kind;
        out_caps->accepts_parakeet_stream = accepts_pkst;
    }

    sc_transcribe_session * wrapper = new sc_transcribe_session();
    wrapper->session = session;
    wrapper->att_context_right = config->att_context_right;
    wrapper->accepts_parakeet_stream = accepts_pkst;

    st = begin_stream(wrapper);
    if (st != TRANSCRIBE_OK) {
        transcribe_session_free(session);
        delete wrapper;
        return st;
    }

    *out_session = wrapper;
    return TRANSCRIBE_OK;
}

int32_t sc_transcribe_feed(sc_transcribe_session * wrapper,
                           const float * pcm,
                           int32_t n_samples,
                           sc_transcribe_update * out_update) {
    if (!wrapper || !wrapper->session) return TRANSCRIBE_ERR_INVALID_ARG;
    struct transcribe_stream_update update;
    transcribe_stream_update_init(&update);
    transcribe_status st = transcribe_stream_feed(wrapper->session, pcm, n_samples, &update);
    fill_update(wrapper, update, out_update);
    return st;
}

int32_t sc_transcribe_finalize(sc_transcribe_session * wrapper,
                               sc_transcribe_update * out_update) {
    if (!wrapper || !wrapper->session) return TRANSCRIBE_ERR_INVALID_ARG;
    struct transcribe_stream_update update;
    transcribe_stream_update_init(&update);
    transcribe_status st = transcribe_stream_finalize(wrapper->session, &update);
    fill_update(wrapper, update, out_update);
    return st;
}

int32_t sc_transcribe_rebegin(sc_transcribe_session * wrapper) {
    if (!wrapper || !wrapper->session) return TRANSCRIBE_ERR_INVALID_ARG;
    // Clear FINISHED/FAILED → IDLE, then begin a new ACTIVE stream.
    // Does not free the model or session; keeps weights warm.
    transcribe_stream_reset(wrapper->session);
    wrapper->committed_text.clear();
    wrapper->tentative_text.clear();
    wrapper->token_text.clear();
    return begin_stream(wrapper);
}

void sc_transcribe_free(sc_transcribe_session * wrapper) {
    if (!wrapper) return;
    transcribe_session_free(wrapper->session);
    delete wrapper;
}

int32_t sc_transcribe_get_token(sc_transcribe_session * wrapper,
                                int32_t token_index,
                                sc_transcribe_token * out_token) {
    if (!wrapper || !wrapper->session || !out_token) return TRANSCRIBE_ERR_INVALID_ARG;
    struct transcribe_token tok;
    transcribe_token_init(&tok);
    transcribe_status st = transcribe_get_token(wrapper->session, token_index, &tok);
    if (st != TRANSCRIBE_OK) return st;

    wrapper->token_text = tok.text ? tok.text : "";
    out_token->id = tok.id;
    out_token->probability = tok.p;
    out_token->t0_ms = tok.t0_ms;
    out_token->t1_ms = tok.t1_ms;
    out_token->seg_index = tok.seg_index;
    out_token->word_index = tok.word_index;
    out_token->text = wrapper->token_text.c_str();
    return TRANSCRIBE_OK;
}
