// Ports the pacing math from src/ppt2course/video.py and pipeline.py so the
// standalone app's "目標長度" (target duration) auto-distribute and the
// crossfade-duration clamping behave the same way the real backend's do.
// Kept as a direct, literal port (same variable names/formulas) rather than
// a reinterpretation, specifically so it stays easy to diff against the
// Python source if that algorithm changes later.

const MIN_SLIDE_HOLD_MS = 500; // mirrors video.py's MIN_SLIDE_HOLD_MS

// video.py's _effective_transition_ms: a crossfade can never eat more than
// half of an interior slide's own duration (a full duration for the first/
// last slide, which only border one transition each), minus a minimum
// on-screen hold time.
export function effectiveTransitionMs(durationsMs, requestedTransitionMs) {
  if (durationsMs.length < 2) return requestedTransitionMs;
  const budgets = [
    durationsMs[0],
    ...durationsMs.slice(1, -1).map((d) => d / 2),
    durationsMs[durationsMs.length - 1],
  ];
  const safeMaxMs = Math.floor(Math.min(...budgets)) - MIN_SLIDE_HOLD_MS;
  return Math.max(0, Math.min(requestedTransitionMs, safeMaxMs));
}

// video.py's _total_duration_ms via _compute_slide_offsets: each crossfade
// overlaps two adjacent slides by transitionMs, so the rendered total is
// shorter than the sum of per-slide durations by (n-1)*transitionMs.
export function totalDurationMs(durationsMs, transitionMs) {
  if (durationsMs.length === 0) return 0;
  let total = durationsMs[0];
  for (let i = 1; i < durationsMs.length; i += 1) {
    total += durationsMs[i] - transitionMs;
  }
  return total;
}

// pipeline.py's _reading_pauses_for_target_duration: decide each slide's
// reading_pause_ms (added as trailing silence + held frame) so the final
// video lands as close as possible to targetDurationMs, using only
// pause-silence -- never a TTS rate change, never cutting into real
// narration. Returns { pausesMs, reachable }.
export function readingPausesForTargetDuration(
  narrationDurationsMs,
  transitionDurationMs,
  targetDurationMs,
  avoidVoiceOverlap = false
) {
  const n = narrationDurationsMs.length;
  if (n === 0) return { pausesMs: [], reachable: true };

  const effectiveMs = effectiveTransitionMs(narrationDurationsMs, transitionDurationMs);
  let narrationOnlyTotalMs = totalDurationMs(narrationDurationsMs, effectiveMs);
  if (avoidVoiceOverlap) {
    // avoid_voice_overlap no longer lets narration audio borrow
    // transition time back from the next slide at each of the n-1
    // transitions, so the floor is that much longer.
    narrationOnlyTotalMs += (n - 1) * effectiveMs;
  }
  const slackMs = targetDurationMs - narrationOnlyTotalMs;

  if (slackMs <= 0) {
    return { pausesMs: new Array(n).fill(0), reachable: slackMs === 0 };
  }

  const perSlideMs = Math.floor(slackMs / n);
  const pauses = new Array(n).fill(perSlideMs);
  pauses[n - 1] += slackMs - perSlideMs * n; // remainder -> exact target
  return { pausesMs: pauses, reachable: true };
}
