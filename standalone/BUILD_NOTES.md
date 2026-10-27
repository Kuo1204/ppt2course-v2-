# Standalone emergency app — build notes

A zero-backend, client-side-only replica of ppt2course's core workflow
(minus AI script generation and PPTX notes extraction), meant to run as a
free GitHub Pages site when the real FastAPI backend / company intranet is
unavailable. Everything — Chinese TTS, video assembly, subtitle burn-in,
logo/watermark overlays, SRT/DOCX export — runs inside the visitor's own
browser tab. No file ever leaves the browser except two one-time public
downloads (the TTS model weights, the CJK subtitle font), both from public
CDNs, never the company intranet.

Source lives in `standalone/`. The exact same files are duplicated into
`docs/` at the repo root, which is what GitHub Pages actually serves (see
"GitHub Pages setup" below) — `docs/` is not a build output, just a copy,
since there's no bundler/build step at all.

## What works

- Upload per-slide images (no PPTX parsing — explicitly out of scope).
- Script input: paste-with-page-markers ("第1頁...") or per-slide textareas,
  using `frontend/src/scriptParser.js` copied verbatim into
  `standalone/lib/scriptParser.js` (it was already pure, dependency-free
  JS, so no rewrite was needed).
- Fully offline-after-first-load Chinese TTS via
  `@huggingface/transformers` + `BricksDisplay/vits-cmn` (text converted to
  pinyin first via `pinyin-pro`, which this VITS model requires).
- Voice rate/volume controls, applied via ffmpeg's `atempo`/`volume`
  filters at assembly time (not at TTS time, since this voice model has no
  native rate/volume knobs).
- Transition effect + duration, rendered with ffmpeg's `xfade`/`acrossfade`
  filters, pairwise-accumulated across all slides (or a straight
  concat-demuxer cut when the duration is 0 or "avoid voice overlap" is on
  — see Simplifications).
- All Step-04 advanced options from the real app: subtitle style (size/
  position/color/outline/bold), pacing (flat reading+closing pause, or
  auto-distributed target-duration mode), avoid-voice-overlap, a custom
  dictionary that keeps listed terms from being split across a subtitle
  line break, image logo watermark (opacity/corner/size), text watermark
  (enable/text/x%/y%/opacity/font size) behind the same required
  要/不要 gate as the current `frontend/src/App.jsx`, background music
  (+ volume), intro video, outro video.
- Output: a real `.mp4` (h264 + aac, via ffmpeg.wasm), a `.srt` built
  directly from this app's own per-slide timing data, and a `.docx`
  transcript (via the `docx` npm package loaded from jsdelivr's ESM
  wrapper). All three download via plain `<a download>` links.
- Real-time progress log + progress bar while the (synchronous, in-tab)
  pipeline runs.

## Simplifications vs. the real backend (and why)

- **Single fixed TTS voice.** `BricksDisplay/vits-cmn` is the only
  client-side-runnable Chinese TTS model identified in the spike that needs
  no server and no paid API key. The real app's 5-voice Edge-TTS picker
  needs a live network call to Microsoft's service per request; that was
  explicitly ruled out of scope for this zero-backend fallback up front.
- **No real Chinese word segmentation for subtitle wrapping.**
  `standalone/lib/wrap.js` treats every term listed in the custom
  dictionary as one unsplittable token and every other character as its
  own token, then greedily packs tokens onto lines up to a computed
  max-width. This is NOT equivalent to the backend's `wordseg.py` (which
  can lean on a real segmenter); it only guarantees listed terms stay
  intact, not that line breaks fall at linguistically sensible word
  boundaries in general.
- **One subtitle cue per slide, not multiple timed cues within a slide.**
  The `.srt` and the burned-in captions show a slide's whole script for
  that slide's entire on-screen duration, rather than the backend's
  finer-grained cue timing. Chosen because the backend's intra-slide cue
  timing depends on nuances of pipeline.py/timeline.py and TTS word
  timestamps that this VITS model doesn't expose.
