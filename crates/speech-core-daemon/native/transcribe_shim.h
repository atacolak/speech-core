#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct sc_transcribe_session sc_transcribe_session;

typedef struct sc_transcribe_config {
    const char * model_path;
    int32_t stream_chunk_ms;
    int32_t att_context_right;
} sc_transcribe_config;

typedef struct sc_transcribe_capabilities {
    int32_t native_sample_rate;
    bool supports_streaming;
    int32_t max_timestamp_kind;
    bool accepts_parakeet_stream;
} sc_transcribe_capabilities;

typedef struct sc_transcribe_update {
    bool result_changed;
    bool is_final;
    int32_t revision;
    int64_t input_received_ms;
    int64_t audio_committed_ms;
    int64_t buffered_ms;
    bool committed_changed;
    bool tentative_changed;
    int32_t committed_tokens;
    int32_t total_tokens;
    int32_t returned_timestamp_kind;
    const char * committed_text;
    const char * tentative_text;
} sc_transcribe_update;

typedef struct sc_transcribe_token {
    int32_t id;
    float probability;
    int64_t t0_ms;
    int64_t t1_ms;
    int32_t seg_index;
    int32_t word_index;
    const char * text;
} sc_transcribe_token;

const char * sc_transcribe_status_string(int32_t status);
int32_t sc_transcribe_open_stream(const sc_transcribe_config * config,
                                  sc_transcribe_session ** out_session,
                                  sc_transcribe_capabilities * out_caps);
int32_t sc_transcribe_feed(sc_transcribe_session * session,
                           const float * pcm,
                           int32_t n_samples,
                           sc_transcribe_update * out_update);
int32_t sc_transcribe_finalize(sc_transcribe_session * session,
                               sc_transcribe_update * out_update);
/* After finalize (FINISHED), reset + begin a fresh stream on the same session
 * without reloading the model. Required for per-turn finalization while the
 * mic session stays live. */
int32_t sc_transcribe_rebegin(sc_transcribe_session * session);
void sc_transcribe_free(sc_transcribe_session * session);
int32_t sc_transcribe_get_token(sc_transcribe_session * session,
                                int32_t token_index,
                                sc_transcribe_token * out_token);

#ifdef __cplusplus
}
#endif
