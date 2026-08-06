from unittest.mock import patch

import pytest

from ppt2course.subtitle import TimedChunk
from ppt2course.video import (
    SlideVideoInput,
    VideoComposeError,
    _add_logo_overlay,
    _audio_acrossfade_chain,
    _build_cues,
    _build_ffmpeg_command,
    _compute_slide_offsets,
    _concatenate_with_intro_outro,
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