- **"Avoid voice overlap" forces a plain cut, not fade-video-without-
  overlapping-audio.** The real backend's `avoid_voice_overlap` is subtle:
  the *video* still crossfades, but the *audio* streams are concatenated
  with no overlap (so narration never overlaps) and the video is briefly
  held to compensate. Replicating that exact asymmetric timing was not
  worth the added complexity for an emergency fallback whose one hard
  requirement ("narration never overlaps") is still fully honored — the
  standalone app instead does a straight cut (no video crossfade, no audio
  overlap) whenever this option is on.
- **"Bold" subtitle style is approximated with a thicker outline.** The
  vendored CJK font (Noto Sans TC, a single variable-weight TTF) isn't
  selectable to its bold instance via ffmpeg's `drawtext`/libfreetype the
  way a packaged Bold .ttf would be, so bold is approximated by increasing
  `borderw` rather than switching glyphs.
- **Target-duration auto-distribute uses a rough pre-TTS estimate.** The
  real backend computes each slide's exact narration duration from real
  TTS output *before* deciding reading-pause lengths. The standalone app
  has to decide each slide's `reading_pause_ms` before running the (slow,
  one-at-a-time) client-side TTS, so it estimates narration length from
  character count (`~260ms/char`) instead. The final video can land a few
  seconds off the requested target, especially for decks with very short
  or very long slides — this mirrors the spirit of the backend's own
  "目標長度可能無法完全達成" framing, just with a coarser estimate.
- **No slide reordering/removal UI** — slides are used in upload order.
  The real app's Step 01 may support more; out of scope here given time
  budget.
- **TTS and ffmpeg both run on the main thread**, not in a Web Worker —
  so the tab's UI can stutter during synthesis/encoding on a long deck.
  ffmpeg.wasm already offloads its own heavy lifting to its internal
  worker (`vendor/ffmpeg/worker.js`); the TTS pipeline does not. Acceptable
  for an emergency fallback; a future improvement would move TTS into a
  dedicated Worker.
- **No AI script generation, no PPTX notes extraction** — both explicitly
  out of scope per the task that produced this app.

## Hard-won bugs fixed (see code comments for the gory details)

