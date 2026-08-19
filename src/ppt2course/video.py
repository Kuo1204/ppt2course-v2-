"""STEP 5: Video composition — xfade transitions, subtitle burn-in, optional
Logo overlay / BGM mixing / intro-outro concatenation.

This module is the single place that computes the post-transition cumulative
timeline, so the burned-in subtitles and the xfade/acrossfade-shortened video
always agree on where each slide actually starts — per-slide TimedChunk lists
(STEP3's raw, un-offset output) go in, offsets are computed here, and
STEP4's generate_cues/cues_to_srt are called with those offsets baked in.
"""

import os
import re
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

# ffmpeg overlay= x:y expressions per avatar corner/side, in terms of the
# overlay filter's own W/H (base video) and w/h (avatar) variables. Distinct
# from _LOGO_POSITION_OVERLAYS above: the avatar only ever anchors to a
# bottom corner or a vertically-centered side, matching the four choices the
# UI exposes (右下/左下/右側/左側) — it never sits in a top corner, which
# would collide with a typical slide's own title text.
_AVATAR_POSITION_OVERLAYS = {
    "bottom_right": ("W-w-{margin}", "H-h-{margin}"),
    "bottom_left": ("{margin}", "H-h-{margin}"),
    "right": ("W-w-{margin}", "(H-h)/2"),
    "left": ("{margin}", "(H-h)/2"),
}
AVATAR_POSITIONS = tuple(_AVATAR_POSITION_OVERLAYS)
DEFAULT_AVATAR_POSITION = "bottom_right"

# Avatar height as a fraction of the output frame's own height; width
# follows automatically from the source PNG's aspect ratio. "small" keeps a
# portrait bust from ever competing with the slide content behind it.
_AVATAR_SIZE_HEIGHT_FRACTION = {"small": 0.28, "medium": 0.40, "large": 0.55}
AVATAR_SIZES = tuple(_AVATAR_SIZE_HEIGHT_FRACTION)
DEFAULT_AVATAR_SIZE = "small"
DEFAULT_AVATAR_MARGIN = 24

# When True, a slide's narration never starts until the previous slide's
# own narration (plus its own reading pause) has completely finished —
# the video still crossfades purely visually, but audio switches from
# acrossfade (blended, overlapping) to a straight concat (silent gap,
# never overlapping). See compose_video's avoid_voice_overlap handling.
DEFAULT_AVOID_VOICE_OVERLAP = False

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
DEFAULT_SUBTITLE_FONT_COLOR = "#FFFFFF"
DEFAULT_SUBTITLE_OUTLINE_COLOR = "#000000"
# ASS/libass styles only ever carry a boolean Bold flag (-1 true / 0 false)
# — there's no continuous weight scale the way CSS font-weight has one,
# since it just picks between the family's regular and bold faces.
DEFAULT_SUBTITLE_BOLD = False

_HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


class VideoComposeError(Exception):
    pass


