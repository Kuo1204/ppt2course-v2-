"""STEP 5: Video composition — xfade transitions, subtitle burn-in, optional
Logo overlay / BGM mixing / intro-outro concatenation.

This module is the single place that computes the post-transition cumulative
timeline, so the burned-in subtitles and the xfade/acrossfade-shortened video
always agree on where each slide actually starts — per-slide TimedChunk lists
(STEP3's raw, un-offset output) go in, offsets are computed here, and
STEP4's generate_cues/cues_to_srt are called with those offsets baked in.
"""

import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from ppt2course.audio_duration import get_audio_duration_ms
from ppt2course.subtitle import MIN_GAP_MS, SubtitleCue, TimedChunk, cues_to_srt, generate_cues
from ppt2course.wordseg import WordSegmenter, get_default_segmenter

DEFAULT_TRANSITION = "fade"
DEFAULT_TRANSITION_DURATION_MS = 500
DEFAULT_RESOLUTION = (1920, 1080)
DEFAULT_FPS = 30
# libass burns FontSize as a literal pixel count against the video's real
# PlayResY (see _subtitle_filter below), so this needs to be sized against
# an actual 1080p frame, not a nominal "readable-looking px" guess. Real
# ffmpeg renders measured glyph ink height at ~0.71 * FontSize; 22px only
# produced ~16px of ink (1.5% of an 1080-tall frame) — noticeably too
# small to read on an actual video. 64px targets ~4.2% of frame height,
# in line with typical subtitle sizing.
DEFAULT_FONT_SIZE = 64
DEFAULT_LOGO_WIDTH = 160
# Tightened from an earlier 24px: at a 1920-wide frame that read as a
# visibly bigger gap from the corner than a watermark should have,
# especially since the frontend never actually scaled either value by the
# selected output resolution before sending them (see scaledLogoWidth /
# scaledLogoMargin in App.jsx) — a flat 24px also became proportionally
# almost invisible at 4K and comparatively roomy at 720p.
DEFAULT_LOGO_MARGIN = 12
DEFAULT_LOGO_OPACITY = 1.0
DEFAULT_LOGO_POSITION = "top-right"
DEFAULT_BGM_VOLUME = 0.2

# ffmpeg overlay= x:y expressions per corner, in terms of the overlay
# filter's own W/H (base video) and w/h (logo) variables.
_LOGO_POSITION_OVERLAYS = {
    "top-left": ("{margin}", "{margin}"),
    "top-right": ("W-w-{margin}", "{margin}"),
    "bottom-left": ("{margin}", "H-h-{margin}"),
    "bottom-right": ("W-w-{margin}", "H-h-{margin}"),
}
LOGO_POSITIONS = tuple(_LOGO_POSITION_OVERLAYS)

# Ported from the user's earlier prototype: white text with a black outline
# (no background box). MarginV (distance from the bottom edge, in pixels —
# literal pixels now that PlayResX/PlayResY are pinned to the real output
# resolution, see _subtitle_filter) is user-adjustable per job; this is only
# the fallback for anyone who doesn't touch that control.
SUBTITLE_FONT_NAME = "Noto Sans CJK TC"
DEFAULT_SUBTITLE_MARGIN_V = 30


class VideoComposeError(Exception):
    pass


@dataclass(frozen=True)
class BrollOverlay:
    """A full-screen visual swap over one slide's own picture, timed against
    that slide's own audio (0 = the slide's narration start).

    This deliberately carries no reference to the global/offset timeline —
    ``compose_video`` never reads it when computing ``durations_ms``,
    offsets, or subtitle cues, so adding, removing, or mistiming an overlay
    cannot move a single frame of narration, subtitle, or total video
    duration. It can only change which picture is on screen.
    """

    image_path: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise VideoComposeError(
                f"broll overlay must have 0 <= start_ms < end_ms, got "
                f"start_ms={self.start_ms} end_ms={self.end_ms}"
            )


@dataclass(frozen=True)
class SlideVideoInput:
    image_path: str
    audio_path: str
    chunks: list[TimedChunk]
    # Optional full-screen B-roll swaps layered over this slide's own image.
    # Empty by default, so every existing caller/test is unaffected.
    broll_overlays: tuple[BrollOverlay, ...] = ()


