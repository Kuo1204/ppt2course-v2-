import pytest

from ppt2course.timeline_models import (
    TimelineBlock,
    TimelineBlockType,
    TimelineValidationError,
    VisualAsset,
    VisualAssetType,
    VisualRecommendation,
)


def test_slide_block_visual_duration_is_audio_plus_reading_pause():
    block = TimelineBlock(
        id="slide_1",
        block_type=TimelineBlockType.SLIDE,
        slide_number=1,
        script="hello",
        audio_path="slide_1.mp3",
        audio_duration_ms=5000,
        reading_pause_ms=1000,
    )
    assert block.visual_duration_ms == 6000


def test_ai_insert_requires_after_slide_and_never_claims_a_slide_number():
    with pytest.raises(TimelineValidationError, match="after_slide"):
        TimelineBlock(
            id="insert_1",
            block_type=TimelineBlockType.AI_INSERT,
            script="extra",
            audio_path="insert.mp3",
            audio_duration_ms=800,
        )


def test_negative_duration_is_rejected():
    with pytest.raises(TimelineValidationError, match="negative"):
        TimelineBlock(
            id="slide_1",
            block_type=TimelineBlockType.SLIDE,
            slide_number=1,
            script="",
            audio_path="silent.mp3",
            audio_duration_ms=-1,
        )


def test_recommendation_threshold_is_a_model_invariant():
    recommendation = VisualRecommendation(
        slide_number=5,
        title="Isolation",
        visual_need_score=82,
        recommended=True,
        reason="Abstract workplace scenario",
        visual_type=VisualAssetType.IMAGE,
        keywords=("workplace isolation", "employee stress"),
    )
    assert recommendation.recommended is True

    with pytest.raises(TimelineValidationError, match="recommended"):
        VisualRecommendation(
            slide_number=2,
            title="Summary",
            visual_need_score=20,
            recommended=True,
            reason="Already visual",
            visual_type=VisualAssetType.IMAGE,
        )


def test_visual_asset_uses_normalized_provider_fields():
    asset = VisualAsset(
        source="pexels",
        asset_type=VisualAssetType.IMAGE,
        preview_url="https://example.test/preview.jpg",
        download_url="https://example.test/full.jpg",
        photographer="Photographer",
        keyword="workplace isolation",
        slide_number=5,
    )
    assert asset.source == "pexels"
    assert asset.local_path is None