def _hex_to_ass_color(hex_color: str) -> str:
    """Converts a "#RRGGBB" (or "RRGGBB") web color into libass's
    "&HBBGGRR&" force_style color format — ASS stores colors byte-swapped
    (blue first) relative to the familiar RGB hex order."""
    match = _HEX_COLOR_RE.match(hex_color)
    if not match:
        raise VideoComposeError(
            f"invalid color {hex_color!r}; expected a 6-digit hex color like #FFFFFF"
        )
    rr, gg, bb = match.group(1)[0:2], match.group(1)[2:4], match.group(1)[4:6]
    return f"&H{bb}{gg}{rr}&".upper()


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
class AvatarOverlay:
    """One corner-anchored avatar image swap over this slide, timed against
    that slide's own audio (0 = the slide's narration start) — same
    contract as BrollOverlay, and for the same reason: ``compose_video``
    never reads this when computing ``durations_ms``, offsets, or subtitle
    cues, so the avatar can only change what's drawn in its corner, never
    move a frame of narration or subtitle timing.
    """

    image_path: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise VideoComposeError(
                f"avatar overlay must have 0 <= start_ms < end_ms, got "
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
    # Optional corner-anchored 2D avatar mouth-flap frames. Empty by
    # default, so every existing caller/test is unaffected.
    avatar_overlays: tuple[AvatarOverlay, ...] = ()
    # Extra silent hold time (ms) appended after this slide's own narration
    # ends, purely so the viewer has a beat to read the slide before it
    # transitions away. This extends the slide's own *visual* (and, via
    # silent padding, audio-track) duration only — the real narration audio
    # file on disk, its chunks, and the subtitle cues built from those
    # chunks are completely untouched; see _build_cues and
    # compose_video's narration_durations_ms vs. visual_durations_ms split
    # below. 0 by default, so every existing caller/test is unaffected.
    reading_pause_ms: int = 0

    def __post_init__(self) -> None:
        if self.reading_pause_ms < 0:
            raise VideoComposeError(
                f"reading_pause_ms must be >= 0, got {self.reading_pause_ms}"
            )


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
    #
    # The comparison below deliberately uses each slide's *raw* (pre-delay)
    # last-cue end — never the delayed one — to decide whether the next
    # slide needs delaying. Every crossfaded boundary structurally overlaps
    # by ~transition_ms (that's what a crossfade *is*: offsets[i] is placed
    # transition_ms before slide i-1's narration actually ends), so with a
    # non-zero transition this delay fires at *every* boundary. Chaining it
    # off the already-delayed end (as an earlier version did) compounded
    # that delay by roughly (transition_ms + MIN_GAP_MS) on every single
    # slide — subtitles drifting several seconds behind the real
    # audio/video by the back half of a longer deck, while the actual
    # rendered video (whose xfade/acrossfade offsets are fixed and never
    # inherit this delay) stayed perfectly on schedule. Anchoring to the
    # raw end instead keeps each boundary's adjustment a small, constant
    # amount instead of an ever-growing one, at the cost of tolerating a
    # brief (typically <= transition_ms) on-screen overlap between two
    # captions right at the cut — which mirrors what's actually audible
    # during that same window anyway (both narrations are genuinely
    # blended together there), so it reads as correct rather than broken.
    reconciled: list[list[SubtitleCue]] = []
    raw_prev_last_end_ms: int | None = None
    for cues in per_slide_cues:
        raw_last_end_ms = cues[-1].end_ms if cues else None
        if cues and raw_prev_last_end_ms is not None:
            min_start = raw_prev_last_end_ms + MIN_GAP_MS
            if cues[0].start_ms < min_start:
                cues = _delay_cues(cues, min_start - cues[0].start_ms)
        reconciled.append(cues)
        if raw_last_end_ms is not None:
            raw_prev_last_end_ms = raw_last_end_ms

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
    prev_label: str | None = None,
    final_label: str | None = None,
) -> list[str]:
    """Build the filter lines that swap each B-roll over slide ``slide_index``
    during its own [start_ms, end_ms) window, on top of that slide's already
    scaled/padded base image (labeled ``v{slide_index}base`` unless
    ``prev_label`` overrides it).

    Ends at ``final_label`` (default ``v{slide_index}``, the label
    ``_video_xfade_chain`` expects when nothing comes after B-roll). Callers
    that still need to layer an avatar overlay on top pass a different
    ``final_label`` so that stays the true end of this slide's chain
    instead.
    """
    parts: list[str] = []
    prev_label = prev_label or f"v{slide_index}base"
    input_index = first_input_index
    for i, overlay in enumerate(overlays):
        cover_label = f"v{slide_index}broll{i}"
        parts.append(_cover_scale_filter(input_index, cover_label, width, height, fps))
        is_last = i == len(overlays) - 1
        out_label = (final_label or f"v{slide_index}") if is_last else f"v{slide_index}mix{i}"
        start_sec = overlay.start_ms / 1000
        end_sec = overlay.end_ms / 1000
        parts.append(
            f"[{prev_label}][{cover_label}]overlay=enable='between(t,{start_sec},{end_sec})'"
            f"[{out_label}]"
        )
        prev_label = out_label
        input_index += 1
    return parts


def _avatar_fit_filter(index: int, out_label: str, height_px: int, fps: int) -> str:
    # "fit" (scale to a target height, width follows from the source's own
    # aspect ratio), not "cover" or "contain" against a fixed box — an
    # avatar PNG has no frame to fill, it just needs to end up at the right
    # on-screen size. format=rgba keeps the source PNG's alpha channel
    # (transparent background) alive through the scale.
    return f"[{index}:v]format=rgba,scale=-2:{height_px}:flags=lanczos,fps={fps}[{out_label}]"