# The minimum stretch of a slide's own audio that must remain outside any
# neighboring crossfade, even in the worst case. Without this floor, a
# transition duration close to (or longer than) a short slide's own audio
# lets _compute_slide_offsets' delta (durations_ms[i] - transition_ms) hit
# zero or go negative — the cumulative offset stops advancing (or drifts
# backwards) from that slide onward, which is exactly what produced the
# reported "字幕與聲音對不上... 聲音重疊" bug when the transition slider was
# pushed to its 2-second max next to a short slide.
MIN_SLIDE_HOLD_MS = 100


def _effective_transition_ms(durations_ms: list[int], requested_transition_ms: int) -> int:
    if len(durations_ms) < 2:
        return requested_transition_ms

    # The first and last slide only border one transition each; every
    # interior slide borders two (an incoming crossfade eating its head, an
    # outgoing one eating its tail) and so can only spend half its own
    # duration on each side.
    budgets = [durations_ms[0], *(d / 2 for d in durations_ms[1:-1]), durations_ms[-1]]
    safe_max_ms = int(min(budgets)) - MIN_SLIDE_HOLD_MS
    return max(0, min(requested_transition_ms, safe_max_ms))


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
    slides: list[SlideVideoInput],
    durations_ms: list[int],
    transition_ms: int,
    word_segmenter: WordSegmenter | None = None,
) -> list[SubtitleCue]:
    offsets = _compute_slide_offsets(durations_ms, transition_ms)
    per_slide_cues = [
        generate_cues(
            slide.chunks,
            start_offset_ms=offset,
            protected_spans=tuple(
                word_segmenter.word_spans("".join(c.text for c in slide.chunks))
            )
            if word_segmenter
            else (),
        )
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


def _scale_pad_filter(
    index: int, width: int, height: int, fps: int, out_label: str | None = None
) -> str:
    label = out_label or f"v{index}"
    return (
        f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[{label}]"
    )


def _cover_scale_filter(index: int, out_label: str, width: int, height: int, fps: int) -> str:
    # "cover" (scale up + crop), not "contain" (_scale_pad_filter's
    # letterbox behavior) — a B-roll is meant to fully replace the slide's
    # own picture on screen, so padding bars that left slivers of the
    # original PPT visible around the edges would defeat the point.
    return (
        f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps}[{out_label}]"
    )


def _broll_scale_and_overlay_filters(
    slide_index: int,
    overlays: tuple[BrollOverlay, ...],
    first_input_index: int,
    width: int,
    height: int,
    fps: int,
) -> list[str]:
    """Build the filter lines that swap each B-roll over slide ``slide_index``
    during its own [start_ms, end_ms) window, on top of that slide's already
    scaled/padded base image (labeled ``v{slide_index}base``).

    Always ends at label ``v{slide_index}`` — the exact label
    ``_video_xfade_chain`` already expects — so nothing downstream of this
    (xfade, subtitles, acrossfade) needs to know B-roll exists at all.
    """
    parts: list[str] = []
    prev_label = f"v{slide_index}base"
    input_index = first_input_index
    for i, overlay in enumerate(overlays):
        cover_label = f"v{slide_index}broll{i}"
        parts.append(_cover_scale_filter(input_index, cover_label, width, height, fps))
        out_label = f"v{slide_index}" if i == len(overlays) - 1 else f"v{slide_index}mix{i}"
        start_sec = overlay.start_ms / 1000
        end_sec = overlay.end_ms / 1000
        parts.append(
            f"[{prev_label}][{cover_label}]overlay=enable='between(t,{start_sec},{end_sec})'"
            f"[{out_label}]"
        )
        prev_label = out_label
        input_index += 1
    return parts


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


