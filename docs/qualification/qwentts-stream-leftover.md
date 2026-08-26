# qwentts stream leftover — text in vs pcm out (2026-08-18)

**not a CosyVoice bead.** live pin is qwentts (sc-o5i). this note is the measured leftover after "are we still one-shot?"

## verdict

| layer | streams? | what "stream" means |
| --- | --- | --- |
| `POST :18091` pcm **out** | **yes** | s16le 24 kHz chunked as codec frames (`--codec-chunk-dur 0.08`) |
| `POST :18091` text **in** | **no** | one non-empty `input` string. no append. no token socket. |
| `:8788` speak | **no** | one `{"type":"speak","text":...}` per utterance. daemon has no `append` / `text_delta`. |
| voicecat `CosyVoiceTTSService` | **no** | `TextAggregationMode.SENTENCE` waits for a sentence/boundary on the **whole agent reply** before the first speak. |

pcm already leaves while qwen is still talking. the product is still one-shot **because the call side waits for the finished reply**, then hands one `text` blob.

## proof (leftover tts-server, 2026-08-18)

API (`:18091`):

- `input=""` / missing `input` / `{append:…}` → **400** `'input' must be a non-empty string`
- `{input:"hi.", stream:true}` → **200 audio/pcm**. `stream` is ignored. pcm is already the default.

clocks, same ryan/informal, n=3 after warmup (`bench/ttfp-oneshot-vs-clause.json`):

| arm | first-use p50 | e2e p50 | audio |
| --- | ---: | ---: | ---: |
| first clause `hey, good to see you.` | **71.0 ms** | 234 ms | ~2.0 s |
| full paragraph (clause + rest) | **71.3 ms** | 782 ms | ~7.2 s |
| two sequential speaks (clause then rest) | first-use **75 ms** then **71 ms** | wall 939 ms | 8.48 s |

**extra text does not delay first usable audio.** ~0.2 ms. the win of "start when the agent has a clause" is **not waiting for the rest of the tokens**, not a faster qwen first-pcm.

two sequential speaks work today. they are two utterances, not one KV-continued generation. join/prosody across the cut is unmeasured.

## what will not work

- CosyVoice AutoModel `inference_bistream` on this pin. bake-off: first pcm **after** last text, ~2463 ms. closed as the wrong engine.
- pretending `sc-01p` (CosyVoice text-in-while-pcm) is the leftover. CosyVoice is not live.
- changing qwentts.cpp to accept a token socket this afternoon. the C ABI is `p.text = req.input.c_str()` then `qt_synthesize`. no generator-in.

## what actually reduces latency

clock starts at first usable text **into qwen**, not omp start.
clause vs full paragraph first-use is the same (~71 ms). extra tokens do
**not** make qwen slower. the wait we pay today is voicecat SENTENCE holding
the **whole reply** before the first `speak`.

if the model still thinks 400 ms after the first period, that 400 ms is
the win — start synth on the first speakable unit, overlap the rest.

that is **not** "drip every llm token." `The` then `The cat` then `The cat sat`
is either many tiny one-shots (choppy, restart) or one utterance that
**appends** while pcm is already leaving. qwen has no append / token socket.
daemon `SpeakRequest` is one `text` string. evolution forbids token drip
(`docs/evolution/05-progressive-audio-contract.md`).

opener-flush (`Hello,`) was a fake of this and is **reverted**. operator
rejected it.

real hop (source, not live pin):

1. `sc` (`sc-qwen-text-in-jxf`): `:8788` accepts `{type:append}` and
   `{type:finish}` / `{type:finish_utterance}` on an open utterance.
   `hold_open:true` on speak (or the first append) keeps the utterance
   open after unit-complete. honest v1 is still **queued next unit**,
   same client `utterance_id`. pcm offsets stay contiguous. cancel
   drops the queued continuation. **not** a qwen generator-in. live
   leftover still qwentts @ :18091. aa86b67 pin not swapped.
2. `sdc`: emit speakable units into `speak` then `append`. send
   `{type:finish}` when the llm turn ends. client-local `finish_utterance`
   is not enough — the daemon will hold until it sees finish or cancel.

proof: 38 progressive tests, 2026-08-18.
join/prosody across the unit cut is still unmeasured. do not ship this
binary over the live pin without operator accept.

## leftover / honesty

- join across two `speak`s (breath, pitch, "and then") unmeasured
- handshake still advertises `backend=progressive-cosyvoice`
- voicecat class/docs still say CosyVoice3
- qwentts has no clone on this CustomVoice pin