def _avatar_scale_and_overlay_filters(
    slide_index: int,
    overlays: tuple[AvatarOverlay, ...],
    first_input_index: int,
    prev_label: str,
    final_label: str,
    height_px: int,
    position: str,
    margin: int,
    fps: int,
) -> list[str]:
    """Same shape as ``_broll_scale_and_overlay_filters``, but fit-scaled to
    ``height_px`` and anchored to one corner/side via the overlay filter's
    x/y expressions instead of covering the whole frame.
    """
    try:
        x_expr, y_expr = _AVATAR_POSITION_OVERLAYS[position]
    except KeyError:
        raise VideoComposeError(
            f"unknown avatar_position {position!r}; expected one of {AVATAR_POSITIONS}"
        ) from None
    overlay_xy = f"{x_expr}:{y_expr}".format(margin=margin)

    parts: list[str] = []
    input_index = first_input_index
    current_label = prev_label
    for i, overlay in enumerate(overlays):
        fit_label = f"v{slide_index}avatar{i}"
        parts.append(_avatar_fit_filter(input_index, fit_label, height_px, fps))
        is_last = i == len(overlays) - 1
        out_label = final_label if is_last else f"v{slide_index}avatarmix{i}"
        start_sec = overlay.start_ms / 1000
        end_sec = overlay.end_ms / 1000
        parts.append(
            f"[{current_label}][{fit_label}]overlay={overlay_xy}:"
            f"enable='between(t,{start_sec},{end_sec})'[{out_label}]"
        )
        current_label = out_label
        input_index += 1
    return parts


def _audio_label_filter(
    input_index: int, slide_index: int, visual_duration_ms: int | None = None
) -> str:
    # Only a slide with an actual reading pause needs padding — every other
    # slide keeps the exact "anull" passthrough string it always had, so a
    # job with reading_pause_ms=0 everywhere (the default) produces a
    # byte-identical filter graph to before this feature existed. apad's
    # whole_dur pads with silence up to the target only if the real stream
    # is shorter (never trims), so this is safe even against a rounding
    # mismatch between the ms this was computed from and ffmpeg's own
    # measurement of the same audio file.
    if visual_duration_ms is None:
        return f"[{input_index}:a]anull[a{slide_index}]"
    return f"[{input_index}:a]apad=whole_dur={visual_duration_ms / 1000}[a{slide_index}]"


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


def _audio_concat_chain(n: int) -> tuple[str, str]:
    # avoid_voice_overlap's audio path: every [a{i}] stream is already
    # padded (via _audio_label_filter) to exactly that slide's own
    # visual_durations_ms — so placing them back-to-back with concat, with
    # no crossfade blend at all, is exactly "the next slide's narration
    # never starts until the previous one (and its own reading pause) has
    # completely finished playing".
    if n == 1:
        return "", "a0"
    inputs = "".join(f"[a{i}]" for i in range(n))
    return f"{inputs}concat=n={n}:v=0:a=1[aout]", "aout"


