# CV2 Unet vs CV3 TRT listen matrix

**historical.** 2026-08-18 live mouth is qwentts, not either of these.
**ui:** https://sfub.taila159c4.ts.net:18080/ (tailnet)  
**same 5 personas × 16 texts** (10 original + 6 edges: pinyin-ish news, mixed EN/zh, 13800138000, URL, `cargo test`, laugh).

| model | honest n | first-audio p50 | p95 |
|---|---:|---:|---:|
| **cv2 unet** isolated triton | 77 | **256 ms** | 480 ms |
| **cv3 trt** isolated hop5/s4 (aa86b67 worker, unit still parked) | 69 | **299 ms** | 441 ms |

cv2 `zh_default` still ~225 ms. cv3 `zh_default` ~278–284 on short cells, with a fat tail on a few zh lines (tongue 552, how-are-you 468, laugh 482). long refs still hurt both; cv3 less than unet on `human_slow` (~350–370 vs ~480).

**missing / do not score:** 9 leftover `human_slow` × edge/`Hello.` cells — cv3 TRT `AssertionError` on “Hello.” after a 16 s prompt. two cv3 shorts: `en_cross__en_ready`, `en_cross__en_code`.

listen both wavs per cell. latency color is cv2 first-chunk; numbers for both are on the cell.
