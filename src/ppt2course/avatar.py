"""2D presenter avatar — mouth-flap timing derived from real narration audio.

Like every other optional visual add-on in this pipeline (B-roll, Ken Burns),
the avatar is strictly a decoration layered on top of the Narration Audio
master timeline: it never triggers a new TTS call, never changes a slide's
own audio duration, and a problem here (missing asset, bad config) degrades
to "no avatar on this slide" rather than failing the job.

Mouth timing comes from tts.py's real per-word ``TimedChunk`` list — the
exact same data ``video.py`` already uses to burn in subtitles — so the
avatar's mouth can never disagree with what's actually audible.
"""

import importlib.resources
from dataclasses import dataclass
from enum import Enum

from ppt2course.subtitle import TimedChunk


class AvatarState(str, Enum):
    IDLE = "idle"
    TALK_OPEN = "talk_open"
    TALK_CLOSE = "talk_close"
    POINT = "point"
    WAVE = "wave"


class AvatarMode(str, Enum):
    NONE = "none"
    # "Key" slides only: those with actual narration to accompany. Slides
    # with empty/whitespace-only script (pure title/silent slides) are
    # skipped so the avatar isn't left standing idle over nothing to say.
    KEYFRAME = "keyframe"
    ALWAYS = "always"
    CUSTOM = "custom"


AVATAR_MODES = tuple(m.value for m in AvatarMode)


class AvatarPosition(str, Enum):
    BOTTOM_RIGHT = "bottom_right"
    BOTTOM_LEFT = "bottom_left"
    RIGHT = "right"
    LEFT = "left"


AVATAR_POSITIONS = tuple(p.value for p in AvatarPosition)


class AvatarSize(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


AVATAR_SIZES = tuple(s.value for s in AvatarSize)

DEFAULT_AVATAR_MODE = AvatarMode.NONE.value
DEFAULT_AVATAR_POSITION = AvatarPosition.BOTTOM_RIGHT.value
DEFAULT_AVATAR_SIZE = AvatarSize.SMALL.value

# A gap this long (or longer) between two spoken TimedChunks drops the
# avatar back to idle instead of continuing to flap its mouth across a
# natural pause.
DEFAULT_SILENCE_GAP_MS = 350


@dataclass(frozen=True)
class AvatarAssetSet:
    """Resolved image paths for one avatar's states. Only ``idle`` and
    ``talk_open`` are required; anything else falls back to a reasonable
    neighbor, so a custom asset pack can be as small as two images."""

    idle: str
    talk_open: str
    talk_close: str | None = None
    point: str | None = None
    wave: str | None = None

    def path_for(self, state: AvatarState) -> str:
        return {
            AvatarState.IDLE: self.idle,
            AvatarState.TALK_OPEN: self.talk_open,
            AvatarState.TALK_CLOSE: self.talk_close or self.idle,
            AvatarState.POINT: self.point or self.idle,
            AvatarState.WAVE: self.wave or self.idle,
        }[state]


def default_asset_set() -> AvatarAssetSet:
    """The bundled placeholder character (see
    scripts/generate_default_avatar.py) — a flat-icon-style bust, not real
    character art, used whenever the caller doesn't supply their own."""
    base = importlib.resources.files("ppt2course") / "assets" / "avatar" / "default"

    def p(name: str) -> str:
        return str(base / f"{name}.png")

    return AvatarAssetSet(
        idle=p("idle"),
        talk_open=p("talk_open"),
        talk_close=p("talk_close"),
        point=p("point"),
        wave=p("wave"),
    )


@dataclass(frozen=True)
class AvatarSegment:
    state: AvatarState
    start_ms: int
    end_ms: int


def build_avatar_track(
    chunks: list[TimedChunk],
    duration_ms: int,
    silence_gap_ms: int = DEFAULT_SILENCE_GAP_MS,
) -> list[AvatarSegment]:
    """Turn one slide's real TimedChunk word timings into a mouth-flap
    timeline on that slide's own local audio clock (0 = narration start).

    Consecutive spoken chunks less than ``silence_gap_ms`` apart are grouped
    into one continuous "speaking run"; within a run the mouth alternates
    open/close per word. A gap of ``silence_gap_ms`` or more — including
    before the first word or after the last — is idle. The returned segments
    always cover ``[0, duration_ms)`` contiguously with no gaps, so an
    overlay built from them never leaves a frame with no avatar visible.
    """
    if duration_ms <= 0:
        return []

    spoken = sorted(
        (c for c in chunks if c.text.strip() and c.end_ms > c.start_ms),
        key=lambda c: c.start_ms,
    )
    if not spoken:
        return [AvatarSegment(AvatarState.IDLE, 0, duration_ms)]

    groups: list[list[TimedChunk]] = [[spoken[0]]]
    for chunk in spoken[1:]:
        if chunk.start_ms - groups[-1][-1].end_ms >= silence_gap_ms:
            groups.append([chunk])
        else:
            groups[-1].append(chunk)

    segments: list[AvatarSegment] = []
    cursor = 0
    for group in groups:
        group_start = max(0, min(group[0].start_ms, duration_ms))
        group_end = max(0, min(group[-1].end_ms, duration_ms))
        if group_start > cursor:
            segments.append(AvatarSegment(AvatarState.IDLE, cursor, group_start))

        talk_open = True
        for i, chunk in enumerate(group):
            seg_start = max(group_start, min(chunk.start_ms, duration_ms))
            next_start = group[i + 1].start_ms if i + 1 < len(group) else group_end
            seg_end = max(seg_start, min(next_start, duration_ms))
            if seg_end <= seg_start:
                continue
            state = AvatarState.TALK_OPEN if talk_open else AvatarState.TALK_CLOSE
            talk_open = not talk_open
            segments.append(AvatarSegment(state, seg_start, seg_end))

        cursor = max(cursor, group_end)

    if cursor < duration_ms:
        segments.append(AvatarSegment(AvatarState.IDLE, cursor, duration_ms))

    return segments


def should_show_avatar(
    mode: str,
    slide_number: int,
    script_text: str,
    custom_slide_numbers: tuple[int, ...] = (),
) -> bool:
    """Decide whether a given slide gets an avatar at all, per the four
    modes the UI exposes (不使用/關鍵頁面/全程顯示/自訂頁面). Called once per
    slide before spending any time building its mouth-flap track."""
    if mode == AvatarMode.ALWAYS.value:
        return True
    if mode == AvatarMode.KEYFRAME.value:
        return bool(script_text.strip())
    if mode == AvatarMode.CUSTOM.value:
        return slide_number in custom_slide_numbers
    return False
