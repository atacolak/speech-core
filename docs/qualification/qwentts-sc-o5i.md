# qwentts.cpp 0.6B CustomVoice live cutover — sc-o5i

**bead:** `sc-o5i`
**executed:** 2026-08-18T07:48Z → 2026-08-18T08:00Z (UTC)
**host:** local RTX 4070 12 GB, Ubuntu
**status of this document:** live env/unit evidence after operator-requested cutover

Machine twin: [`qwentts-sc-o5i-report.json`](./qwentts-sc-o5i-report.json)
Isolated stack: [`qwen3-tts-qwentts-q8.md`](./qwen3-tts-qwentts-q8.md)
Voice / instruct research: [`qwen3-tts-customvoice-research.md`](./qwen3-tts-customvoice-research.md)

---

## Verdict

| Decision | Value |
| --- | --- |
| **live mouth** | **qwentts.cpp Q8 CustomVoice** behind the existing aa86b67 daemon |
| **unit binary** | still `speech-out-aa86b67` on `:8788` |
| **inference** | leftover `tts-server` adopted as `qwentts-tts-server.service` on `:18091` |
| **voicecat** | **not edited** — `CosyVoiceTTSService` stays a `:8788` client |
| **rollback** | restore `SPEECH_OUT_COSYVOICE_WORKER_SCRIPT` + stop `qwentts-tts-server` |

CHARTER: production synthesis backend change needs operator accept of the exact env/unit diff. operator asked to migrate after locking informal/crisp/clip and confirming ~70 ms usable.

---

## 1. Exact env / unit diff

### 1.1 What did **not** change

- candidate tree `…/speech-out-cosy3-trt-aa86b67-20260812`
- `ExecStart=…/bin/speech-out-aa86b67 daemon`
- `ExecStartPre=…/deploy/preflight.sh` (still hashes aa86b67 artifacts + imports tensorrt 10.13.3.9)
- `SPEECH_OUT_PROGRESSIVE_BACKEND=cosyvoice`
- `SPEECH_OUT_DAEMON_BIND=0.0.0.0:8788`
- voicecat `CosyVoiceTTSService` / `ProgressiveSpeechOutClient`
- speak / pcm / cancel wire (`speech-out.progressive.ws` v2)

handshake still advertises `backend=progressive-cosyvoice`. that is the daemon label, not the inference engine.

### 1.2 `~/.config/speech-core/speech-out.env`

```diff
 SPEECH_OUT_PROGRESSIVE_BACKEND=cosyvoice
 SPEECH_OUT_COSYVOICE_LIFECYCLE=warm
 SPEECH_OUT_COSYVOICE_QUAL_ROOT=~/.cache/speech-out/cosyvoice-qual-sc-e71.4
-SPEECH_OUT_COSYVOICE_WORKER_SCRIPT=~/.local/share/discord-voice-agent/candidates/speech-out-cosy3-trt-aa86b67-20260812/worker/cosyvoice_progressive_worker.py
+SPEECH_OUT_COSYVOICE_WORKER_SCRIPT=~/.cache/speech-out/qwen3-tts-0.6b-20260817/worker/qwentts_progressive_worker.py
+SPEECH_OUT_QWENTTS_URL=http://127.0.0.1:18091/v1/audio/speech
+SPEECH_OUT_QWENTTS_VOICE=ryan
+SPEECH_OUT_QWENTTS_INSTRUCT=Fast and informal, drop the politeness, keep moving, no pauses.
+SPEECH_OUT_COSYVOICE_WORKER_SCRIPT_ROLLBACK=~/.local/share/discord-voice-agent/candidates/speech-out-cosy3-trt-aa86b67-20260812/worker/cosyvoice_progressive_worker.py
 SPEECH_OUT_COSYVOICE_LOAD_TRT=1
```

TRT / PYTHONPATH / LD_LIBRARY_PATH stay because preflight still imports tensorrt. they are **not** the inference path.

### 1.3 systemd

new unit: `~/.config/systemd/user/qwentts-tts-server.service`

```
ExecStart=…/qwentts.cpp/build/tts-server
  --model …/qwen-talker-0.6b-customvoice-Q8_0.gguf
  --codec …/qwen-tokenizer-12hz-Q8_0.gguf
  --host 127.0.0.1 --port 18091 --lang English
  --alias qwen3-tts-0.6b-q8 --codec-chunk-dur 0.08
Environment=GGML_BACKEND=CUDA0
Environment=LD_LIBRARY_PATH=…/cuda-home/lib64
```

`speech-out-daemon.service` After/Wants that unit. Description now names qwentts. binary path unchanged.

### 1.4 Worker argv

daemon still passes CosyVoice pin flags. worker `parse_known_args` swallows them. voice aliases: `default` / `m1` / `lock-informal` / `informal` / empty → `ryan`.

---

## 2. Clock contract

never subtract daemon vs worker vs http.

| id | meaning | domain |
| --- | --- | --- |
| http first-byte | first TCP byte of `POST /v1/audio/speech` `response_format=pcm` | isolated http |
| http first-use | first sample `\|s\| >= 512` in that stream | isolated http |
| D | daemon `request_received` → first ws pcm frame | daemon_mono |
| W | worker `first_pcm` / `worker_first_pcm_latency_ms` | worker child |
| cancel ack | client cancel → `speech_out_cancel_ack` | client wall |

