from unittest.mock import patch

import pytest

from ppt2course.subtitle import TimedChunk
from ppt2course.video import (
    MIN_SLIDE_HOLD_MS,
    AvatarOverlay,
    BrollOverlay,
    SlideVideoInput,
    VideoComposeError,
    _add_logo_overlay,
    _audio_acrossfade_chain,
    _audio_concat_chain,
    _audio_label_filter,
    _build_cues,
    _build_ffmpeg_command,
    _compute_slide_offsets,
    _concatenate_with_intro_outro,
    _effective_transition_ms,
    _ken_burns_filter,
    _mix_background_music,
    _scale_pad_filter,
    _total_duration_ms,
    _video_xfade_chain,
    compose_video,
)


# ---- _compute_slide_offsets ----

def test_compute_slide_offsets_empty():
    assert _compute_slide_offsets([], 500) == []


def test_compute_slide_offsets_single_slide():
    assert _compute_slide_offsets([5000], 500) == [0]


def test_compute_slide_offsets_two_slides():
    assert _compute_slide_offsets([5000, 3000], 500) == [0, 4500]


def test_compute_slide_offsets_three_slides():
    assert _compute_slide_offsets([5000, 3000, 4000], 500) == [0, 4500, 7000]


def test_compute_slide_offsets_zero_transition():
    assert _compute_slide_offsets([1000, 1000], 0) == [0, 1000]


# ---- _effective_transition_ms ----
# A slide's own audio duration limits how long any transition touching it
# can be — a crossfade longer than the clip it's crossfading breaks the
# cumulative-offset math (_compute_slide_offsets assumes each slide adds
# durations_ms[i] - transition_ms of new timeline; if the transition is >=
# a slide's duration, that delta goes to zero or negative, which is exactly
# what produced the reported "subtitles drift out of sync / audio overlaps"
# bug when the transition slider was maxed out next to a short slide).


def test_effective_transition_ms_returns_request_unchanged_when_it_fits():
    assert _effective_transition_ms([5000, 5000], 500) == 500


def test_effective_transition_ms_clamps_to_the_shorter_of_two_slides():
    # budget is durations[1] (800ms) minus the safety margin
    assert _effective_transition_ms([5000, 800], 2000) == 800 - MIN_SLIDE_HOLD_MS


def test_effective_transition_ms_halves_an_interior_slides_budget():
    # the middle slide has both an incoming and an outgoing transition to
    # pay for out of its own 400ms, so each side gets at most half
    assert _effective_transition_ms([5000, 400, 5000], 2000) == 200 - MIN_SLIDE_HOLD_MS


def test_effective_transition_ms_never_goes_negative():
    assert _effective_transition_ms([5000, 50, 5000], 2000) == 0


def test_effective_transition_ms_single_slide_passes_through_unchanged():
    assert _effective_transition_ms([5000], 2000) == 2000


def test_effective_transition_ms_empty_passes_through_unchanged():
    assert _effective_transition_ms([], 2000) == 2000


def test_effective_transition_ms_first_and_last_slide_only_pay_once():
    # first/last slides only have one neighboring transition, not two, so
    # they get their full duration as budget rather than half of it
    assert _effective_transition_ms([1000, 5000, 1000], 2000) == 1000 - MIN_SLIDE_HOLD_MS


# ---- _total_duration_ms ----

def test_total_duration_ms_empty():
    assert _total_duration_ms([], 500) == 0


def test_total_duration_ms_three_slides():
    assert _total_duration_ms([5000, 3000, 4000], 500) == 11000


# ---- _build_cues (cross-slide reconciliation) ----

def _slide(chunks):
    return SlideVideoInput(image_path="img.png", audio_path="audio.mp3", chunks=chunks)


def test_build_cues_no_overlap_no_delay():
    slide1 = _slide([TimedChunk("你好", 0, 800), TimedChunk("。", 800, 800)])
    slide2 = _slide([TimedChunk("謝謝", 0, 700), TimedChunk("。", 700, 700)])
    # durations chosen so offset2 (=durations[0]-transition=2000-500=1500) is
    # well after slide1's own cue naturally ends (800ms) -> no overlap.
    cues = _build_cues([slide1, slide2], durations_ms=[2000, 1000], transition_ms=500)

    assert [(c.index, c.start_ms, c.end_ms, c.text) for c in cues] == [
        (1, 0, 800, "你好"),
        (2, 1500, 2200, "謝謝"),
    ]


def test_build_cues_overlap_delays_next_slide_without_truncating_previous():
    slide1 = _slide([TimedChunk("你好", 0, 1000), TimedChunk("。", 1000, 1000)])
    slide2 = _slide([TimedChunk("謝謝", 0, 700), TimedChunk("。", 700, 700)])
    # offset2 = durations[0]-transition = 1000-500 = 500, which lands inside
    # slide1's own cue (0-1000) -> slide2's cues must be delayed to start at
    # 1000+100=1100, not truncate slide1's cue.
    cues = _build_cues([slide1, slide2], durations_ms=[1000, 900], transition_ms=500)

    assert [(c.index, c.start_ms, c.end_ms, c.text) for c in cues] == [
        (1, 0, 1000, "你好"),
        (2, 1100, 1800, "謝謝"),
    ]


class _FakeWordSegmenter:
    def __init__(self, spans):
        self._spans = spans

    def word_spans(self, text):
        return self._spans


def test_build_cues_uses_word_segmenter_to_protect_a_span_from_a_hard_cut():
    # No punctuation at all -> the default-max_chars hard cut lands at
    # position 18, right in the middle of the term below (span 15-20),
    # unless a word_segmenter reports it as a span to protect.
    text = "今天要來跟大家介紹一下這個主題普拉斯提亞雲端系統效能非常優異值得推薦給大家使用"
    term = "普拉斯提亞"
    idx = text.index(term)
    chunks = [TimedChunk(ch, i * 50, i * 50 + 50) for i, ch in enumerate(text)]
    slide = _slide(chunks)
    durations = [len(text) * 50]

    cues_without = _build_cues([slide], durations, transition_ms=0)
    assert not any(term in c.text for c in cues_without)

    segmenter = _FakeWordSegmenter([(idx, idx + len(term))])
    cues_with = _build_cues([slide], durations, transition_ms=0, word_segmenter=segmenter)
    assert any(term in c.text for c in cues_with)


