import os

from ppt2course.avatar import (
    AvatarAssetSet,
    AvatarSegment,
    AvatarState,
    build_avatar_track,
    default_asset_set,
    should_show_avatar,
)
from ppt2course.subtitle import TimedChunk


def _chunk(text, start_ms, end_ms):
    return TimedChunk(text=text, start_ms=start_ms, end_ms=end_ms)


def test_build_avatar_track_empty_chunks_is_all_idle():
    segments = build_avatar_track([], 2000)
    assert segments == [AvatarSegment(AvatarState.IDLE, 0, 2000)]


def test_build_avatar_track_zero_duration_returns_empty():
    assert build_avatar_track([_chunk("hi", 0, 100)], 0) == []


def test_build_avatar_track_covers_full_duration_contiguously():
    chunks = [_chunk("Hello", 100, 500), _chunk("world", 500, 900)]
    segments = build_avatar_track(chunks, 1200)

    assert segments[0].start_ms == 0
    assert segments[-1].end_ms == 1200
    for a, b in zip(segments, segments[1:]):
        assert a.end_ms == b.start_ms


def test_build_avatar_track_idle_before_first_word():
    chunks = [_chunk("Hello", 300, 700)]
    segments = build_avatar_track(chunks, 1000)
    assert segments[0].state == AvatarState.IDLE
    assert segments[0].start_ms == 0
    assert segments[0].end_ms == 300


def test_build_avatar_track_alternates_open_close_within_a_speaking_run():
    chunks = [_chunk("a", 0, 200), _chunk("b", 200, 400), _chunk("c", 400, 600)]
    segments = build_avatar_track(chunks, 600, silence_gap_ms=350)
    states = [s.state for s in segments]
    assert states == [AvatarState.TALK_OPEN, AvatarState.TALK_CLOSE, AvatarState.TALK_OPEN]


def test_build_avatar_track_drops_to_idle_across_a_long_pause():
    chunks = [_chunk("a", 0, 200), _chunk("b", 900, 1100)]
    segments = build_avatar_track(chunks, 1200, silence_gap_ms=350)
    states = [(s.state, s.start_ms, s.end_ms) for s in segments]
    assert states == [
        (AvatarState.TALK_OPEN, 0, 200),
        (AvatarState.IDLE, 200, 900),
        (AvatarState.TALK_OPEN, 900, 1100),
        (AvatarState.IDLE, 1100, 1200),
    ]


def test_build_avatar_track_merges_a_short_gap_into_one_speaking_run():
    # 100ms gap, well under the default 350ms threshold: no idle segment
    # should appear between these two words.
    chunks = [_chunk("a", 0, 200), _chunk("b", 300, 500)]
    segments = build_avatar_track(chunks, 500, silence_gap_ms=350)
    assert all(s.state != AvatarState.IDLE for s in segments)


def test_build_avatar_track_ignores_empty_text_chunks():
    chunks = [_chunk("", 0, 0), _chunk("hi", 0, 300)]
    segments = build_avatar_track(chunks, 500)
    assert any(s.state == AvatarState.TALK_OPEN for s in segments)


def test_avatar_asset_set_falls_back_talk_close_point_wave_to_idle():
    assets = AvatarAssetSet(idle="idle.png", talk_open="open.png")
    assert assets.path_for(AvatarState.TALK_CLOSE) == "idle.png"
    assert assets.path_for(AvatarState.POINT) == "idle.png"
    assert assets.path_for(AvatarState.WAVE) == "idle.png"
    assert assets.path_for(AvatarState.IDLE) == "idle.png"
    assert assets.path_for(AvatarState.TALK_OPEN) == "open.png"


def test_default_asset_set_resolves_to_real_bundled_files():
    assets = default_asset_set()
    for path in (assets.idle, assets.talk_open, assets.talk_close, assets.point, assets.wave):
        assert os.path.isfile(path), path


def test_should_show_avatar_none_mode_always_false():
    assert should_show_avatar("none", 1, "some script") is False


def test_should_show_avatar_always_mode_true_even_for_empty_script():
    assert should_show_avatar("always", 1, "") is True


def test_should_show_avatar_keyframe_mode_requires_nonempty_script():
    assert should_show_avatar("keyframe", 1, "  ") is False
    assert should_show_avatar("keyframe", 1, "有內容") is True


def test_should_show_avatar_custom_mode_checks_slide_number_membership():
    assert should_show_avatar("custom", 2, "x", custom_slide_numbers=(1, 3)) is False
    assert should_show_avatar("custom", 3, "x", custom_slide_numbers=(1, 3)) is True
