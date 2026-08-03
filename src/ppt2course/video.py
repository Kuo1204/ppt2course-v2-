"""STEP 5: Video composition — xfade transitions, subtitle burn-in.

Logo/BGM/intro-outro are deferred (out of scope for this pass). This module
is the single place that computes the post-transition cumulative timeline,
so the burned-in subtitles and the xfade/acrossfade-shortened video always
agree on where each slide actually starts — per-slide TimedChunk lists
(STEP3's raw, un-offset output) go in, offsets are computed here, and
STEP4's generate_cues/cues_to_srt are called with those offsets baked in.
"""

import shutil
import subprocess
from dataclasses import dataclass

from ppt2course.audio_duration import get_audio_duration_ms
from ppt2course.subtitle import MIN_GAP_MS, SubtitleCue, TimedChunk, cues_to_srt, generate_cues

DEFAULT_TRANSITION = "fade"
DEFAULT_TRANSITION_DURATION_MS = 500
DEFAULT_RESOLUTION = (1920, 1080)
DEFAULT_FPS = 30
DEFAULT_FONT_SIZE = 48

# ASS BackColour alpha: 00=opaque .. FF=transparent. 0x33 ≈ 20% transparent
# i.e. ~80% opaque black box, per the confirmed subtitle styling default.
SUBTITLE_BACK_ALPHA = "33"


class VideoComposeError(Exception):
    pass


@dataclass(frozen=True)
class SlideVideoInput:
    image_path: str
    audio_path: str
    chunks: list[TimedChunk]


def _compute_slide_offsets(durations_ms: list[int], transition_ms: int) -> list[int]:
    if not durations_ms:
        return []
    offsets = [0]
    for i in range(1, len(durations_ms)):
        offsets.append(offsets[i - 1] + durations_ms[i - 1] - transition_ms)
    return offsets


def _total_duration_ms(durations_ms: list[int], transition_ms: int) -> int:
    if not durations_ms:
        return 0
    offsets = _compute_slide_offsets(durations_ms, transition_ms)
    return offsets[-1] + durations_ms[-1]


def _delay_cues(cues: list[SubtitleCue], delay_ms: int) -> list[SubtitleCue]:
    return [
        SubtitleCue(index=c.index, start_ms=c.start_ms + delay_ms, end_ms=c.end_ms + delay_ms, text=c.text)
        for c in cues
    ]


def _build_cues(
    slides: list[SlideVideoInput], durations_ms: list[int], transition_ms: int
) -> list[SubtitleCue]:
    offsets = _compute_slide_offsets(durations_ms, transition_ms)
    per_slide_cues = [
        generate_cues(slide.chunks, start_offset_ms=offset)
        for slide, offset in zip(slides, offsets)
    ]

    # Consecutive slides' narration overlaps during the crossfade transition
    # (acrossfade), which can make a slide's cues start before the previous
    # slide's last cue naturally ends. Rather than truncating the earlier
    # cue early (STEP4's normal gap-trim, which assumes non-overlapping
    # input), the later slide's cues are delayed as a block until they clear
    # the previous slide's last cue by MIN_GAP_MS — so subtitles never
    # overlap, and a slide's own last cue is never cut short.
    reconciled: list[list[SubtitleCue]] = []
    prev_last_end_ms: int | None = None
    for cues in per_slide_cues:
        if cues and prev_last_end_ms is not None:
            min_start = prev_last_end_ms + MIN_GAP_MS
            if cues[0].start_ms < min_start:
                cues = _delay_cues(cues, min_start - cues[0].start_ms)
        reconciled.append(cues)
        if cues:
            prev_last_end_ms = cues[-1].end_ms

    flat = [cue for cues in reconciled for cue in cues]
    return [
        SubtitleCue(index=i + 1, start_ms=c.start_ms, end_ms=c.end_ms, text=c.text)
        for i, c in enumerate(flat)
    ]


def _escape_ffmpeg_filter_path(path: str) -> str:
    escaped = path.replace("\\", "\\\\").replace(":", "\\:")
    return escaped.replace("'", "\\'")


def _scale_pad_filter(index: int, width: int, height: int, fps: int) -> str:
    return (
        f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{index}]"
    )


def _audio_label_filter(input_index: int, slide_index: int) -> str:
    return f"[{input_index}:a]anull[a{slide_index}]"


