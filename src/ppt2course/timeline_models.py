"""Typed data contracts for narration-led course timelines.

These models deliberately contain no FFmpeg, API, or UI behavior.  They are
the boundary between future visual-analysis/UI features and the existing
render pipeline.  Milliseconds are used throughout to match edge-tts,
subtitle, and ffprobe timing without repeated float conversions.
"""

from dataclasses import dataclass, field
from enum import Enum


class TimelineValidationError(ValueError):
    """Raised when a timeline model would violate timing invariants."""


class TimelineBlockType(str, Enum):
    SLIDE = "slide"
    AI_INSERT = "ai_insert"


class VisualAssetType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


class VisualEventType(str, Enum):
    SLIDE = "slide"
    BROLL = "broll"
    OVERLAY = "overlay"
    AVATAR = "avatar"


class VisualPlacement(str, Enum):
    AUTO = "auto"
    BESIDE_SLIDE = "beside_slide"
    FULLSCREEN_BROLL = "fullscreen_broll"
    OVER_SLIDE = "over_slide"
    BEFORE_SLIDE = "before_slide"
    AFTER_SLIDE = "after_slide"


@dataclass(frozen=True)
class VisualAsset:
    """A normalized candidate returned by a media provider."""

    source: str
    asset_type: VisualAssetType
    preview_url: str
    download_url: str
    keyword: str
    slide_number: int
    photographer: str = ""
    local_path: str | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise TimelineValidationError("visual asset source must not be empty")
        if self.slide_number < 1:
            raise TimelineValidationError("visual asset slide_number must be at least 1")
        if not self.preview_url.strip() or not self.download_url.strip():
            raise TimelineValidationError("visual asset URLs must not be empty")


@dataclass(frozen=True)
class VisualRecommendation:
    """AI/heuristic advice for one slide; it is not user approval."""

    slide_number: int
    title: str
    visual_need_score: int
    recommended: bool
    reason: str
    visual_type: VisualAssetType
    keywords: tuple[str, ...] = ()
    suggested_position: str = "during_slide"
    # A short verbatim excerpt of this slide's own narration script, used to
    # anchor *when* in the audio a chosen visual should appear (distinct
    # from `keywords`, which are optimized for image-search relevance and
    # may not be substrings of the script at all, e.g. English search terms
    # for a Chinese script). Empty means "no anchor" — start of narration.
    script_anchor: str = ""

    def __post_init__(self) -> None:
        if self.slide_number < 1:
            raise TimelineValidationError("recommendation slide_number must be at least 1")
        if not 0 <= self.visual_need_score <= 100:
            raise TimelineValidationError("visual_need_score must be between 0 and 100")
        if self.recommended != (self.visual_need_score >= 61):
            raise TimelineValidationError(
                "recommended must be true exactly when visual_need_score is 61 or higher"
            )


@dataclass(frozen=True)
class TimelineBlock:
    """One narration block whose real audio duration drives the timeline.

    ``reading_pause_ms`` is visual hold/silence after narration.  It shifts
    later narration blocks but never extends this block's subtitle timings.
    """

    id: str
    block_type: TimelineBlockType
    script: str
    audio_path: str
    audio_duration_ms: int
    slide_number: int | None = None
    after_slide: int | None = None
    image_path: str | None = None
    reading_pause_ms: int = 0

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise TimelineValidationError("timeline block id must not be empty")
        if self.audio_duration_ms < 0 or self.reading_pause_ms < 0:
            raise TimelineValidationError("timeline durations must not be negative")
        if self.block_type is TimelineBlockType.SLIDE:
            if self.slide_number is None or self.slide_number < 1:
                raise TimelineValidationError("slide blocks require a positive slide_number")
            if self.after_slide is not None:
                raise TimelineValidationError("slide blocks must not define after_slide")
        elif self.block_type is TimelineBlockType.AI_INSERT:
            if self.after_slide is None or self.after_slide < 1:
                raise TimelineValidationError("AI insert blocks require a positive after_slide")
            if self.slide_number is not None:
                raise TimelineValidationError("AI insert blocks must not define slide_number")

    @property
    def visual_duration_ms(self) -> int:
        return self.audio_duration_ms + self.reading_pause_ms


@dataclass(frozen=True)
class ScheduledBlock:
    """A narration block placed on the global master timeline."""

    block: TimelineBlock
    start_ms: int
    narration_end_ms: int
    visual_end_ms: int

    def __post_init__(self) -> None:
        if not (0 <= self.start_ms <= self.narration_end_ms <= self.visual_end_ms):
            raise TimelineValidationError("scheduled block timestamps are not monotonic")
        if self.narration_end_ms - self.start_ms != self.block.audio_duration_ms:
            raise TimelineValidationError("scheduled narration span must match audio duration")
        if self.visual_end_ms - self.narration_end_ms != self.block.reading_pause_ms:
            raise TimelineValidationError("scheduled visual tail must match reading pause")


@dataclass(frozen=True)
class VisualEvent:
    """A visual-only event attached to a scheduled narration block."""

    id: str
    event_type: VisualEventType
    block_id: str
    start_ms: int
    end_ms: int
    slide_number: int | None = None
    asset: VisualAsset | None = None
    placement: VisualPlacement = VisualPlacement.AUTO
    user_confirmed: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.block_id.strip():
            raise TimelineValidationError("visual event id and block_id must not be empty")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise TimelineValidationError("visual event must have a positive time span")
        if self.event_type in (VisualEventType.BROLL, VisualEventType.OVERLAY) and self.asset is None:
            raise TimelineValidationError("B-roll and overlay events require an asset")


@dataclass(frozen=True)
class VisualTimeline:
    """Complete narration schedule plus optional, non-authoritative visuals."""

    blocks: tuple[ScheduledBlock, ...]
    visual_events: tuple[VisualEvent, ...] = ()

    @property
    def total_duration_ms(self) -> int:
        return self.blocks[-1].visual_end_ms if self.blocks else 0


@dataclass(frozen=True)
class TimelineEstimate:
    narration_ms: int
    ai_extension_ms: int
    reading_pause_ms: int
    transition_ms: int = 0
    intro_outro_ms: int = 0

    def __post_init__(self) -> None:
        if min(
            self.narration_ms,
            self.ai_extension_ms,
            self.reading_pause_ms,
            self.transition_ms,
            self.intro_outro_ms,
        ) < 0:
            raise TimelineValidationError("estimate values must not be negative")

    @property
    def total_ms(self) -> int:
        return (
            self.narration_ms
            + self.ai_extension_ms
            + self.reading_pause_ms
            + self.transition_ms
            + self.intro_outro_ms
        )