- **ffmpeg.wasm silent hang #1**: importing `FFmpeg` from jsdelivr and
  passing a CDN `toBlobURL()` as `classWorkerURL` hangs `ffmpeg.load()`
  forever with zero error (worker.js is itself an ES module with relative
  imports that can't resolve from a `blob:` URL). Fixed by vendoring
  `@ffmpeg/ffmpeg`, `@ffmpeg/core`, `@ffmpeg/util`'s `dist/esm/*` files
  into `standalone/vendor/` for same-origin hosting.
- **ffmpeg.wasm silent hang #2**: even same-origin, passing *relative*
  path strings to `ffmpeg.load()` hangs forever with zero error, because
  the library resolves `classWorkerURL` against its own module URL, not
  the page's URL. Fixed with an `abs()` helper
  (`standalone/lib/ffmpeg.js`) that resolves against `document.baseURI`
  before calling `ffmpeg.load()`.
- **vits-cmn crashes on raw Hanzi** with an opaque ONNX Runtime error —
  fixed by converting to pinyin via `pinyin-pro` first
  (`standalone/lib/tts.js`).
- **vits-cmn ALSO crashes on an empty/punctuation-only input** with the
  exact same opaque "Tensor shape.Size() must be >= 0" error (found live,
  via Playwright, when a test run left a slide's script blank) — pinyin-pro
  converts punctuation to nothing, so the model still ends up fed a
  zero-length token sequence. Fixed two ways: `tts.js` now skips the model
  entirely and returns silence for blank/non-pinyin-able input (a safety
  net), and `app.js`'s `validate()` now blocks submission if any slide's
  script is blank in the first place (the real fix the user should hit).
- **`-shortest` does not reliably cut a `-loop 1` still-image input down to
  the narration audio's length** in this ffmpeg.wasm build (single-
  threaded `ffmpeg-core` 0.12.6, ffmpeg 5.1.4 internally) — found by
  diffing the actual rendered segment duration (consistently ~6.03s)
  against the known TTS audio duration (4.70s) across multiple real runs,
  regardless of whether a `drawtext` filter was present. Isolated with a
  minimal reproduction (`-loop 1` image + exact-length wav + `-shortest`,
  no other filters) before concluding it wasn't specific to this app's
  filter chain. Fixed in `buildSlideSegment` (`standalone/lib/pipeline.js`)
  by passing an explicit `-t <seconds>` matching the intended visual
  duration instead of relying on `-shortest` inference; `-shortest` is
  kept as a harmless secondary guard. This was caught and fixed before
  being considered "done" — the first draft's Playwright run looked
  superficially fine (produced a playable video with correct-looking
  frames) but was silently timing every slide ~1.3s too long and
  therefore desyncing the crossfade offsets and `.srt` cue timestamps from
  the real audio; both were re-verified correct after the fix (see task
  report for exact before/after durations).

## GitHub Pages setup (for the user)

1. Push the `standalone-emergency-app` branch (or merge it to `master`,
   your call) so `docs/` exists on whichever branch you point Pages at.
2. In the GitHub repo: **Settings → Pages**.
3. Under "Build and deployment", set **Source** to "Deploy from a branch".
4. Set **Branch** to `master` (or whichever branch has `docs/`), folder
   `/docs`, then **Save**.
5. GitHub will publish at `https://<your-username>.github.io/<repo-name>/`
   within a minute or two. Because the app resolves every asset path
   relative to the real page URL (`document.baseURI`), it works correctly
   under that sub-path — nothing is hardcoded to domain root.
6. Open the published URL in a normal browser with internet access (it
   must NOT be opened via `file://`, and does not need to be on any
   company network — it's a public static site). The first video
   generated in a given browser will trigger two one-time public
   downloads: the TTS model (~tens of MB, from the Hugging Face Hub CDN)
   and the CJK subtitle font (~11 MB, from a GitHub-hosted Google Fonts
   mirror). Both are cached by the browser afterwards.

## Testing performed

Driven with Playwright against a real headless Chromium, served over
`python -m http.server` (not `file://`), with real network access (so the
TTS model, docx library, and CJK font all downloaded for real):

- **Full-feature run**: 2 uploaded slide images, per-slide script text,
  fade crossfade (500ms), image logo watermark, text watermark, background
  music. Verified with `ffprobe` (correct h264/aac streams, 1280x720,
  11.65s for 2 real-TTS slides with a 500ms crossfade) and by extracting
  frames at 2s/8s and visually inspecting them: correct per-slide
  background, burned-in Chinese subtitle (with a custom-dictionary term
  kept intact across the line break), logo watermark in the chosen corner,
  and text watermark all present and correctly positioned.
- **Cut-path run**: paste-with-page-marker script input (verifying
  `scriptParser.js` reuse end-to-end through the UI), transition duration
  0 (straight concat-demuxer path, not crossfade), "不要" (no watermark)
  branch. Verified with `ffprobe` (10.37s, matching the sum of the two
  slides' real narration durations with no overlap, as expected for a
  cut).
- Both runs' `.srt` and `.docx` outputs were independently inspected (not
  just "file exists"): `.srt` content read directly, `.docx` unzipped and
  its `word/document.xml` text runs extracted and read.
- `standalone/lib/pacing.js` and `standalone/lib/wrap.js` were also
  exercised directly in Node (outside the browser) with hand-computed
  expected values, confirming the target-duration distribution lands on
  the exact requested total and that listed dictionary terms are never
  split across a wrapped line.
- Two real bugs were found this way and fixed (see above): the TTS
  blank-input crash, and the `-shortest` segment-duration bug. Both fixes
  were re-verified with the same two Playwright runs after fixing.

**Not independently smoke-tested** (implemented and code-reviewed against
`video.py`/`pipeline.py`'s logic, but not run end-to-end): intro/outro
video concatenation, target-duration auto-pacing end-to-end (its pure-math
core is unit-verified per above, but not through a full TTS+ffmpeg run),
avoid-voice-overlap, and resolutions other than 1280x720 (used in testing
for faster iteration; the default 1920x1080 and 4K options use the same
code path with different numbers and were not separately run). These are
reasonable residual risks for a first version of an emergency fallback,
not known-broken — but they haven't been proven the way the paths above
have.
