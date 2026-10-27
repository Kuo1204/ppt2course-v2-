// Builds a plain SubRip (.srt) file directly from this app's own per-slide
// timing data (start/end ms + wrapped text lines) -- no special library
// needed, matching the task's requested approach. One cue per slide: the
// standalone app burns a single subtitle (the slide's whole script,
// line-wrapped) in for the slide's entire on-screen duration, rather than
// the real backend's finer multi-cue-per-slide timing -- a deliberate
// simplification (see BUILD_NOTES.md).
function toSrtTimestamp(ms) {
  const clamped = Math.max(0, Math.round(ms));
  const hours = Math.floor(clamped / 3600000);
  const minutes = Math.floor((clamped % 3600000) / 60000);
  const seconds = Math.floor((clamped % 60000) / 1000);
  const millis = clamped % 1000;
  const pad = (n, len) => String(n).padStart(len, "0");
  return `${pad(hours, 2)}:${pad(minutes, 2)}:${pad(seconds, 2)},${pad(millis, 3)}`;
}

// cues: [{ startMs, endMs, lines: string[] }]
export function buildSrt(cues) {
  return cues
    .map((cue, i) => {
      const text = cue.lines.join("\n");
      return `${i + 1}\n${toSrtTimestamp(cue.startMs)} --> ${toSrtTimestamp(cue.endMs)}\n${text}\n`;
    })
    .join("\n");
}