def test_build_cues_delay_propagates_to_third_slide():
    slide1 = _slide([TimedChunk("一", 0, 1000), TimedChunk("。", 1000, 1000)])
    slide2 = _slide([TimedChunk("二", 0, 200), TimedChunk("。", 200, 200)])
    slide3 = _slide([TimedChunk("三", 0, 500), TimedChunk("。", 500, 500)])
    # slide2 offset = 1000-500=500 -> overlaps slide1 (ends 1000) -> delayed
    # by 600 to start at 1100, its own cue ends at 1100+200=1300.
    # slide3 offset = offsets[1]+durations[1]-transition = 500+700-500=700,
    # which is before slide2's now-delayed end (1300) -> slide3 also delayed.
    cues = _build_cues(
        [slide1, slide2, slide3], durations_ms=[1000, 700, 600], transition_ms=500
    )

    assert [(c.index, c.start_ms, c.end_ms, c.text) for c in cues] == [
        (1, 0, 1000, "一"),
        (2, 1100, 1300, "二"),
        (3, 1400, 1900, "三"),
    ]


# ---- filter_complex construction ----

def test_scale_pad_filter():
    assert _scale_pad_filter(0, 1920, 1080, 30) == (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v0]"
    )


def test_video_xfade_chain_single_slide_no_xfade():
    filt, label = _video_xfade_chain(1, "fade", 500, [0])
    assert filt == ""
    assert label == "v0"


def test_video_xfade_chain_two_slides():
    filt, label = _video_xfade_chain(2, "fade", 500, [0, 4500])
    assert filt == "[v0][v1]xfade=transition=fade:duration=0.5:offset=4.5[vout]"
    assert label == "vout"


def test_video_xfade_chain_three_slides():
    filt, label = _video_xfade_chain(3, "fade", 500, [0, 4500, 7000])
    assert filt == (
        "[v0][v1]xfade=transition=fade:duration=0.5:offset=4.5[vx1];"
        "[vx1][v2]xfade=transition=fade:duration=0.5:offset=7.0[vout]"
    )
    assert label == "vout"


def test_audio_acrossfade_chain_single_slide():
    filt, label = _audio_acrossfade_chain(1, 500)
    assert filt == ""
    assert label == "a0"


def test_audio_acrossfade_chain_three_slides():
    filt, label = _audio_acrossfade_chain(3, 500)
    assert filt == (
        "[a0][a1]acrossfade=d=0.5:c1=tri:c2=tri[ax1];"
        "[ax1][a2]acrossfade=d=0.5:c1=tri:c2=tri[aout]"
    )
    assert label == "aout"


def test_build_ffmpeg_command_two_slides_structure():
    slide1 = _slide([TimedChunk("你好", 0, 800)])
    slide2 = _slide([TimedChunk("謝謝", 0, 700)])
    cmd = _build_ffmpeg_command(
        [slide1, slide2],
        durations_ms=[2000, 1000],
        offsets_ms=[0, 1500],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=48,
    )

    assert cmd[:2] == ["ffmpeg", "-y"]
    assert cmd.count("-loop") == 2
    assert cmd[cmd.index("-i") + 1] == "img.png"
    assert "audio.mp3" in cmd
    assert "-filter_complex" in cmd
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "xfade=transition=fade:duration=0.5:offset=1.5[vout]" in filter_complex
    assert "acrossfade=d=0.5:c1=tri:c2=tri[aout]" in filter_complex
    assert "subtitles=" in filter_complex
    assert cmd[-1] == "out.mp4"
    assert "-map" in cmd

    # WrapStyle=2 disables libass's automatic line-wrap for cues that don't
    # fit the frame width — without it, a single-line SRT cue still gets
    # visually split across two lines by the renderer at small resolutions
    # or large font sizes, defeating the "always one line" cue design.
    assert "WrapStyle=2" in filter_complex

    # Plain SRT has no PlayResX/PlayResY, so libass falls back to a 384x288
    # script canvas and scales FontSize up to fill the real frame (~5x at
    # 1920x1080) unless told the actual output resolution — inflating
    # rendered subtitles well past what font_size configured and overflowing
    # long cues off both edges of the frame. PlayResX/PlayResY overrides via
    # force_style are what actually fixes this (original_size measurably
    # does not, verified against a real ffmpeg render).
    assert "PlayResX=1920" in filter_complex
    assert "PlayResY=1080" in filter_complex

    # Without an explicit codec/pixel format, ffmpeg picks whatever the filter
    # graph naturally produces (often yuv444p after xfade/subtitles), which
    # many standard players — including Windows' built-in ones — can't decode.
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "+faststart" in cmd[cmd.index("-movflags") + 1]


def test_build_ffmpeg_command_playres_tracks_resolution():
    slide = _slide([TimedChunk("你好", 0, 800)])
    cmd = _build_ffmpeg_command(
        [slide],
        durations_ms=[2000],
        offsets_ms=[0],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1280, 720),
        fps=30,
        font_size=22,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "PlayResX=1280" in filter_complex
    assert "PlayResY=720" in filter_complex


