# Breeze hybrid leftover park (Task 10, sc-breeze-hybrid-81p.11)

Timestamp (UTC): `2026-09-06T07:24:41Z` pre / `2026-09-06T07:24:57Z` park / `2026-09-06T07:25:20Z` post.

Leftover was already inactive at dispatch. `park()` is is-active then stop-if-active. `restore()` was not called. No `systemctl start`. Unit file not edited.

## Pre-state

```
systemctl --user is-active ata-speech-tts.service || true
inactive

ActiveState=inactive
SubState=dead
FragmentPath=~/.config/systemd/user/ata-speech-tts.service
UnitFileState=disabled
```

`nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv,noheader` — empty.

## Park

```
QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
PYTHONPATH=lab/scripts "$QUAL_ROOT/venv/bin/python" -c 'from breeze_tts_qual.park_leftover import park; park()'
```

Result: `park() returned`, exit 0 (no-op; leftover was not active).

## Post-state

```
systemctl --user is-active ata-speech-tts.service
inactive   # exit 3
ActiveState=inactive
SubState=dead
```

`nvidia-smi` compute apps: empty (csv header only; no qwentts ~2.4 GiB occupant).
Display processes only (Xorg / firefox / kitty). GPU mem ~640 MiB / 12282 MiB. No leftover TTS occupant.
