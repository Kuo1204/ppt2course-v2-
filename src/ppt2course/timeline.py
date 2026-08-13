"""Pure construction and validation of narration-led visual timelines."""

from collections.abc import Iterable

from ppt2course.timeline_models import (
    ScheduledBlock,
    TimelineBlock,
    TimelineBlockType,
    TimelineEstimate,
    TimelineValidationError,
    VisualEvent,
    VisualTimeline,
)


def schedule_blocks(blocks: Iterable[TimelineBlock]) -> tuple[ScheduledBlock, ...]:
    """Place narration blocks sequentially using real audio durations.

    Reading pause follows a block's narration and shifts every later block.
    Visual assets are intentionally absent from this calculation.
    """
    scheduled: list[ScheduledBlock] = []
    cursor_ms = 0
    seen_ids: set[str] = set()

    for block in blocks:
        if block.id in seen_ids:
            raise TimelineValidationError(f"duplicate timeline block id: {block.id!r}")
        seen_ids.add(block.id)
        narration_end_ms = cursor_ms + block.audio_duration_ms
        visual_end_ms = narration_end_ms + block.reading_pause_ms
        scheduled.append(
            ScheduledBlock(
                block=block,
                start_ms=cursor_ms,
                narration_end_ms=narration_end_ms,
                visual_end_ms=visual_end_ms,
            )
        )
        cursor_ms = visual_end_ms

    return tuple(scheduled)


def build_visual_timeline(
    blocks: Iterable[TimelineBlock], visual_events: Iterable[VisualEvent] = ()
) -> VisualTimeline:
    """Build and validate a timeline without allowing visuals to change it."""
    scheduled = schedule_blocks(blocks)
    scheduled_by_id = {item.block.id: item for item in scheduled}
    events = tuple(visual_events)
    seen_event_ids: set[str] = set()

    for event in events:
        if event.id in seen_event_ids:
            raise TimelineValidationError(f"duplicate visual event id: {event.id!r}")
        seen_event_ids.add(event.id)
        parent = scheduled_by_id.get(event.block_id)
        if parent is None:
            raise TimelineValidationError(
                f"visual event {event.id!r} references unknown block {event.block_id!r}"
            )
        if event.start_ms < parent.start_ms or event.end_ms > parent.visual_end_ms:
            raise TimelineValidationError(
                f"visual event {event.id!r} falls outside block {event.block_id!r}"
            )
        if event.slide_number is not None and parent.block.slide_number is not None:
            if event.slide_number != parent.block.slide_number:
                raise TimelineValidationError(
                    f"visual event {event.id!r} slide_number does not match its block"
                )

    return VisualTimeline(blocks=scheduled, visual_events=events)


def estimate_timeline(
    blocks: Iterable[TimelineBlock], transition_ms: int = 0, intro_outro_ms: int = 0
) -> TimelineEstimate:
    """Return the UI-facing duration breakdown for narration blocks."""
    block_list = tuple(blocks)
    if transition_ms < 0 or intro_outro_ms < 0:
        raise TimelineValidationError("estimate additions must not be negative")

    ai_extension_ms = sum(
        block.audio_duration_ms
        for block in block_list
        if block.block_type is TimelineBlockType.AI_INSERT
    )
    narration_ms = sum(
        block.audio_duration_ms
        for block in block_list
        if block.block_type is TimelineBlockType.SLIDE
    )
    reading_pause_ms = sum(block.reading_pause_ms for block in block_list)
    return TimelineEstimate(
        narration_ms=narration_ms,
        ai_extension_ms=ai_extension_ms,
        reading_pause_ms=reading_pause_ms,
        transition_ms=transition_ms,
        intro_outro_ms=intro_outro_ms,
    )
