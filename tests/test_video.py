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
    assert "scale=160:-1[logo]" in filter_arg
    assert "overlay=W-w-24:24" in filter_arg
    assert cmd[-1] == "out.mp4"


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