def _subtitle_filter(
    srt_path: str,
    video_label: str,
    font_size: int,
    resolution: tuple[int, int],
    margin_v: int = DEFAULT_SUBTITLE_MARGIN_V,
    font_color: str = DEFAULT_SUBTITLE_FONT_COLOR,
    outline_color: str = DEFAULT_SUBTITLE_OUTLINE_COLOR,
    bold: bool = DEFAULT_SUBTITLE_BOLD,
) -> tuple[str, str]:
    width, height = resolution
    escaped_path = _escape_ffmpeg_filter_path(srt_path)
    primary_ass = _hex_to_ass_color(font_color)
    outline_ass = _hex_to_ass_color(outline_color)
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
        f"PrimaryColour={primary_ass},OutlineColour={outline_ass},"
        f"Bold={-1 if bold else 0},"
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
    subtitle_font_color: str = DEFAULT_SUBTITLE_FONT_COLOR,
    subtitle_outline_color: str = DEFAULT_SUBTITLE_OUTLINE_COLOR,
    subtitle_bold: bool = DEFAULT_SUBTITLE_BOLD,
    avatar_position: str = DEFAULT_AVATAR_POSITION,
    avatar_size: str = DEFAULT_AVATAR_SIZE,
    avatar_margin: int = DEFAULT_AVATAR_MARGIN,
    avoid_voice_overlap: bool = DEFAULT_AVOID_VOICE_OVERLAP,
    audio_pad_durations_ms: list[int] | None = None,
) -> list[str]:
    width, height = resolution
    n = len(slides)
    # Normally identical to durations_ms (each slide pads its own audio up
    # to its own on-screen hold time) — compose_video passes a separate,
    # unextended list here in avoid_voice_overlap mode, where durations_ms
    # itself has the last slide's hold time stretched to cover the
    # now-non-overlapping audio concat's real total length. Padding audio
    # to that stretched figure too would push extra silence *inside* the
    # concat instead of after it.
    if audio_pad_durations_ms is None:
        audio_pad_durations_ms = durations_ms

    cmd = ["ffmpeg", "-y"]
    for slide, duration_ms in zip(slides, durations_ms):
        cmd += ["-loop", "1", "-t", str(duration_ms / 1000), "-i", slide.image_path]
    for slide in slides:
        cmd += ["-i", slide.audio_path]

    try:
        avatar_height_px = int(height * _AVATAR_SIZE_HEIGHT_FRACTION[avatar_size])
    except KeyError:
        raise VideoComposeError(
            f"unknown avatar_size {avatar_size!r}; expected one of {AVATAR_SIZES}"
        ) from None

    # B-roll and avatar images are appended last, after every slide image
    # (0..n-1) and every slide audio (n..2n-1) — so those two existing index
    # ranges never shift, regardless of which slides have B-roll/avatar or
    # how many.
    scale_filters: list[str] = []
    next_input_index = 2 * n
    for i, slide in enumerate(slides):
        has_broll = bool(slide.broll_overlays)
        has_avatar = bool(slide.avatar_overlays)
        needs_own_label = has_broll or has_avatar

        if not needs_own_label:
            base_label = f"v{i}"
            scale_filters.append(_scale_pad_filter(i, width, height, fps))
            continue

        base_label = f"v{i}base"
        scale_filters.append(_scale_pad_filter(i, width, height, fps, out_label=base_label))
        current_label = base_label

        if has_broll:
            broll_final_label = f"v{i}brolled" if has_avatar else f"v{i}"
            for overlay in slide.broll_overlays:
                cmd += ["-loop", "1", "-t", str(durations_ms[i] / 1000), "-i", overlay.image_path]
            scale_filters.extend(
                _broll_scale_and_overlay_filters(
                    i,
                    slide.broll_overlays,
                    next_input_index,
                    width,
                    height,
                    fps,
                    prev_label=current_label,
                    final_label=broll_final_label,
                )
            )
            next_input_index += len(slide.broll_overlays)
            current_label = broll_final_label

        if has_avatar:
            for overlay in slide.avatar_overlays:
                cmd += ["-loop", "1", "-t", str(durations_ms[i] / 1000), "-i", overlay.image_path]
            scale_filters.extend(
                _avatar_scale_and_overlay_filters(
                    i,
                    slide.avatar_overlays,
                    next_input_index,
                    prev_label=current_label,
                    final_label=f"v{i}",
                    height_px=avatar_height_px,
                    position=avatar_position,
                    margin=avatar_margin,
                    fps=fps,
                )
            )
            next_input_index += len(slide.avatar_overlays)

    audio_label_filters = [
        _audio_label_filter(
            n + i,
            i,
            visual_duration_ms=audio_pad_durations_ms[i] if slide.reading_pause_ms > 0 else None,
        )
        for i, slide in enumerate(slides)
    ]
    xfade_filter, video_label = _video_xfade_chain(n, transition, transition_duration_ms, offsets_ms)
    if avoid_voice_overlap:
        audio_filter, audio_label = _audio_concat_chain(n)
    else:
        audio_filter, audio_label = _audio_acrossfade_chain(n, transition_duration_ms)
    sub_filter, final_video_label = _subtitle_filter(
        srt_path, video_label, font_size, resolution, margin_v=subtitle_margin_v,
        font_color=subtitle_font_color, outline_color=subtitle_outline_color,
        bold=subtitle_bold,
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


# On Windows, subprocess has no argv[] like POSIX exec -- CreateProcess only
# takes a single command-line string, internally capped at roughly 32K
# characters, and Python's subprocess.run() joins the whole cmd list into
# one before handing it over. A deck with many slides/overlays already
# spends a lot of that budget on repeated "-i <long absolute path>" pairs
# (this project's own working directories run long -- OneDrive + Chinese
# folder names). That surfaces as CreateProcess itself failing with
# WinError 206 ("the filename or extension is too long") before ffmpeg
# ever runs. Shortening the -i/output path arguments below is a plain
# string-length optimization ffmpeg itself has no opinion on, so it works
# regardless of ffmpeg build/version -- unlike -filter_complex_script, which
# a real user's ffmpeg build (a current gyan.dev Windows build) turned out to
# reject outright, so that approach isn't used here.
#
# IMPORTANT: a real reported recurrence of this same error turned out to
# have a total command length of only ~5,400 characters -- nowhere near this
# threshold, and nowhere near Windows' real limit either. So command length
# is not the only cause of WinError 206 here: there is also a known
# Windows/Python behavior where a long-running process that repeatedly calls
# CreateProcess (as this job worker does, once per ffmpeg step, for as many
# jobs as the server has processed since it started) can eventually hit the
# same error for reasons unrelated to any single command's length -- see the
# close_fds=False and retry handling in _run_ffmpeg below, which cover that
# case. This threshold-gated path-shortening is kept because it's still a
# real, if less common, way to hit the same error on a very large deck.
_COMMAND_LENGTH_REWRITE_THRESHOLD = 20000

# A real recurrence of WinError 206 happened on a small (10-slide) job whose
# actual command was only ~5,400 characters -- far too short to be a command-
# line-length issue. This matches a separate, known Windows/Python behavior:
# subprocess.run's default close_fds=True makes CreateProcess build an
# explicit inheritable-handle list every call, and in a long-running process
# that keeps launching subprocesses (exactly what this job worker does, once
# per ffmpeg step across every job the server processes over its lifetime),
# that bookkeeping has been reported to eventually fail with this exact
# misleading error, unrelated to command length. close_fds=False skips that
# code path entirely (this only affects which OS handles the child *could*
# inherit, not which files ffmpeg is told to read/write -- ffmpeg is only
# ever given the paths explicitly listed in cmd regardless). Paired with one
# retry, since if this is an intermittent environmental hiccup rather than a
# deterministic one, a second attempt is likely to succeed.
_LAUNCH_RETRY_ATTEMPTS = 2


def _shorten_command_paths(cmd: list[str]) -> tuple[list[str], str | None]:
    """Rewrite -i/output path arguments to be relative to their common
    directory, and return that directory to run ffmpeg from as cwd. A
    no-op (returns cmd, None) unless the command is already long enough
    that the rewrite is worth doing.
    """
    if sum(len(arg) for arg in cmd) < _COMMAND_LENGTH_REWRITE_THRESHOLD:
        return cmd, None

    # Every builder in this module follows the same shape: each input path
    # follows an "-i" flag, and the output path is always the trailing
    # argument.
    path_indices = [i + 1 for i, arg in enumerate(cmd) if arg == "-i"]
    if cmd:
        path_indices.append(len(cmd) - 1)

    abs_paths = [cmd[i] for i in path_indices if os.path.isabs(cmd[i])]
    if not abs_paths:
        return cmd, None

    try:
        common_dir = os.path.commonpath([os.path.dirname(p) for p in abs_paths])
    except ValueError:
        # e.g. paths on different drives -- nothing sensible to make relative to
        return cmd, None

    new_cmd = list(cmd)
    for i in path_indices:
        if os.path.isabs(cmd[i]):
            new_cmd[i] = os.path.relpath(cmd[i], common_dir)
    return new_cmd, common_dir


def _run_ffmpeg(cmd: list[str], step_description: str) -> None:
    cmd = list(cmd)
    # Every builder in this module emits the bare string "ffmpeg" as cmd[0]
    # and relies on Windows' own CreateProcess to search PATH for it fresh
    # on every single invocation. Resolving it to a full path once up front
    # instead removes that PATH-search step from the equation entirely --
    # a real WinError 206 recurrence traced this far (small deck, short
    # command, first job on a freshly restarted server) couldn't be
    # reproduced standalone with identical files/command, which points at
    # something specific to how the live server process resolves "ffmpeg"
    # rather than the command itself. Resolving up front removes that
    # variable regardless of the exact underlying mechanism.
    if cmd and cmd[0] == "ffmpeg":
        resolved = shutil.which("ffmpeg")
        if resolved:
            # On this machine (and likely most winget-installed ffmpeg
            # setups), that resolves to a winget shim under
            # AppData\...\WinGet\Links\ffmpeg.EXE -- a reparse point, not
            # the real binary -- which itself launches the real ffmpeg.exe
            # as a further subprocess. realpath() follows that reparse
            # point so the real binary is launched directly, one layer of
            # indirection (and one more process spawn that could itself hit
            # OS-level launch quirks) fewer. Only swapped in when the
            # resolved target actually exists, so a mocked/fake path (as
            # every test in this module uses) is left alone rather than
            # silently mangled by realpath's own path normalization.
            real_target = os.path.realpath(resolved)
            if os.path.exists(real_target):
                cmd[0] = real_target

    cmd, cwd = _shorten_command_paths(cmd)

    last_launch_error: OSError | None = None
    for attempt in range(1, _LAUNCH_RETRY_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, cwd=cwd, close_fds=False
            )
            break
        except OSError as exc:
            # e.g. the CreateProcess-level WinError 206 above -- this is
            # ffmpeg never actually launching, distinct from ffmpeg launching
            # and then failing (handled below via returncode). Retried a
            # couple of times since this specific error has been seen to be
            # an intermittent environmental hiccup rather than deterministic.
            last_launch_error = exc
    else:
        raise VideoComposeError(
            f"failed to launch ffmpeg during {step_description}: {last_launch_error}"
        ) from last_launch_error

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
    subtitle_font_color: str = DEFAULT_SUBTITLE_FONT_COLOR,
    subtitle_outline_color: str = DEFAULT_SUBTITLE_OUTLINE_COLOR,
    subtitle_bold: bool = DEFAULT_SUBTITLE_BOLD,
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
    avatar_position: str = DEFAULT_AVATAR_POSITION,
    avatar_size: str = DEFAULT_AVATAR_SIZE,
    avatar_margin: int = DEFAULT_AVATAR_MARGIN,
    avoid_voice_overlap: bool = DEFAULT_AVOID_VOICE_OVERLAP,
) -> None:
    if not slides:
        raise VideoComposeError("slides must not be empty")

    if shutil.which("ffmpeg") is None:
        raise VideoComposeError("ffmpeg executable not found")

    # narration_durations_ms is the one true master timeline (real audio, as
    # measured off disk) — B-roll/avatar overlay windows are validated
    # against it, and subtitle cues (via slide.chunks, which know nothing
    # about any padding) always land inside it regardless of what follows.
    # visual_durations_ms adds each slide's own reading_pause_ms on top —
    # that's the number everything about *layout in time* (crossfade
    # offsets, image -t, this slide's own audio padded with trailing
    # silence) is computed from. A reading_pause_ms of 0 everywhere (the
    # default) makes the two lists identical, so nothing here changes
    # behavior for a caller that has never heard of reading pauses.
    narration_durations_ms = [get_audio_duration_ms(s.audio_path) for s in slides]
    visual_durations_ms = [
        n + s.reading_pause_ms for n, s in zip(narration_durations_ms, slides)
    ]

    for slide, duration_ms in zip(slides, narration_durations_ms):
        for overlay in slide.broll_overlays:
            if overlay.end_ms > duration_ms:
                raise VideoComposeError(
                    f"broll overlay [{overlay.start_ms}, {overlay.end_ms}) falls outside "
                    f"slide's own audio duration (0, {duration_ms})"
                )
        for overlay in slide.avatar_overlays:
            if overlay.end_ms > duration_ms:
                raise VideoComposeError(
                    f"avatar overlay [{overlay.start_ms}, {overlay.end_ms}) falls outside "
                    f"slide's own audio duration (0, {duration_ms})"
                )

    # Reassigned (not a new variable) so every downstream use of
    # transition_duration_ms below — offsets, subtitle cues, and the ffmpeg
    # xfade/acrossfade filters — automatically agrees on the same,
    # per-slide-visual-duration-safe value. The safety clamp itself is
    # always computed against the real (unextended) visual_durations_ms
    # below, regardless of avoid_voice_overlap — extending durations only
    # ever adds room, never removes it, so clamping against the
    # unextended figures stays conservative either way.
    transition_duration_ms = _effective_transition_ms(visual_durations_ms, transition_duration_ms)

    # avoid_voice_overlap: audio can no longer borrow transition_duration_ms
    # back from the next slide's start the way a normal crossfade does, so
    # its own placement is a plain concatenation (transition_ms=0 reproduces
    # exactly that — no slide's audio starts before the previous one, plus
    # its own reading pause, has fully finished; see cue_transition_ms
    # below, which the audio's own apad+concat chain is built to match).
    #
    # The video's crossfade must land on that *same* concatenated timeline,
    # not the usual compressed one — otherwise slide i's picture appears up
    # to (i * transition_duration_ms) *before* its narration has even
    # started once a deck runs more than a couple of slides, which is
    # exactly the "投影片已經換了，字幕/旁白還在講上一頁" bug this feature
    # was supposed to prevent, not reproduce. Every slide except the first
    # gets its own raw on-screen duration padded by transition_duration_ms
    # before being run through the *same* compression-based offset math
    # used everywhere else; algebraically that lands each xfade so it
    # finishes blending into slide i at the exact instant slide i's
    # (concatenated) audio begins — see test_video.py for the derivation
    # spelled out slide by slide. Static looped images have no visible
    # content of their own, so "borrowing" extra raw duration to blend from
    # doesn't change what the transition looks like, only when it lands.
    video_durations_ms = visual_durations_ms
    cue_transition_ms = transition_duration_ms
    if avoid_voice_overlap and len(slides) > 1:
        cue_transition_ms = 0
        video_durations_ms = [
            d if i == 0 else d + transition_duration_ms
            for i, d in enumerate(visual_durations_ms)
        ]

    offsets_ms = _compute_slide_offsets(video_durations_ms, transition_duration_ms)

    # A per-job custom dictionary needs its own isolated jieba.Tokenizer (see
    # wordseg.py) — the shared default segmenter otherwise covers every job
    # that doesn't supply one, so it's not rebuilt from scratch here.
    word_segmenter = (
        WordSegmenter(custom_dict_path) if custom_dict_path else get_default_segmenter()
    )
    # Cue placement uses visual_durations_ms (where each slide's own local
    # clock actually starts in the final render, including any reading
    # pause before it) — but the cues *within* a slide only ever span
    # slide.chunks, which is real narration timing untouched by the pause,
    # so captions still end exactly at the real narration end.
    cues = _build_cues(slides, visual_durations_ms, cue_transition_ms, word_segmenter)
    with open(out_srt_path, "w", encoding="utf-8", newline="") as f:
        f.write(cues_to_srt(cues))

    needs_post_processing = bool(logo_path or bgm_path or intro_path or outro_path)
    temp_dir = tempfile.mkdtemp(prefix="ppt2course_video_") if needs_post_processing else None

    try:
        core_output = f"{temp_dir}/01_core.mp4" if needs_post_processing else out_video_path

        cmd = _build_ffmpeg_command(
            slides,
            video_durations_ms,
            offsets_ms,
            out_srt_path,
            core_output,
            transition,
            transition_duration_ms,
            resolution,
            fps,
            font_size,
            subtitle_margin_v=subtitle_margin_v,
            subtitle_font_color=subtitle_font_color,
            subtitle_outline_color=subtitle_outline_color,
            subtitle_bold=subtitle_bold,
            avatar_position=avatar_position,
            avatar_size=avatar_size,
            avatar_margin=avatar_margin,
            avoid_voice_overlap=avoid_voice_overlap,
            audio_pad_durations_ms=visual_durations_ms,
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