def _video_xfade_chain(
    n: int, transition: str, transition_duration_ms: int, offsets_ms: list[int]
) -> tuple[str, str]:
    if n == 1:
        return "", "v0"

    transition_sec = transition_duration_ms / 1000
    parts = []
    prev_label = "v0"
    for i in range(1, n):
        out_label = "vout" if i == n - 1 else f"vx{i}"
        offset_sec = offsets_ms[i] / 1000
        parts.append(
            f"[{prev_label}][v{i}]xfade=transition={transition}:"
            f"duration={transition_sec}:offset={offset_sec}[{out_label}]"
        )
        prev_label = out_label
    return ";".join(parts), prev_label


def _audio_acrossfade_chain(n: int, transition_duration_ms: int) -> tuple[str, str]:
    if n == 1:
        return "", "a0"

    transition_sec = transition_duration_ms / 1000
    parts = []
    prev_label = "a0"
    for i in range(1, n):
        out_label = "aout" if i == n - 1 else f"ax{i}"
        parts.append(
            f"[{prev_label}][a{i}]acrossfade=d={transition_sec}:c1=tri:c2=tri[{out_label}]"
        )
        prev_label = out_label
    return ";".join(parts), prev_label


def _subtitle_filter(srt_path: str, video_label: str, font_size: int) -> tuple[str, str]:
    escaped_path = _escape_ffmpeg_filter_path(srt_path)
    style = (
        f"FontSize={font_size},PrimaryColour=&H00FFFFFF,"
        f"BackColour=&H{SUBTITLE_BACK_ALPHA}000000,BorderStyle=3,Outline=0,Shadow=0"
    )
    filt = f"[{video_label}]subtitles='{escaped_path}':force_style='{style}'[vsub]"
    return filt, "vsub"


def _build_ffmpeg_command(
    slides: list[SlideVideoInput],
    durations_ms: list[int],
    offsets_ms: list[int],
    srt_path: str,
    out_video_path: str,
    transition: str,
    transition_duration_ms: int,
    resolution: tuple[int, int],
    fps: int,
    font_size: int,
) -> list[str]:
    width, height = resolution
    n = len(slides)

    cmd = ["ffmpeg", "-y"]
    for slide, duration_ms in zip(slides, durations_ms):
        cmd += ["-loop", "1", "-t", str(duration_ms / 1000), "-i", slide.image_path]
    for slide in slides:
        cmd += ["-i", slide.audio_path]

    scale_filters = [_scale_pad_filter(i, width, height, fps) for i in range(n)]
    audio_label_filters = [_audio_label_filter(n + i, i) for i in range(n)]
    xfade_filter, video_label = _video_xfade_chain(n, transition, transition_duration_ms, offsets_ms)
    audio_filter, audio_label = _audio_acrossfade_chain(n, transition_duration_ms)
    sub_filter, final_video_label = _subtitle_filter(srt_path, video_label, font_size)

    filter_parts = scale_filters + audio_label_filters
    if xfade_filter:
        filter_parts.append(xfade_filter)
    filter_parts.append(sub_filter)
    if audio_filter:
        filter_parts.append(audio_filter)

    filter_complex = ";".join(filter_parts)

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        f"[{final_video_label}]",
        "-map",
        f"[{audio_label}]",
        "-r",
        str(fps),
        out_video_path,
    ]
    return cmd


def compose_video(
    slides: list[SlideVideoInput],
    out_video_path: str,
    out_srt_path: str,
    transition: str = DEFAULT_TRANSITION,
    transition_duration_ms: int = DEFAULT_TRANSITION_DURATION_MS,
    resolution: tuple[int, int] = DEFAULT_RESOLUTION,
    fps: int = DEFAULT_FPS,
    font_size: int = DEFAULT_FONT_SIZE,
) -> None:
    if not slides:
        raise VideoComposeError("slides must not be empty")

    if shutil.which("ffmpeg") is None:
        raise VideoComposeError("ffmpeg executable not found")

    durations_ms = [get_audio_duration_ms(s.audio_path) for s in slides]
    offsets_ms = _compute_slide_offsets(durations_ms, transition_duration_ms)

    cues = _build_cues(slides, durations_ms, transition_duration_ms)
    with open(out_srt_path, "w", encoding="utf-8", newline="") as f:
        f.write(cues_to_srt(cues))

    cmd = _build_ffmpeg_command(
        slides,
        durations_ms,
        offsets_ms,
        out_srt_path,
        out_video_path,
        transition,
        transition_duration_ms,
        resolution,
        fps,
        font_size,
    )

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise VideoComposeError(f"ffmpeg exited with code {result.returncode}: {result.stderr}")
