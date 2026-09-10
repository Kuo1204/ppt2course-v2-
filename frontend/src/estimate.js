// Purely additive helper: a best-effort estimate of how long video
// generation will take, and a formatter for showing it to the user. This
// module is never imported anywhere except to *display* a number — nothing
// here changes what a job actually does, only how long we tell the user to
// expect it to take.
//
// It's a rough heuristic, not a measurement of this deployment's real
// hardware: there's no history of past job durations to learn from yet, so
// this is calibrated loosely against typical per-slide TTS+ffmpeg timings
// and padded upward (see the final multiplier below) since undershooting
// reads worse to a waiting user than a slightly conservative number does.

// Fixed per-slide cost for TTS synthesis + that slide's own ffmpeg encode
// pass, before any of the multipliers/add-ons below.
const BASE_SECONDS_PER_SLIDE = 6;

// Higher output resolutions cost noticeably more to encode; below 1080p is
// a bit cheaper. Keyed by the same "WIDTHxHEIGHT" value strings the
// resolution picker already uses (see RESOLUTIONS in App.jsx).
const RESOLUTION_MULTIPLIER = {
  "1280x720": 0.8,
  "1920x1080": 1,
  "2560x1440": 1.6,
  "3840x2160": 2.6,
};

// One flat add-on for the AI script-generation call: a single batched
// Gemini request for the whole deck (see script_gen.py), not per slide, so
// it doesn't scale with slideCount the way the base cost does.
const AI_GENERATION_SECONDS = 25;

// Each of these runs as its own extra ffmpeg pass over the whole video
// afterwards (see video.py's compose_video: logo overlay / bgm mix /
// intro-outro concat are separate post-processing steps when present).
const LOGO_SECONDS = 8;
const BGM_SECONDS = 6;
const INTRO_OUTRO_SECONDS = 10;

// avoid_voice_overlap can't borrow time back across a crossfade the way the
// default blended transition does, so the finished video (and therefore the
// encode) tends to run a little longer.
const AVOID_OVERLAP_MULTIPLIER = 1.05;

// Headroom for everything not modeled individually above -- queue wait,
// network variance on the TTS calls, etc.
const PADDING_MULTIPLIER = 1.2;

export function estimateGenerationSeconds({
  slideCount,
  resolution = "1920x1080",
  usesAiGeneration = false,
  hasLogo = false,
  hasBgm = false,
  hasIntroOrOutro = false,
  avoidVoiceOverlap = false,
} = {}) {
  if (!slideCount || slideCount <= 0) return null;

  const resolutionMultiplier = RESOLUTION_MULTIPLIER[resolution] ?? 1;
  let seconds = slideCount * BASE_SECONDS_PER_SLIDE * resolutionMultiplier;

  if (usesAiGeneration) seconds += AI_GENERATION_SECONDS;
  if (hasLogo) seconds += LOGO_SECONDS;
  if (hasBgm) seconds += BGM_SECONDS;
  if (hasIntroOrOutro) seconds += INTRO_OUTRO_SECONDS;
  if (avoidVoiceOverlap) seconds *= AVOID_OVERLAP_MULTIPLIER;

  return Math.round(seconds * PADDING_MULTIPLIER);
}

// "約 3 分鐘" / "約 1 分鐘" / "不到 1 分鐘" -- deliberately coarse (whole
// minutes) since this is a rough estimate, not a countdown timer's worth of
// precision.
export function formatEstimatedDuration(seconds) {
  if (seconds == null) return null;
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return "不到 1 分鐘";
  if (minutes === 1) return "約 1 分鐘";
  return `約 ${minutes} 分鐘`;
}

// For the live "還需約 X 分鐘" readout while a job is running: how much of
// the original estimate is left, given how long it's actually been running
// so far. Never negative, and never silently claims "快好了" once we're
// past the original estimate -- past that point we genuinely don't know
// more than "longer than expected", so callers should treat null as "no
// remaining-time estimate to show" rather than defaulting to 0.
export function estimateRemainingSeconds(estimatedSeconds, startedAtMs) {
  if (estimatedSeconds == null || startedAtMs == null) return null;
  const elapsedSeconds = (Date.now() - startedAtMs) / 1000;
  const remaining = estimatedSeconds - elapsedSeconds;
  return remaining > 5 ? Math.round(remaining) : null;
}
