# Validation record

Validated October 18, 2026, on Windows AMD64 with Python 3.12.14 and FFmpeg/ffprobe
9.0.2 (libx264, AAC and libass enabled). The main source uses Python 3.11+ syntax.
Python 3.11 itself and other operating systems were not executed here.
Exact Python package versions are in `validated-environment.txt`.

## Executed

The final combined offline suite passed **80 tests and 45 subtests** in 15.96
seconds, with portable FFmpeg and ffprobe explicitly configured. No media tests
were skipped. The delayed-audio rendering fixture confirms a source audio start
at 1.0 seconds, cut from 0.4 seconds, remains silent for the first 0.6 seconds and
its first caption also starts at 0.6 seconds.

| Check | Actual verification |
|---|---|
| Timestamp grounding | Existing boundary IDs, exact copied times, allowed-window membership, duration bounds, anchor order, unknown IDs, reversed intervals, NaN/infinity and extra fields |
| Model output failures | Mocked schema-constrained Ollama responses, one correction, repeated failure to review, cloud/remote endpoint refusal, no redirects/proxies |
| Long transcripts | Synthetic overlapping windows preserve absolute times, fit conservative serialized budgets, and suppress duplicate candidate intervals |
| Creator subtitles | Local SRT cue timings remain absolute phrase timings; no invented word alignment |
| Subtitle rebasing | Source 126.0 seconds becomes clip-relative 0.6 for a 125.4-second cut; boundary intersections/clamping, grouping, escaping and ASS resolution |
| Audio extraction | Real source video at time zero with audio starting at 1.0 seconds retains leading silence in the extracted 16 kHz WAV |
| Vertical output | Real 1080×1920 H.264/yuv420p, AAC audio, expected duration and complete decoding; crop plus fit/blur alternatives |
| Music | A real 0.5-second synthetic track loops through a much longer clip; spectral checks find both source and music tones later in the result; mixing/ducking paths render successfully |
| Floating-point boundaries | Exact canonical times stay strict, while ordinary duration subtraction roundoff does not reject an otherwise valid boundary |
| Repeat runs | Rendering and full preparation reuse intact existing artifacts, avoiding duplicate artifact rows and inference |
| Selection replacement | Older unsubmitted selections become superseded; an empty replacement cannot leave an old approved excerpt active |
| Submitted artifact preservation | Changed metadata/credit inputs and explicit preparation retries preserve the exact artifact/video referenced by a durable publication intention |
| Missing/invalid inputs | Missing media waits then recovers; invalid manifests/candidates/metadata require review; transient job attempts are finite with backoff |
| Schedules/slots | Naive, stale and insufficient-lead timestamps fail; schedule payload is private/UTC; slot retries reuse reservations; DST gaps/folds are skipped |
| Discovery | Mocked uploads-playlist monitoring, separate search cadence, pagination, 50-ID batches, unavailable videos, filters, cache times and request budgets |
| Trend estimates | Synthetic actual observation intervals, corrected/delayed counters, hidden/zero/rounded/small subscriber handling, distinct comparable-age baselines and sustained confirmation |
| Retention/deletion | Public API data purge, immediate forget paths, analytics-evidence gate, owner-data expiry and consent deletion with local duplicate tombstones |
| Resumable recovery | Mocked interrupted uploads resume the same session/offset; lost final responses are recovered by querying that session; expired/ambiguous outcomes do not start another insert |
| Upload gates | Explicit enablement, editorial/metadata/visual approvals, exact owner-channel match, file checksum and required audience/synthetic-media declarations |
| Auth/quota/retries | Mocked quota reset hold, token-generation auth hold, finite cumulative transient retries, persisted Retry-After, retryable token failures preserving recovery, permanent consent rejection/deletion |
| Post-upload status | Mocked processing/publication separation, throttled polling, completed-record refresh and overdue scheduled publication review |
| Invalid artifact shapes | Valid JSON of the wrong shape returns review instead of aborting the upload batch |

The CLI itself generated a synthetic 22-second source and 0.5-second test-music
file, then prepared a **20-second 1080×1920, 30 fps** clip using default rendering
settings. `verify` reported matching checksum, correct dimensions and complete
decode. A second `prepare` returned `cached: true` and the same artifact folder.
One representative frame was visually inspected: outlined captions were readable
and inside the configured margin. The preview is included in `demo/preview.mp4`.

The source was a generated pattern and tones. Its transcript, candidate and
metadata were explicitly supplied synthetic fixtures. This proves assembly and
state behavior, not speech-recognition or LLM quality.

A wheel was successfully built, installed into a separate temporary workspace
directory without dependencies, and imported using the test dependency path.
Both packaged prompt files loaded, and `python -m clipforge --help` worked from
that installed copy. No credentials, model weights, private state or FFmpeg
binaries are included in the deliverable.

The sandbox's default temporary directory denied some tool writes. Successful
checks used a workspace-local `TEMP`/`TMP` and pytest `--basetemp`; this is an
execution-environment accommodation, not a requirement on an ordinary computer.

## Not executed

- Real Whisper inference, Whisper model downloading, or real Ollama inference.
- Human semantic assessment of an AI-selected clip or generated metadata.
- Real discovery API requests, caption listing/downloading, quota allocation or analytics approval.
- OAuth consent, real Google token renewal/revocation, or a YouTube upload.
- Real scheduled publication, processing eligibility, copyright checks or monetization review.
- Installation of cron/Windows scheduled tasks, GPU compatibility or long-term unattended operation.
- Face tracking, active-speaker association, new narration or discontinuous edit mapping.

Network behavior in tests used mocks. The upload mock counts insertion calls and
asserts no additional insert during interrupted/ambiguous session recovery; this
is evidence about the implementation, not an exactly-once guarantee from YouTube.
Run an owner-authorized private acceptance test on the configured channel after
setup, then inspect actual processing/publication state before enabling routine
uploads.

## Reproduce

Install the project and its dev dependencies, make FFmpeg/ffprobe available, then:

```sh
python -m pytest -q
python -m clipforge init
python examples/make_demo.py --config config.json
python -m clipforge prepare inbox/demo.json
python -m clipforge verify prepared/ACTUAL_CLIP_ID
python -m clipforge prepare inbox/demo.json
```

Set `CLIPFORGE_TEST_FFMPEG` / `CLIPFORGE_TEST_FFPROBE` for binaries outside PATH.
Without them the render tests explicitly skip. No model or OAuth credential is
necessary for this synthetic verification. The normal recording path requires
the locally installed models described in the README.

## Platform documentation review

Primary documentation was browsed for current quotas, discovery permissions,
derived analytics, retention, caption access, Shorts, monetization, upload status,
OAuth and scheduling. Findings and direct official citations are in
`docs/sourcing-notes.md` and `docs/upload-notes.md`. Account/project-specific
settings and permission evidence were not verified. Recheck the current official
pages and your own Console before publication.