def test_build_ffmpeg_command_default_subtitle_margin_v_unchanged():
    # Locks in the pre-existing look for anyone not touching the new control.
    slide = _slide([TimedChunk("你好", 0, 800)])
    cmd = _build_ffmpeg_command(
        [slide],
        durations_ms=[2000],
        offsets_ms=[0],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=22,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "MarginV=30" in filter_complex


def test_build_ffmpeg_command_custom_subtitle_margin_v():
    slide = _slide([TimedChunk("你好", 0, 800)])
    cmd = _build_ffmpeg_command(
        [slide],
        durations_ms=[2000],
        offsets_ms=[0],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=22,
        subtitle_margin_v=180,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "MarginV=180" in filter_complex
    assert "MarginV=30" not in filter_complex


# ---- BrollOverlay / B-roll filter construction ----
# The core promise: B-roll is a picture swap only. It must never appear in
# _compute_slide_offsets, _build_cues, or durations_ms — those functions
# don't even take a SlideVideoInput with broll_overlays into account, so
# these tests mostly prove the *wiring* (inputs/filters) is correct, while
# the "audio/subtitles never move" guarantee is structural (see also the
# real-ffmpeg regression test in test_video_integration.py).


def test_broll_overlay_rejects_end_before_start():
    with pytest.raises(VideoComposeError):
        BrollOverlay(image_path="b.jpg", start_ms=500, end_ms=500)


def test_broll_overlay_rejects_negative_start():
    with pytest.raises(VideoComposeError):
        BrollOverlay(image_path="b.jpg", start_ms=-1, end_ms=100)


def test_build_ffmpeg_command_with_broll_adds_extra_input_after_all_slides():
    slide1 = SlideVideoInput(
        image_path="s1.png",
        audio_path="a1.mp3",
        chunks=[TimedChunk("你好", 0, 800)],
        broll_overlays=(BrollOverlay(image_path="broll.jpg", start_ms=200, end_ms=600),),
    )
    slide2 = _slide([TimedChunk("謝謝", 0, 700)])
    cmd = _build_ffmpeg_command(
        [slide1, slide2],
        durations_ms=[2000, 1000],
        offsets_ms=[0, 1500],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
    )

    # Inputs: [0]=slide1 img, [1]=slide2 img, [2]=slide1 audio, [3]=slide2
    # audio, [4]=broll — the broll is appended last, so slide/audio indices
    # are exactly what they'd be with zero B-roll.
    inputs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]
    assert inputs[:4] == ["s1.png", "img.png", "a1.mp3", "audio.mp3"]
    assert inputs[4] == "broll.jpg"

    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v0base]" in filter_complex
    assert "[4:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,fps=30[v0broll0]" in filter_complex
    assert "[v0base][v0broll0]overlay=enable='between(t,0.2,0.6)'[v0]" in filter_complex
    # Downstream xfade must reference plain v0/v1 — unaware B-roll exists.
    assert "[v0][v1]xfade=transition=fade:duration=0.5:offset=1.5[vout]" in filter_complex


