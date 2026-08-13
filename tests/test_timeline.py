import pytest

from ppt2course.timeline import build_visual_timeline, estimate_timeline, schedule_blocks
from ppt2course.timeline_models import (
    TimelineBlock,
    TimelineBlockType,
    TimelineValidationError,
    VisualAsset,
    VisualAssetType,
    VisualEvent,
    VisualEventType,
)


def _slide(number: int, duration_ms: int, pause_ms: int = 0) -> TimelineBlock:
    return TimelineBlock(
        id=f"slide_{number}",
        block_type=TimelineBlockType.SLIDE,
        slide_number=number,
        script=f"script {number}",
        audio_path=f"slide_{number}.mp3",
        audio_duration_ms=duration_ms,
        reading_pause_ms=pause_ms,
    )


def _asset(slide_number: int = 1) -> VisualAsset:
    return VisualAsset(
        source="pexels",
        asset_type=VisualAssetType.IMAGE,
        preview_url="https://example.test/preview.jpg",
        download_url="https://example.test/full.jpg",
        keyword="employee isolation",
        slide_number=slide_number,
    )


def test_1_no_new_features_is_plain_sequential_narration():
    scheduled = schedule_blocks([_slide(1, 1500), _slide(2, 2300)])
    assert [(b.start_ms, b.narration_end_ms, b.visual_end_ms) for b in scheduled] == [
        (0, 1500, 1500),
        (1500, 3800, 3800),
    ]


def test_2_broll_does_not_change_audio_or_total_duration():
    blocks = [_slide(1, 5000), _slide(2, 3000)]
    baseline = build_visual_timeline(blocks)
    with_broll = build_visual_timeline(
        blocks,
        [
            VisualEvent(
                id="broll_1",
                event_type=VisualEventType.BROLL,
                block_id="slide_1",
                slide_number=1,
                asset=_asset(),
                start_ms=1000,
                end_ms=4000,
                user_confirmed=True,
            )
        ],
    )
    assert with_broll.blocks == baseline.blocks
    assert with_broll.total_duration_ms == baseline.total_duration_ms == 8000


def test_3_avatar_does_not_change_audio_or_total_duration():
    blocks = [_slide(1, 2000), _slide(2, 3000)]
    timeline = build_visual_timeline(
        blocks,
        [
            VisualEvent(
                id="avatar_1",
                event_type=VisualEventType.AVATAR,
                block_id="slide_1",
                slide_number=1,
                start_ms=0,
                end_ms=2000,
                user_confirmed=True,
            )
        ],
    )
    assert timeline.total_duration_ms == 5000


def test_4_reading_pause_extends_visual_tail_and_shifts_next_narration():
    scheduled = schedule_blocks([_slide(1, 2000, 500), _slide(2, 3000)])
    assert scheduled[0].narration_end_ms == 2000
    assert scheduled[0].visual_end_ms == 2500
    assert scheduled[1].start_ms == 2500
    assert scheduled[1].narration_end_ms == 5500


def test_5_confirmed_ai_extension_recalculates_following_timeline():
    insert = TimelineBlock(
        id="ai_insert_1",
        block_type=TimelineBlockType.AI_INSERT,
        after_slide=1,
        script="confirmed extension",
        audio_path="insert.mp3",
        audio_duration_ms=800,
        image_path="example.jpg",
    )
    scheduled = schedule_blocks([_slide(1, 2000), insert, _slide(2, 3000)])
    assert scheduled[1].start_ms == 2000
    assert scheduled[2].start_ms == 2800
    assert scheduled[-1].visual_end_ms == 5800


def test_visual_event_must_stay_inside_its_parent_block():
    with pytest.raises(TimelineValidationError, match="outside block"):
        build_visual_timeline(
            [_slide(1, 2000)],
            [
                VisualEvent(
                    id="broll_1",
                    event_type=VisualEventType.BROLL,
                    block_id="slide_1",
                    asset=_asset(),
                    start_ms=1000,
                    end_ms=2500,
                )
            ],
        )


def test_visual_event_cannot_reference_an_unknown_block():
    with pytest.raises(TimelineValidationError, match="unknown block"):
        build_visual_timeline(
            [_slide(1, 2000)],
            [
                VisualEvent(
                    id="avatar_1",
                    event_type=VisualEventType.AVATAR,
                    block_id="missing",
                    start_ms=100,
                    end_ms=500,
                )
            ],
        )


def test_estimate_reports_ai_extension_separately():
    insert = TimelineBlock(
        id="ai_insert_1",
        block_type=TimelineBlockType.AI_INSERT,
        after_slide=1,
        script="extra",
        audio_path="extra.mp3",
        audio_duration_ms=800,
    )
    estimate = estimate_timeline(
        [_slide(1, 2000, 500), insert, _slide(2, 3000)],
        transition_ms=200,
        intro_outro_ms=1000,
    )
    assert estimate.narration_ms == 5000
    assert estimate.ai_extension_ms == 800
    assert estimate.reading_pause_ms == 500
    assert estimate.total_ms == 7500


def test_duplicate_block_and_event_ids_are_rejected():
    with pytest.raises(TimelineValidationError, match="duplicate timeline block"):
        schedule_blocks([_slide(1, 1000), _slide(1, 1000)])

    event = VisualEvent(
        id="avatar_1",
        event_type=VisualEventType.AVATAR,
        block_id="slide_1",
        start_ms=0,
        end_ms=500,
    )
    with pytest.raises(TimelineValidationError, match="duplicate visual event"):
        build_visual_timeline([_slide(1, 1000)], [event, event])