def _subtitle_filter(
    srt_path: str,
    video_label: str,
    font_size: int,
    resolution: tuple[int, int],
    margin_v: int = DEFAULT_SUBTITLE_MARGIN_V,
) -> tuple[str, str]:
    width, height = resolution
    escaped_path = _escape_ffmpeg_filter_path(srt_path)
    # Plain SRT carries no PlayResX/PlayResY of its own, so ffmpeg's
    # SRT->ASS conversion always stamps a fixed 384x288 default script
    # canvas (confirmed by dumping the intermediate .ass — this does not
    # depend on the target resolution at all). libass then scales that
    # 384x288 canvas up to the real output frame (~5x at 1920x1080),
    # inflating FontSize right along with it: a cue sized against max_chars
    # at the nominal font size renders several times wider than intended
    # and overflows off both edges of the frame. The `original_size` filter
    # option, despite being documented for exactly this, measurably has no
    # effect here (verified with a real ffmpeg render) — the fix that does
    # work is overriding PlayResX/PlayResY directly via force_style, which
    # libass's force-style parser honors as script-level (not just
    # per-style) overrides, making FontSize a literal pixel size matching
    # the actual output resolution.
    style = (
        f"FontName={SUBTITLE_FONT_NAME},FontSize={font_size},"
        f"PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,"
        f"BorderStyle=1,Outline=2,Shadow=0,MarginV={margin_v},"
        f"WrapStyle=2,PlayResX={width},PlayResY={height}"
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
    subtitle_margin_v: int = DEFAULT_SUBTITLE_MARGIN_V,
) -> list[str]:
    width, height = resolution
    n = len(slides)

    cmd = ["ffmpeg", "-y"]
    for slide, duration_ms in zip(slides, durations_ms):
        cmd += ["-loop", "1", "-t", str(duration_ms / 1000), "-i", slide.image_path]
    for slide in slides:
        cmd += ["-i", slide.audio_path]

    # B-roll images are appended last, after every slide image (0..n-1) and
    # every slide audio (n..2n-1) — so those two existing index ranges never
    # shift, regardless of which slides have B-roll or how many.
    scale_filters: list[str] = []
    next_input_index = 2 * n
    for i, slide in enumerate(slides):
        if not slide.broll_overlays:
            scale_filters.append(_scale_pad_filter(i, width, height, fps))
            continue
        scale_filters.append(_scale_pad_filter(i, width, height, fps, out_label=f"v{i}base"))
        for overlay in slide.broll_overlays:
            cmd += ["-loop", "1", "-t", str(durations_ms[i] / 1000), "-i", overlay.image_path]
        scale_filters.extend(
            _broll_scale_and_overlay_filters(
                i, slide.broll_overlays, next_input_index, width, height, fps
            )
        )
        next_input_index += len(slide.broll_overlays)

    audio_label_filters = [_audio_label_filter(n + i, i) for i in range(n)]
    xfade_filter, video_label = _video_xfade_chain(n, transition, transition_duration_ms, offsets_ms)
    audio_filter, audio_label = _audio_acrossfade_chain(n, transition_duration_ms)
    sub_filter, final_video_label = _subtitle_filter(
        srt_path, video_label, font_size, resolution, margin_v=subtitle_margin_v
    )

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
        # Without these, ffmpeg picks whatever the filter graph naturally
        # produces (often yuv444p after xfade/subtitles), which standard
        # players — including Windows' built-in ones — can't decode.
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        out_video_path,
    ]
    return cmd


def _run_ffmpeg(cmd: list[str], step_description: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise VideoComposeError(
            f"ffmpeg failed during {step_description} (exit {result.returncode}): {result.stderr}"
        )


def _add_logo_overlay(
    video_path: str,
    logo_path: str,
    out_path: str,
    logo_width: int,
    margin: int,
    opacity: float = DEFAULT_LOGO_OPACITY,
    position: str = DEFAULT_LOGO_POSITION,
) -> None:
    try:
        x_expr, y_expr = _LOGO_POSITION_OVERLAYS[position]
    except KeyError:
        raise ValueError(
            f"unknown logo_position {position!r}; expected one of "
            f"{sorted(_LOGO_POSITION_OVERLAYS)}"
        ) from None
    overlay_xy = f"{x_expr}:{y_expr}".format(margin=margin)

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", logo_path,
        "-filter_complex",
        f"[1:v]scale={logo_width}:-1,format=rgba,colorchannelmixer=aa={opacity}[logo];"
        f"[0:v][logo]overlay={overlay_xy}",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    _run_ffmpeg(cmd, "logo overlay")


def _mix_background_music(
    video_path: str, bgm_path: str, out_path: str, bgm_volume: float
) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-i", bgm_path,
        "-filter_complex",
        f"[1:a]volume={bgm_volume}[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        out_path,
    ]
    _run_ffmpeg(cmd, "background music mixing")


def _concatenate_with_intro_outro(
    main_video_path: str,
    out_path: str,
    intro_path: str | None,
    outro_path: str | None,
    resolution: tuple[int, int],
    fps: int,
) -> None:
    parts = [p for p in (intro_path, main_video_path, outro_path) if p]

    if len(parts) == 1:
        shutil.copy(main_video_path, out_path)
        return

    width, height = resolution
    cmd = ["ffmpeg", "-y"]
    for p in parts:
        cmd += ["-i", p]

    filter_segments = []
    for idx in range(len(parts)):
        filter_segments.append(
            f"[{idx}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={fps},setsar=1,format=yuv420p[v{idx}];"
            f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{idx}]"
        )
    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(len(parts)))
    filter_complex = (
        ";".join(filter_segments)
        + f";{concat_inputs}concat=n={len(parts)}:v=1:a=1[outv][outa]"
    )

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p",
        out_path,
    ]
    _run_ffmpeg(cmd, "intro/outro concatenation")


