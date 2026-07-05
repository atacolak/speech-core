# turn detection and `<EOU>` policy

this doc explains exactly when the current runtime prints `<EOU>`.

## key point

there is no language-aware eou model in the default path right now.

`<EOU>` is caused by silero vad plus turn-manager policy.

```text
silero vad says: speech ended
turn manager says: this segment is long enough to count
watcher prints: <EOU>
```

## silero vad frame mechanics

silero runs on 30ms frames:

```text
sample_rate = 16000 hz
frame_samples = 480
frame_ms = 30
```

for each frame, silero returns a speech probability. current threshold:

```text
SPEECH_CORE_VAD_THRESHOLD=0.3
```

if probability is above threshold, the frame is treated as speech. if it is below threshold, it is treated as non-speech.

## speech start trigger

current start config:

```text
SPEECH_CORE_VAD_ONSET_FRAMES=2
SPEECH_CORE_VAD_PRE_SPEECH_FRAMES=5
```

meaning:

1. wait for 2 consecutive speech frames.
2. then emit `vad_speech_start`.
3. report the start sample with up to 5 frames / 150ms of pre-roll.

plain english: the system waits for about 60ms of speech, then marks the turn as having started slightly before that so it does not chop the first syllable.

## speech end trigger

current end config:

```text
SPEECH_CORE_VAD_HANGOVER_FRAMES=12
```

meaning:

1. once in speech, watch for consecutive non-speech frames.
2. after 12 non-speech frames, emit `vad_speech_end`.
3. the actual `end_sample` is the first silent frame.
4. the `decision_sample` is 12 frames later.

math:

```text
12 frames * 30ms = 360ms
```

plain english: after silero sees about 360ms of continuous non-speech, it declares the speech segment ended.

## turn close trigger

current turn config:

```text
SPEECH_CORE_TURN_VAD_CLOSE_ENABLED=true
SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS=700
```

when `vad_speech_end` arrives, the turn manager checks:

```text
observed_speech_ms = end_sample - start_sample
```

if observed speech is under 700ms:

```text
turn_eou_suppressed reason=vad_too_short
```

if observed speech is at least 700ms:

```text
turn_eou source=vad degraded=true
turn_closed source=vad reason=vad_speech_end
```

then the watcher prints:

```text
<EOU>
```

## why `<EOU>` can appear mid-thought

because the current policy is acoustic, not semantic.

this closes:

```text
you speak for 2s
pause for 360ms+
continue speaking
```

silero only sees the 360ms silence and says “speech ended.” it does not know whether the sentence was complete.

this is the tradeoff:

- lower hangover = faster turn taking, more mid-sentence eou.
- higher hangover = fewer mid-sentence eou, more delay after you really stop.

## tuning knobs

### reduce premature eou

raise hangover:

```bash
SPEECH_CORE_VAD_HANGOVER_FRAMES=12 ./scripts/install-sfub-daemon.sh   # ~360ms
SPEECH_CORE_VAD_HANGOVER_FRAMES=15 ./scripts/install-sfub-daemon.sh   # ~450ms
```

or raise the minimum segment duration:

```bash
SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS=1000 ./scripts/install-sfub-daemon.sh
```

raising min segment duration filters short entire segments. it does not help with pauses inside long speech.

### reduce end delay

lower hangover:

```bash
SPEECH_CORE_VAD_HANGOVER_FRAMES=4 ./scripts/install-sfub-daemon.sh   # ~120ms
```

this is snappier but more trigger-happy.

## event names to inspect

```text
vad_speech_start
vad_speech_end
turn_eou_candidate
turn_eou_suppressed
turn_eou
turn_closed
transcript_update
```

quick log inspection:

```bash
python3 - <<'PY'
import json, pathlib, collections
p = pathlib.Path.home() / '.local/state/speech-core/logs/events.jsonl'
latest = None
sessions = collections.OrderedDict()
for line in p.open(errors='replace'):
    try:
        o = json.loads(line)
    except Exception:
        continue
    sid = o.get('stream_session_id')
    if sid and o.get('stream_id') == 'sfnix.live_mic':
        sessions.setdefault(sid, []).append(o)
if sessions:
    sid, evs = next(reversed(sessions.items()))
    print('session', sid)
    for e in evs:
        if e.get('event') in {'vad_speech_start','vad_speech_end','turn_eou_suppressed','turn_closed'}:
            print(e)
PY
```

## current observed behavior

latest live tests after retiring parakeet eou showed the expected pattern:

```text
vad decision lag: 360ms
parakeet eou events: none
turn_closed source: vad
short segments under 700ms: suppressed
```

some user tests still had `<EOU>` between spoken phrases. that is consistent with this policy when natural pauses exceed ~360ms.