def test_build_ffmpeg_command_with_broll_chains_multiple_overlays_on_one_slide():
    slide = SlideVideoInput(
        image_path="s1.png",
        audio_path="a1.mp3",
        chunks=[TimedChunk("你好", 0, 800)],
        broll_overlays=(
            BrollOverlay(image_path="b1.jpg", start_ms=100, end_ms=300),
            BrollOverlay(image_path="b2.jpg", start_ms=400, end_ms=600),
        ),
    )
    cmd = _build_ffmpeg_command(
        [slide],
        durations_ms=[1000],
        offsets_ms=[0],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "[v0base][v0broll0]overlay=enable='between(t,0.1,0.3)'[v0mix0]" in filter_complex
    assert "[v0mix0][v0broll1]overlay=enable='between(t,0.4,0.6)'[v0]" in filter_complex


def test_build_ffmpeg_command_without_broll_is_unchanged():
    # Same slide count/content as the very first structural test, just
    # re-asserted here as an explicit "no B-roll -> identical command"
    # regression guard next to the new B-roll tests.
    slide1 = _slide([TimedChunk("你好", 0, 800)])
    slide2 = _slide([TimedChunk("謝謝", 0, 700)])
    cmd = _build_ffmpeg_command(
        [slide1, slide2],
        durations_ms=[2000, 1000],
        offsets_ms=[0, 1500],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "v0base" not in filter_complex
    assert "broll" not in filter_complex


def test_compose_video_rejects_broll_overlay_past_slides_own_audio_duration(tmp_path):
    slide = SlideVideoInput(
        image_path="s1.png",
        audio_path="a1.mp3",
        chunks=[TimedChunk("你好", 0, 800)],
        broll_overlays=(BrollOverlay(image_path="b.jpg", start_ms=500, end_ms=1500),),
    )
    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch("ppt2course.video.get_audio_duration_ms", return_value=1000):
            with pytest.raises(VideoComposeError):
                compose_video([slide], str(tmp_path / "out.mp4"), str(tmp_path / "out.srt"))


def test_compose_video_with_broll_produces_identical_srt_to_without_broll(tmp_path):
    # The single most important B-roll regression: adding a picture swap
    # must not move a single subtitle timestamp.
    chunks = [TimedChunk("你好", 0, 800), TimedChunk("。", 800, 800)]

    class FakeResult:
        returncode = 0
        stderr = ""

    def _run(with_broll: bool, srt_path):
        broll_overlays = (
            (BrollOverlay(image_path="b.jpg", start_ms=100, end_ms=400),) if with_broll else ()
        )
        slide = SlideVideoInput(
            image_path="s1.png", audio_path="a1.mp3", chunks=chunks, broll_overlays=broll_overlays
        )
        with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
            with patch("ppt2course.video.get_audio_duration_ms", return_value=1600):
                with patch("ppt2course.video.subprocess.run", return_value=FakeResult()):
                    compose_video([slide], str(tmp_path / "out.mp4"), str(srt_path))
        return srt_path.read_text(encoding="utf-8")

    srt_without = _run(False, tmp_path / "without.srt")
    srt_with = _run(True, tmp_path / "with.srt")

    assert srt_without == srt_with


# ---- AvatarOverlay ----

def test_avatar_overlay_rejects_end_before_start():
    with pytest.raises(VideoComposeError):
        AvatarOverlay(image_path="a.png", start_ms=500, end_ms=500)


def test_avatar_overlay_rejects_negative_start():
    with pytest.raises(VideoComposeError):
        AvatarOverlay(image_path="a.png", start_ms=-1, end_ms=100)


def test_build_ffmpeg_command_with_avatar_adds_extra_input_after_all_slides():
    slide1 = SlideVideoInput(
        image_path="s1.png",
        audio_path="a1.mp3",
        chunks=[TimedChunk("你好", 0, 800)],
        avatar_overlays=(AvatarOverlay(image_path="idle.png", start_ms=0, end_ms=2000),),
    )
    slide2 = _slide([TimedChunk("謝謝", 0, 700)])
    cmd = _build_ffmpeg_command(
        [slide1, slide2],
        durations_ms=[2000, 1000],
        offsets_ms=[0, 1500],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
    )

    inputs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]
    assert inputs[:4] == ["s1.png", "img.png", "a1.mp3", "audio.mp3"]
    assert inputs[4] == "idle.png"

    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    # "small" (default) at 1080p height -> int(1080 * 0.28) = 302.
    assert "[4:v]format=rgba,scale=-2:302:flags=lanczos,fps=30[v0avatar0]" in filter_complex
    assert (
        "[v0base][v0avatar0]overlay=W-w-24:H-h-24:enable='between(t,0.0,2.0)'[v0]"
        in filter_complex
    )
    # Downstream xfade must still reference plain v0/v1.
    assert "[v0][v1]xfade=transition=fade:duration=0.5:offset=1.5[vout]" in filter_complex


def test_build_ffmpeg_command_avatar_position_and_size_are_configurable():
    slide = SlideVideoInput(
        image_path="s1.png",
        audio_path="a1.mp3",
        chunks=[TimedChunk("你好", 0, 800)],
        avatar_overlays=(AvatarOverlay(image_path="idle.png", start_ms=0, end_ms=1000),),
    )
    cmd = _build_ffmpeg_command(
        [slide],
        durations_ms=[1000],
        offsets_ms=[0],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
        avatar_position="left",
        avatar_size="large",
        avatar_margin=10,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=-2:594:flags=lanczos" in filter_complex  # int(1080 * 0.55)
    assert "overlay=10:(H-h)/2:enable=" in filter_complex


def test_build_ffmpeg_command_avatar_layers_on_top_of_broll_on_the_same_slide():
    slide = SlideVideoInput(
        image_path="s1.png",
        audio_path="a1.mp3",
        chunks=[TimedChunk("你好", 0, 800)],
        broll_overlays=(BrollOverlay(image_path="b.jpg", start_ms=100, end_ms=300),),
        avatar_overlays=(AvatarOverlay(image_path="idle.png", start_ms=0, end_ms=1000),),
    )
    cmd = _build_ffmpeg_command(
        [slide],
        durations_ms=[1000],
        offsets_ms=[0],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    # B-roll chain ends at an intermediate label (not v0, since avatar comes
    # after it), and the avatar chain is the one that finally produces v0.
    assert "[v0base][v0broll0]overlay=enable='between(t,0.1,0.3)'[v0brolled]" in filter_complex
    assert "[v0brolled][v0avatar0]overlay=" in filter_complex
    assert "[v0]" in filter_complex.split("[v0brolled][v0avatar0]overlay=")[1][:80]


def test_build_ffmpeg_command_without_avatar_is_unchanged():
    slide1 = _slide([TimedChunk("你好", 0, 800)])
    slide2 = _slide([TimedChunk("謝謝", 0, 700)])
    cmd = _build_ffmpeg_command(
        [slide1, slide2],
        durations_ms=[2000, 1000],
        offsets_ms=[0, 1500],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "avatar" not in filter_complex


def test_compose_video_rejects_avatar_overlay_past_slides_own_audio_duration(tmp_path):
    slide = SlideVideoInput(
        image_path="s1.png",
        audio_path="a1.mp3",
        chunks=[TimedChunk("你好", 0, 800)],
        avatar_overlays=(AvatarOverlay(image_path="idle.png", start_ms=500, end_ms=1500),),
    )
    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch("ppt2course.video.get_audio_duration_ms", return_value=1000):
            with pytest.raises(VideoComposeError):
                compose_video([slide], str(tmp_path / "out.mp4"), str(tmp_path / "out.srt"))


def test_compose_video_with_avatar_produces_identical_srt_to_without_avatar(tmp_path):
    chunks = [TimedChunk("你好", 0, 800), TimedChunk("。", 800, 800)]

    class FakeResult:
        returncode = 0
        stderr = ""

    def _run(with_avatar: bool, srt_path):
        avatar_overlays = (
            (AvatarOverlay(image_path="idle.png", start_ms=0, end_ms=1600),)
            if with_avatar
            else ()
        )
        slide = SlideVideoInput(
            image_path="s1.png", audio_path="a1.mp3", chunks=chunks, avatar_overlays=avatar_overlays
        )
        with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
            with patch("ppt2course.video.get_audio_duration_ms", return_value=1600):
                with patch("ppt2course.video.subprocess.run", return_value=FakeResult()):
                    compose_video([slide], str(tmp_path / "out.mp4"), str(srt_path))
        return srt_path.read_text(encoding="utf-8")

    srt_without = _run(False, tmp_path / "without.srt")
    srt_with = _run(True, tmp_path / "with.srt")

    assert srt_without == srt_with


# ---- avoid_voice_overlap (audio concat instead of acrossfade) ----

def test_audio_concat_chain_single_slide():
    filt, label = _audio_concat_chain(1)
    assert filt == ""
    assert label == "a0"


def test_audio_concat_chain_two_slides():
    filt, label = _audio_concat_chain(2)
    assert filt == "[a0][a1]concat=n=2:v=0:a=1[aout]"
    assert label == "aout"


def test_audio_concat_chain_three_slides():
    filt, label = _audio_concat_chain(3)
    assert filt == "[a0][a1][a2]concat=n=3:v=0:a=1[aout]"
    assert label == "aout"


def test_build_ffmpeg_command_avoid_voice_overlap_uses_concat_not_acrossfade():
    slide1 = _slide([TimedChunk("你好", 0, 800)])
    slide2 = _slide([TimedChunk("謝謝", 0, 700)])
    cmd = _build_ffmpeg_command(
        [slide1, slide2],
        durations_ms=[2000, 1500],
        offsets_ms=[0, 1500],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
        avoid_voice_overlap=True,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "[a0][a1]concat=n=2:v=0:a=1[aout]" in filter_complex
    assert "acrossfade" not in filter_complex
    # Video crossfade itself is untouched -- still uses the (unextended by
    # this test's caller) offsets/duration it was given.
    assert "[v0][v1]xfade=transition=fade:duration=0.5:offset=1.5[vout]" in filter_complex


def test_build_ffmpeg_command_avoid_voice_overlap_false_is_unchanged():
    slide1 = _slide([TimedChunk("你好", 0, 800)])
    slide2 = _slide([TimedChunk("謝謝", 0, 700)])
    cmd = _build_ffmpeg_command(
        [slide1, slide2],
        durations_ms=[2000, 1000],
        offsets_ms=[0, 1500],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2:v=0:a=1" not in filter_complex
    assert "acrossfade" in filter_complex


def test_build_ffmpeg_command_avoid_voice_overlap_pads_audio_to_separate_target():
    # audio_pad_durations_ms lets compose_video give a slide's own apad
    # target separately from the (possibly last-slide-extended) video hold
    # time it also passes as durations_ms.
    slide = SlideVideoInput(
        image_path="s1.png", audio_path="a1.mp3", chunks=[TimedChunk("x", 0, 800)],
        reading_pause_ms=500,
    )
    cmd = _build_ffmpeg_command(
        [slide, _slide([TimedChunk("y", 0, 700)])],
        durations_ms=[2500, 5000],  # last slide's video hold artificially stretched
        offsets_ms=[0, 2000],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
        avoid_voice_overlap=True,
        audio_pad_durations_ms=[2500, 1500],  # slide0's real apad target
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "[2:a]apad=whole_dur=2.5[a0]" in filter_complex
    # slide1 has no reading pause -> anull regardless of either duration list.
    assert "[3:a]anull[a1]" in filter_complex
    # But the *video* hold time (image -t) for each slide is what's used for
    # its own on-screen duration -- slide0's unextended, slide1's extended.
    ts_values = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-t"]
    assert ts_values[:2] == ["2.5", "5.0"]


def test_compose_video_avoid_voice_overlap_extends_last_slide_and_uses_pure_concat_offsets(
    tmp_path,
):
    chunks1 = [TimedChunk("你好", 0, 800)]
    chunks2 = [TimedChunk("謝謝", 0, 700)]
    slide1 = SlideVideoInput(image_path="s1.png", audio_path="a1.mp3", chunks=chunks1)
    slide2 = SlideVideoInput(image_path="s2.png", audio_path="a2.mp3", chunks=chunks2)

    class FakeResult:
        returncode = 0
        stderr = ""

    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch(
            "ppt2course.video.get_audio_duration_ms", side_effect=[2000, 1000]
        ):
            with patch(
                "ppt2course.video.subprocess.run", return_value=FakeResult()
            ) as mock_run:
                compose_video(
                    [slide1, slide2],
                    str(tmp_path / "out.mp4"),
                    str(tmp_path / "out.srt"),
                    transition_duration_ms=500,
                    avoid_voice_overlap=True,
                )

    cmd = mock_run.call_args[0][0]
    # durations_ms=[2000,1000], transition=500 -> compressed total (offsets
    # [0,1500] + last 1000) = 2500; uncompressed (pure concat) total
    # (offsets [0,2000] + last 1000) = 3000 -> last slide's video hold time
    # is stretched by exactly that 500ms difference: 1000 -> 1500.
    ts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-t"]
    assert ts[:2] == ["2.0", "1.5"]  # slide0 unchanged, slide1 stretched by 500ms

    srt_text = (tmp_path / "out.srt").read_text(encoding="utf-8")
    # Pure-concat cue offset for slide2: exactly slide1's own 2000ms
    # duration, not the crossfade-compressed 1500ms.
    assert "00:00:02,000" in srt_text


# ---- compose_video (mocked subprocess) ----

def test_compose_video_raises_on_empty_slides():
    with pytest.raises(VideoComposeError):
        compose_video([], "out.mp4", "out.srt")


def test_compose_video_raises_when_ffmpeg_not_found():
    slide = _slide([TimedChunk("你好", 0, 800)])
    with patch("ppt2course.video.shutil.which", return_value=None):
        with pytest.raises(VideoComposeError):
            compose_video([slide], "out.mp4", "out.srt")


def test_compose_video_raises_on_nonzero_ffmpeg_exit(tmp_path):
    slide = _slide([TimedChunk("你好", 0, 800)])

    class FakeResult:
        returncode = 1
        stderr = "boom"

    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch("ppt2course.video.get_audio_duration_ms", return_value=1000):
            with patch("ppt2course.video.subprocess.run", return_value=FakeResult()):
                with pytest.raises(VideoComposeError):
                    compose_video(
                        [slide],
                        str(tmp_path / "out.mp4"),
                        str(tmp_path / "out.srt"),
                    )


def test_compose_video_forwards_custom_dict_path_to_protect_srt_line_breaks(tmp_path):
    # Real jieba, no mocking. No punctuation at all -> the default hard cut
    # at position 18 lands inside this invented term (span 15-20) unless a
    # custom dictionary teaches jieba to treat it as one word.
    text = "今天要來跟大家介紹一下這個主題普拉斯提亞雲端系統效能非常優異值得推薦給大家使用"
    term = "普拉斯提亞"
    chunks = [TimedChunk(ch, i * 50, i * 50 + 50) for i, ch in enumerate(text)]
    slide = _slide(chunks)

    dict_path = tmp_path / "custom_dict.txt"
    dict_path.write_text(f"{term} 100\n", encoding="utf-8")

    srt_path = tmp_path / "out.srt"

    class FakeResult:
        returncode = 0
        stderr = ""

    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch("ppt2course.video.get_audio_duration_ms", return_value=len(text) * 50):
            with patch("ppt2course.video.subprocess.run", return_value=FakeResult()):
                compose_video(
                    [slide],
                    str(tmp_path / "out.mp4"),
                    str(srt_path),
                    custom_dict_path=str(dict_path),
                )

    srt_content = srt_path.read_text(encoding="utf-8")
    assert term in srt_content


def test_compose_video_writes_srt_and_calls_ffmpeg(tmp_path):
    slide1 = _slide([TimedChunk("你好", 0, 800), TimedChunk("。", 800, 800)])
    slide2 = _slide([TimedChunk("謝謝", 0, 700), TimedChunk("。", 700, 700)])

    class FakeResult:
        returncode = 0
        stderr = ""

    srt_path = tmp_path / "out.srt"
    video_path = tmp_path / "out.mp4"

    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch(
            "ppt2course.video.get_audio_duration_ms", side_effect=[2000, 1000]
        ):
            with patch(
                "ppt2course.video.subprocess.run", return_value=FakeResult()
            ) as mock_run:
                compose_video([slide1, slide2], str(video_path), str(srt_path))

    assert srt_path.exists()
    srt_content = srt_path.read_text(encoding="utf-8")
    assert "你好" in srt_content
    assert "謝謝" in srt_content
    mock_run.assert_called_once()


def test_compose_video_forwards_subtitle_margin_v_to_ffmpeg_command(tmp_path):
    slide = _slide([TimedChunk("你好", 0, 800)])

    class FakeResult:
        returncode = 0
        stderr = ""

    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch("ppt2course.video.get_audio_duration_ms", return_value=1000):
            with patch(
                "ppt2course.video.subprocess.run", return_value=FakeResult()
            ) as mock_run:
                compose_video(
                    [slide],
                    str(tmp_path / "out.mp4"),
                    str(tmp_path / "out.srt"),
                    subtitle_margin_v=250,
                )

    cmd = mock_run.call_args.args[0]
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "MarginV=250" in filter_complex


# ---- _add_logo_overlay / _mix_background_music / _concatenate_with_intro_outro ----

def _fake_run_ok():
    class FakeResult:
        returncode = 0
        stderr = ""

    return FakeResult()


def test_add_logo_overlay_command_construction():
    with patch("ppt2course.video.subprocess.run", return_value=_fake_run_ok()) as mock_run:
        _add_logo_overlay("video.mp4", "logo.png", "out.mp4", logo_width=160, margin=24)

    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["ffmpeg", "-y"]
    assert "video.mp4" in cmd
    assert "logo.png" in cmd
    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=160:-1" in filter_arg
    assert "colorchannelmixer=aa=1.0[logo]" in filter_arg  # default opacity: fully opaque
    assert "overlay=W-w-24:24" in filter_arg
    assert cmd[-1] == "out.mp4"


def test_add_logo_overlay_applies_custom_opacity():
    with patch("ppt2course.video.subprocess.run", return_value=_fake_run_ok()) as mock_run:
        _add_logo_overlay("video.mp4", "logo.png", "out.mp4", logo_width=160, margin=24, opacity=0.4)

    filter_arg = mock_run.call_args[0][0][mock_run.call_args[0][0].index("-filter_complex") + 1]
    assert "colorchannelmixer=aa=0.4[logo]" in filter_arg


def test_add_logo_overlay_defaults_to_top_right():
    with patch("ppt2course.video.subprocess.run", return_value=_fake_run_ok()) as mock_run:
        _add_logo_overlay("video.mp4", "logo.png", "out.mp4", logo_width=160, margin=24)

    filter_arg = mock_run.call_args[0][0][mock_run.call_args[0][0].index("-filter_complex") + 1]
    assert "overlay=W-w-24:24" in filter_arg


@pytest.mark.parametrize(
    "position, expected_overlay",
    [
        ("top-left", "overlay=24:24"),
        ("top-right", "overlay=W-w-24:24"),
        ("bottom-left", "overlay=24:H-h-24"),
        ("bottom-right", "overlay=W-w-24:H-h-24"),
    ],
)
def test_add_logo_overlay_positions_at_each_corner(position, expected_overlay):
    with patch("ppt2course.video.subprocess.run", return_value=_fake_run_ok()) as mock_run:
        _add_logo_overlay(
            "video.mp4", "logo.png", "out.mp4", logo_width=160, margin=24, position=position
        )

    filter_arg = mock_run.call_args[0][0][mock_run.call_args[0][0].index("-filter_complex") + 1]
    assert expected_overlay in filter_arg


def test_add_logo_overlay_rejects_unknown_position():
    with patch("ppt2course.video.subprocess.run", return_value=_fake_run_ok()):
        with pytest.raises(ValueError, match="logo_position"):
            _add_logo_overlay(
                "video.mp4", "logo.png", "out.mp4", logo_width=160, margin=24, position="middle"
            )


def test_add_logo_overlay_raises_on_nonzero_exit():
    class FakeResult:
        returncode = 1
        stderr = "logo failure"

    with patch("ppt2course.video.subprocess.run", return_value=FakeResult()):
        with pytest.raises(VideoComposeError):
            _add_logo_overlay("video.mp4", "logo.png", "out.mp4", 160, 24)


def test_mix_background_music_command_construction():
    with patch("ppt2course.video.subprocess.run", return_value=_fake_run_ok()) as mock_run:
        _mix_background_music("video.mp4", "bgm.mp3", "out.mp4", bgm_volume=0.2)

    cmd = mock_run.call_args[0][0]
    assert "-stream_loop" in cmd
    assert cmd[cmd.index("-stream_loop") + 1] == "-1"
    assert "bgm.mp3" in cmd
    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert "volume=0.2[bgm]" in filter_arg
    assert "amix=inputs=2:duration=first" in filter_arg
    assert "-shortest" in cmd


def test_mix_background_music_raises_on_nonzero_exit():
    class FakeResult:
        returncode = 1
        stderr = "bgm failure"

    with patch("ppt2course.video.subprocess.run", return_value=FakeResult()):
        with pytest.raises(VideoComposeError):
            _mix_background_music("video.mp4", "bgm.mp3", "out.mp4", 0.2)


def test_concatenate_with_intro_outro_no_intro_no_outro_copies_file(tmp_path):
    main = tmp_path / "main.mp4"
    main.write_bytes(b"main content")
    out = tmp_path / "out.mp4"

    _concatenate_with_intro_outro(str(main), str(out), None, None, (1920, 1080), 30)

    assert out.read_bytes() == b"main content"


def test_concatenate_with_intro_outro_builds_concat_filter_with_intro_and_outro():
    with patch("ppt2course.video.subprocess.run", return_value=_fake_run_ok()) as mock_run:
        _concatenate_with_intro_outro(
            "main.mp4", "out.mp4", "intro.mp4", "outro.mp4", (1280, 720), 30
        )

    cmd = mock_run.call_args[0][0]
    assert cmd.count("-i") == 3
    assert "intro.mp4" in cmd
    assert "main.mp4" in cmd
    assert "outro.mp4" in cmd
    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=3:v=1:a=1[outv][outa]" in filter_arg
    assert cmd[-1] == "out.mp4"


def test_concatenate_with_intro_outro_raises_on_nonzero_exit():
    class FakeResult:
        returncode = 1
        stderr = "concat failure"

    with patch("ppt2course.video.subprocess.run", return_value=FakeResult()):
        with pytest.raises(VideoComposeError):
            _concatenate_with_intro_outro(
                "main.mp4", "out.mp4", "intro.mp4", None, (1920, 1080), 30
            )


# ---- compose_video chaining Logo/BGM/intro-outro (mocked) ----

def test_compose_video_chains_logo_bgm_intro_outro_when_all_provided(tmp_path):
    slide1 = _slide([TimedChunk("你好", 0, 800), TimedChunk("。", 800, 800)])

    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch("ppt2course.video.get_audio_duration_ms", return_value=1000):
            with patch(
                "ppt2course.video.subprocess.run", return_value=_fake_run_ok()
            ) as mock_run:
                compose_video(
                    [slide1],
                    str(tmp_path / "out.mp4"),
                    str(tmp_path / "out.srt"),
                    logo_path="logo.png",
                    bgm_path="bgm.mp3",
                    intro_path="intro.mp4",
                    outro_path="outro.mp4",
                )

    # core build + logo + bgm + intro/outro concat = 4 ffmpeg invocations
    assert mock_run.call_count == 4


def test_compose_video_forwards_logo_opacity_to_overlay_filter(tmp_path):
    slide1 = _slide([TimedChunk("你好", 0, 800), TimedChunk("。", 800, 800)])

    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch("ppt2course.video.get_audio_duration_ms", return_value=1000):
            with patch("ppt2course.video.shutil.copy"):
                with patch(
                    "ppt2course.video.subprocess.run", return_value=_fake_run_ok()
                ) as mock_run:
                    compose_video(
                        [slide1],
                        str(tmp_path / "out.mp4"),
                        str(tmp_path / "out.srt"),
                        logo_path="logo.png",
                        logo_opacity=0.5,
                    )

    logo_call = next(c for c in mock_run.call_args_list if "logo.png" in c[0][0])
    filter_arg = logo_call[0][0][logo_call[0][0].index("-filter_complex") + 1]
    assert "colorchannelmixer=aa=0.5[logo]" in filter_arg


def test_compose_video_forwards_logo_position_to_overlay_filter(tmp_path):
    slide1 = _slide([TimedChunk("你好", 0, 800), TimedChunk("。", 800, 800)])

    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch("ppt2course.video.get_audio_duration_ms", return_value=1000):
            with patch("ppt2course.video.shutil.copy"):
                with patch(
                    "ppt2course.video.subprocess.run", return_value=_fake_run_ok()
                ) as mock_run:
                    compose_video(
                        [slide1],
                        str(tmp_path / "out.mp4"),
                        str(tmp_path / "out.srt"),
                        logo_path="logo.png",
                        logo_position="bottom-left",
                        logo_margin=24,
                    )

    logo_call = next(c for c in mock_run.call_args_list if "logo.png" in c[0][0])
    filter_arg = logo_call[0][0][logo_call[0][0].index("-filter_complex") + 1]
    assert "overlay=24:H-h-24" in filter_arg


def test_compose_video_no_optional_extras_only_calls_ffmpeg_once(tmp_path):
    slide1 = _slide([TimedChunk("你好", 0, 800)])

    with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch("ppt2course.video.get_audio_duration_ms", return_value=1000):
            with patch(
                "ppt2course.video.subprocess.run", return_value=_fake_run_ok()
            ) as mock_run:
                compose_video(
                    [slide1], str(tmp_path / "out.mp4"), str(tmp_path / "out.srt")
                )

    assert mock_run.call_count == 1


# ---- reading_pause_ms (visual-only slide hold, no change to narration) ----

def test_slide_video_input_rejects_negative_reading_pause():
    with pytest.raises(VideoComposeError):
        SlideVideoInput(
            image_path="img.png", audio_path="audio.mp3", chunks=[], reading_pause_ms=-1
        )


def test_slide_video_input_reading_pause_defaults_to_zero():
    slide = SlideVideoInput(image_path="img.png", audio_path="audio.mp3", chunks=[])
    assert slide.reading_pause_ms == 0


def test_audio_label_filter_without_pause_is_unchanged_anull():
    assert _audio_label_filter(2, 0) == "[2:a]anull[a0]"


def test_audio_label_filter_with_pause_pads_to_visual_duration():
    assert _audio_label_filter(2, 0, visual_duration_ms=2500) == "[2:a]apad=whole_dur=2.5[a0]"


def test_build_ffmpeg_command_reading_pause_extends_image_hold_time():
    slide1 = SlideVideoInput(
        image_path="s1.png", audio_path="a1.mp3", chunks=[TimedChunk("你好", 0, 800)],
        reading_pause_ms=500,
    )
    slide2 = _slide([TimedChunk("謝謝", 0, 700)])
    # durations_ms passed in here already represents *visual* duration —
    # compose_video is the one responsible for adding reading_pause_ms on
    # top of the real narration length before calling this.
    cmd = _build_ffmpeg_command(
        [slide1, slide2],
        durations_ms=[2500, 1000],  # slide1: 2000ms narration + 500ms pause
        offsets_ms=[0, 2000],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
    )
    assert ["-loop", "1", "-t", "2.5", "-i", "s1.png"] == cmd[cmd.index("-loop"):cmd.index("-loop") + 6]
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "[2:a]apad=whole_dur=2.5[a0]" in filter_complex
    assert "[3:a]anull[a1]" in filter_complex  # slide2 has no pause -> unchanged


def test_compose_video_reading_pause_shifts_next_slide_offset_without_touching_first_cue(tmp_path):
    # The single most important reading-pause regression: the pause moves
    # *when the next slide starts*, but never how long the first slide's own
    # subtitle cue lasts.
    chunks1 = [TimedChunk("你好", 0, 800)]
    chunks2 = [TimedChunk("謝謝", 0, 700)]

    def _run(pause_ms: int, srt_path):
        slide1 = SlideVideoInput(
            image_path="s1.png", audio_path="a1.mp3", chunks=chunks1, reading_pause_ms=pause_ms
        )
        slide2 = SlideVideoInput(image_path="s2.png", audio_path="a2.mp3", chunks=chunks2)
        with patch("ppt2course.video.shutil.which", return_value="/usr/bin/ffmpeg"):
            with patch(
                "ppt2course.video.get_audio_duration_ms", side_effect=[2000, 1000]
            ):
                with patch(
                    "ppt2course.video.subprocess.run", return_value=_fake_run_ok()
                ) as mock_run:
                    compose_video(
                        [slide1, slide2],
                        str(tmp_path / f"out_{pause_ms}.mp4"),
                        str(srt_path),
                        transition_duration_ms=500,
                    )
        return srt_path.read_text(encoding="utf-8"), mock_run

    srt_without, _ = _run(0, tmp_path / "without.srt")
    srt_with, mock_run = _run(1000, tmp_path / "with.srt")

    # First slide's own cue: identical start/end in both cases.
    first_cue_without = srt_without.split("\n\n")[0]
    first_cue_with = srt_with.split("\n\n")[0]
    assert first_cue_without == first_cue_with

    # Second slide's cue starts 1000ms later with the pause (offset shifted
    # from 2000-500=1500 to 3000-500=2500).
    second_cue_with = srt_with.split("\n\n")[1]
    assert "00:00:02,500" in second_cue_with

    filter_complex = mock_run.call_args[0][0][
        mock_run.call_args[0][0].index("-filter_complex") + 1
    ]
    assert "apad=whole_dur=3.0" in filter_complex  # 2000ms narration + 1000ms pause


# ---- Ken Burns (subtle zoompan) ----

def test_ken_burns_filter_reaches_max_zoom_by_the_last_frame():
    filt = _ken_burns_filter(0, "v0", 1920, 1080, 30, 2000)  # 2s @ 30fps = 60 frames
    assert "zoompan=" in filt
    assert "min(1+(1.03-1)*on/60,1.03)" in filt
    assert "s=1920x1080" in filt
    assert "fps=30" in filt


def test_build_ffmpeg_command_ken_burns_replaces_scale_pad_for_every_slide():
    slide1 = _slide([TimedChunk("你好", 0, 800)])
    slide2 = _slide([TimedChunk("謝謝", 0, 700)])
    cmd = _build_ffmpeg_command(
        [slide1, slide2],
        durations_ms=[2000, 1000],
        offsets_ms=[0, 1500],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
        enable_ken_burns=True,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "zoompan=" in filter_complex
    assert "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease" not in filter_complex


def test_build_ffmpeg_command_ken_burns_off_by_default_is_unchanged():
    slide1 = _slide([TimedChunk("你好", 0, 800)])
    cmd = _build_ffmpeg_command(
        [slide1],
        durations_ms=[2000],
        offsets_ms=[0],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "zoompan" not in filter_complex


def test_build_ffmpeg_command_ken_burns_composes_with_broll_and_avatar():
    slide = SlideVideoInput(
        image_path="s1.png",
        audio_path="a1.mp3",
        chunks=[TimedChunk("你好", 0, 800)],
        broll_overlays=(BrollOverlay(image_path="b.jpg", start_ms=100, end_ms=300),),
        avatar_overlays=(AvatarOverlay(image_path="idle.png", start_ms=0, end_ms=1000),),
    )
    cmd = _build_ffmpeg_command(
        [slide],
        durations_ms=[1000],
        offsets_ms=[0],
        srt_path="out.srt",
        out_video_path="out.mp4",
        transition="fade",
        transition_duration_ms=500,
        resolution=(1920, 1080),
        fps=30,
        font_size=64,
        enable_ken_burns=True,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:v]" in filter_complex and "zoompan=" in filter_complex
    assert "[v0base][v0broll0]overlay=enable='between(t,0.1,0.3)'[v0brolled]" in filter_complex
    assert "[v0brolled][v0avatar0]overlay=" in filter_complex