def compose_video(
    slides: list[SlideVideoInput],
    out_video_path: str,
    out_srt_path: str,
    transition: str = DEFAULT_TRANSITION,
    transition_duration_ms: int = DEFAULT_TRANSITION_DURATION_MS,
    resolution: tuple[int, int] = DEFAULT_RESOLUTION,
    fps: int = DEFAULT_FPS,
    font_size: int = DEFAULT_FONT_SIZE,
    subtitle_margin_v: int = DEFAULT_SUBTITLE_MARGIN_V,
    logo_path: str | None = None,
    logo_width: int = DEFAULT_LOGO_WIDTH,
    logo_margin: int = DEFAULT_LOGO_MARGIN,
    logo_opacity: float = DEFAULT_LOGO_OPACITY,
    logo_position: str = DEFAULT_LOGO_POSITION,
    bgm_path: str | None = None,
    bgm_volume: float = DEFAULT_BGM_VOLUME,
    intro_path: str | None = None,
    outro_path: str | None = None,
    custom_dict_path: str | None = None,
) -> None:
    if not slides:
        raise VideoComposeError("slides must not be empty")

    if shutil.which("ffmpeg") is None:
        raise VideoComposeError("ffmpeg executable not found")

    durations_ms = [get_audio_duration_ms(s.audio_path) for s in slides]

    for slide, duration_ms in zip(slides, durations_ms):
        for overlay in slide.broll_overlays:
            if overlay.end_ms > duration_ms:
                raise VideoComposeError(
                    f"broll overlay [{overlay.start_ms}, {overlay.end_ms}) falls outside "
                    f"slide's own audio duration (0, {duration_ms})"
                )

    # Reassigned (not a new variable) so every downstream use of
    # transition_duration_ms below — offsets, subtitle cues, and the ffmpeg
    # xfade/acrossfade filters — automatically agrees on the same,
    # per-slide-audio-safe value.
    transition_duration_ms = _effective_transition_ms(durations_ms, transition_duration_ms)
    offsets_ms = _compute_slide_offsets(durations_ms, transition_duration_ms)

    # A per-job custom dictionary needs its own isolated jieba.Tokenizer (see
    # wordseg.py) — the shared default segmenter otherwise covers every job
    # that doesn't supply one, so it's not rebuilt from scratch here.
    word_segmenter = (
        WordSegmenter(custom_dict_path) if custom_dict_path else get_default_segmenter()
    )
    cues = _build_cues(slides, durations_ms, transition_duration_ms, word_segmenter)
    with open(out_srt_path, "w", encoding="utf-8", newline="") as f:
        f.write(cues_to_srt(cues))

    needs_post_processing = bool(logo_path or bgm_path or intro_path or outro_path)
    temp_dir = tempfile.mkdtemp(prefix="ppt2course_video_") if needs_post_processing else None

    try:
        core_output = f"{temp_dir}/01_core.mp4" if needs_post_processing else out_video_path

        cmd = _build_ffmpeg_command(
            slides,
            durations_ms,
            offsets_ms,
            out_srt_path,
            core_output,
            transition,
            transition_duration_ms,
            resolution,
            fps,
            font_size,
            subtitle_margin_v=subtitle_margin_v,
        )
        _run_ffmpeg(cmd, "slide/transition/subtitle composition")

        current = core_output

        if logo_path:
            next_path = f"{temp_dir}/02_logo.mp4"
            _add_logo_overlay(
                current,
                logo_path,
                next_path,
                logo_width,
                logo_margin,
                opacity=logo_opacity,
                position=logo_position,
            )
            current = next_path

        if bgm_path:
            next_path = f"{temp_dir}/03_bgm.mp4"
            _mix_background_music(current, bgm_path, next_path, bgm_volume)
            current = next_path

        if intro_path or outro_path:
            _concatenate_with_intro_outro(
                current, out_video_path, intro_path, outro_path, resolution, fps
            )
        elif current != out_video_path:
            shutil.copy(current, out_video_path)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