**usable energy (~70 ms) is the number that matters.** D/W first-pcm (~33 ms warm) is first codec frame, often near-silent.

---

## 3. Isolated locked styles (http, leftover tts-server)

file: `~/.cache/speech-out/qwen3-tts-0.6b-20260817/bench/ttfp-locked-styles.json`
n=4 warm per style, leftover pid 2434118 @ 3266 MiB.

| style | first-byte p50 | first-use p50 | rtf p50 |
| --- | ---: | ---: | ---: |
| informal (default mouth) | 14.7 ms | **69.7 ms** | 0.110 |
| crisp | 15.3 ms | **70.7 ms** | 0.110 |
| clip | 15.0 ms | **70.2 ms** | 0.110 |

---

## 4. Live `:8788` now-stamp after worker flip

client: `speech-out-aa86b67 play --play-command true`
text: `hey, good to see you. i was thinking about this earlier.`
unit ActiveEnter: 2026-08-18 17:51:39 AEST, main pid **2667128**, worker **2667142**.

| run | D first-pcm ms | W first-pcm ms | samples | terminal |
| ---: | ---: | ---: | ---: | --- |
| 0 (warm-ish) | 77.55 | 77.49 | 84480 | completed |
| 1 | 37.85 | 37.80 | 76800 | completed |
| 2 | 33.02 | 32.97 | 76800 | completed |
| 3 | 33.19 | 33.14 | 74880 | completed |
| 4 | 32.92 | 32.87 | 86400 | completed |

warm p50 **D ≈ 33.0 ms**. leftover gpu stayed **3266 MiB** qwentts only. no CosyVoice3 reload.

cancel (raw ws, cancel on first pcm): first_ms **81.2**, cancel_ack_after_send **41.2 ms**, terminal `speech_out_cancelled`, 4 pcm frames (3 after cancel send, in-flight codec chunk). leftover pid unchanged.

leftover `tts-server` pid 2434118 adopted into `qwentts-tts-server.service` (new pid **2693825**, ActiveEnter 2026-08-18 18:01:34 AEST). post-adopt `:8788` play: 216.3 / 44.8 / **32.80** / **32.74** ms D first-pcm. warm still ~33 ms. VRAM **2438 MiB** after reload (allocator reset, same Q8).

---

## 5. SpeechCat / voicecat hop (map only)

there is **no** `SpeechCat` type. the call-side mouth is:

```
webrtc_runner / app.DualRun
  CosyVoiceTTSService (SENTENCE aggregation, voice=default)
    ProgressiveSpeechOutClient
      ws://127.0.0.1:8788/ws/speech-out
        speech-out-aa86b67 daemon
          qwentts_progressive_worker.py
            POST :18091/v1/audio/speech  voice=ryan  instruct=informal
```

wire did **not** move. handshake still `speech_out_backend_ready` / `progressive-cosyvoice` / `pcm_f32le` default. worker converts s16 → f32.

what voicecat already does that still works:

- speak `{type,utterance_id,text,voice,lang,steps,speed}`
- `voice=default` aliases to ryan
- cancel mid-stream → `speech_out_cancel_ack` / `speech_out_cancelled`
- 24 kHz progressive pcm frames

what this `sc` bead must **not** do (and did not):

- retune `tts_service.py` / Discord / SmallWebRTC
- drop SENTENCE aggregation (that is an `sdc-*` / charter amend)
- invent a second ws protocol

honest leftover on the **call** side (not done here):

- class/docs still say CosyVoice3 TensorRT
- `Settings.model="cosyvoice3"`
- operator metrics label `processor=CosyVoiceTTSService#0`
- `SPEECH_OUT_VOICE=default` is fine via alias; naming ryan is `sdc-*`

---

## 6. Rollback

```bash
systemctl --user stop speech-out-daemon.service
systemctl --user stop qwentts-tts-server.service
# edit ~/.config/speech-core/speech-out.env:
#   SPEECH_OUT_COSYVOICE_WORKER_SCRIPT=$SPEECH_OUT_COSYVOICE_WORKER_SCRIPT_ROLLBACK
# restore speech-out-daemon.service After=network-online.target only
# (drop qwentts-tts-server Wants)
systemctl --user daemon-reload
systemctl --user start speech-out-daemon.service
```

rollback spends ~6 GB CosyVoice3 TRT. do not start both engines on this 12 GB card.

---

## 7. Leftover / honesty

- `tts-server` was an unsupervised leftover (pid 2434118) at cutover. unit adoption is part of this bead.
- first-pcm on `:8788` is first codec frame, not first usable energy.
- 0.6B CustomVoice is a closed named roster. no clone. no 10th speaker without Base SFT→GGUF (unproven).
- pcm **out** streams. text **in** is one complete `speak`. measured: [`qwentts-stream-leftover.md`](./qwentts-stream-leftover.md).
- `sc-01p` CosyVoice token-bistream is **closed**. wrong engine. first-clause flush is a voicecat hop.
- voicecat adapters untouched this bead.
