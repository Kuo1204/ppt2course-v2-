import os
from unittest.mock import patch

import pytest

from ppt2course.avatar import AvatarAssetSet, default_asset_set
from ppt2course.export import ExportError
from ppt2course.pipeline import DEFAULT_SILENT_DURATION_MS, PipelineError, run_pipeline
from ppt2course.script_gen import ScriptGenerationError, ScriptMode
from ppt2course.subtitle import TimedChunk
from ppt2course.tts import TtsError
from ppt2course.upload import PptParseError, SlideContent
from ppt2course.video import VideoComposeError


def _slides(n=2):
    return [SlideContent(index=i + 1, text=f"slide {i + 1}", notes="") for i in range(n)]


def _fake_run_ok():
    class FakeResult:
        returncode = 0
        stderr = ""

    return FakeResult()


def _base_patches(slides=None, scripts=None):
    slides = slides if slides is not None else _slides(2)
    scripts = scripts if scripts is not None else ["講稿一", "講稿二"]
    return slides, scripts


def test_raises_when_image_count_does_not_match_slide_count(tmp_path):
    slides = _slides(2)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with pytest.raises(PipelineError):
            run_pipeline(
                "deck.pptx",
                ["img1.png"],
                str(tmp_path / "work"),
                str(tmp_path / "out"),
                "課程",
                ScriptMode.NOTES,
                "zh-TW-HsiaoChenNeural",
            )


def test_wraps_ppt_parse_error():
    with patch("ppt2course.pipeline.parse_ppt", side_effect=PptParseError("bad file")):
        with pytest.raises(PipelineError):
            run_pipeline(
                "deck.pptx", ["img1.png"], "work", "out", "課程",
                ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
            )


def test_wraps_script_generation_error():
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch(
            "ppt2course.pipeline.generate_script",
            side_effect=ScriptGenerationError("gemini quota exceeded"),
        ):
            with pytest.raises(PipelineError):
                run_pipeline(
                    "deck.pptx", ["img1.png"], "work", "out", "課程",
                    ScriptMode.AUTO, "zh-TW-HsiaoChenNeural", gemini_api_key="key",
                )


def test_wraps_tts_error_with_slide_index_in_message(tmp_path):
    slides = _slides(2)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿一", "講稿二"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize",
                    side_effect=TtsError("network error"),
                ):
                    with pytest.raises(PipelineError, match="slide 1"):
                        run_pipeline(
                            "deck.pptx", ["img1.png", "img2.png"],
                            str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                            ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                        )


def test_empty_script_slide_generates_silent_audio_instead_of_calling_tts(tmp_path):
    slides = _slides(2)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["", "講稿二"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch("ppt2course.pipeline.synthesize", return_value=[]) as mock_synth:
                    with patch(
                        "ppt2course.pipeline.subprocess.run", return_value=_fake_run_ok()
                    ) as mock_run:
                        with patch("ppt2course.pipeline.compose_video"):
                            with patch(
                                "ppt2course.pipeline.export_outputs",
                                return_value={"mp4": "a", "srt": "b", "docx": "c"},
                            ):
                                with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                    with patch(
                                        "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                    ):
                                        run_pipeline(
                                            "deck.pptx", ["img1.png", "img2.png"],
                                            str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                            ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        )

    # slide 1 (empty script) -> silent audio via ffmpeg, never calls synthesize for it
    # slide 2 (has script) -> calls synthesize once
    assert mock_synth.call_count == 1
    assert mock_run.call_count == 1
    silent_cmd = mock_run.call_args[0][0]
    assert "-t" in silent_cmd
    assert str(DEFAULT_SILENT_DURATION_MS / 1000) in silent_cmd


def test_silent_audio_generation_raises_on_ffmpeg_failure(tmp_path):
    slides = _slides(1)

    class FakeResult:
        returncode = 1
        stderr = "ffmpeg exploded"

    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=[""]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.subprocess.run", return_value=FakeResult()
                ):
                    with pytest.raises(PipelineError):
                        run_pipeline(
                            "deck.pptx", ["img1.png"],
                            str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                            ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                        )


def test_wraps_video_compose_error():
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize",
                    return_value=[TimedChunk("講稿", 0, 1000)],
                ):
                    with patch(
                        "ppt2course.pipeline.compose_video",
                        side_effect=VideoComposeError("ffmpeg not found"),
                    ):
                        with pytest.raises(PipelineError):
                            run_pipeline(
                                "deck.pptx", ["img1.png"], "work", "out", "課程",
                                ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                            )


def test_wraps_export_error():
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize",
                    return_value=[TimedChunk("講稿", 0, 1000)],
                ):
                    with patch("ppt2course.pipeline.compose_video"):
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            side_effect=ExportError("file already exists"),
                        ):
                            with pytest.raises(PipelineError):
                                run_pipeline(
                                    "deck.pptx", ["img1.png"], "work", "out", "課程",
                                    ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                )


def test_happy_path_returns_export_outputs_result(tmp_path):
    slides = _slides(2)
    expected = {"mp4": "課程.mp4", "srt": "課程.srt", "docx": "課程.docx"}
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿一", "講稿二"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize",
                    return_value=[TimedChunk("x", 0, 1000)],
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs", return_value=dict(expected)
                        ) as mock_export:
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=5000
                                ):
                                    result = run_pipeline(
                                        "deck.pptx", ["img1.png", "img2.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        logo_path="logo.png", logo_opacity=0.6, bgm_path="bgm.mp3",
                                        subtitle_margin_v=200, logo_position="bottom-right",
                                    )

    assert result["mp4"] == expected["mp4"]
    assert result["srt"] == expected["srt"]
    assert result["docx"] == expected["docx"]
    assert result["video_size_bytes"] == 1234
    assert result["video_duration_ms"] == 5000
    assert result["script_char_count"] == len("講稿一") + len("講稿二")
    mock_compose.assert_called_once()
    assert mock_compose.call_args.kwargs["logo_path"] == "logo.png"
    assert mock_compose.call_args.kwargs["logo_opacity"] == 0.6
    assert mock_compose.call_args.kwargs["bgm_path"] == "bgm.mp3"
    assert mock_compose.call_args.kwargs["subtitle_margin_v"] == 200
    assert mock_compose.call_args.kwargs["logo_position"] == "bottom-right"
    mock_export.assert_called_once()
    assert mock_export.call_args[0][2] == ["講稿一", "講稿二"]


def test_forwards_voice_rate_and_volume_to_synthesize(tmp_path):
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize",
                    return_value=[TimedChunk("x", 0, 1000)],
                ) as mock_synth:
                    with patch("ppt2course.pipeline.compose_video"):
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        voice_rate="+20%", voice_volume="-10%",
                                    )

    assert mock_synth.call_args.kwargs["rate"] == "+20%"
    assert mock_synth.call_args.kwargs["volume"] == "-10%"


def test_forwards_custom_dict_path_to_compose_video(tmp_path):
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        custom_dict_path="custom_dict.txt",
                                    )

    assert mock_compose.call_args.kwargs["custom_dict_path"] == "custom_dict.txt"


# ---- video/script stats surfaced alongside the mp4/srt/docx paths ----
# So the frontend can show "影片容量/時長/講稿字數" on the results screen
# once a job finishes, without re-deriving them client-side.


def test_result_includes_script_char_count_video_size_and_duration(tmp_path):
    slides = _slides(2)

    def fake_export(video_path, srt_path, scripts, out_dir, base_name):
        os.makedirs(out_dir, exist_ok=True)
        dest = os.path.join(out_dir, f"{base_name}.mp4")
        with open(dest, "wb") as f:
            f.write(b"x" * 12345)
        return {"mp4": dest, "srt": os.path.join(out_dir, "b.srt"), "docx": os.path.join(out_dir, "c.docx")}

    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿一二三", "講稿四五"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video"):
                        with patch("ppt2course.pipeline.export_outputs", side_effect=fake_export):
                            with patch(
                                "ppt2course.pipeline.get_audio_duration_ms", return_value=9876
                            ) as mock_duration:
                                result = run_pipeline(
                                    "deck.pptx", ["img1.png", "img2.png"],
                                    str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                    ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                )

    assert result["script_char_count"] == len("講稿一二三") + len("講稿四五")
    assert result["video_size_bytes"] == 12345
    assert result["video_duration_ms"] == 9876
    mock_duration.assert_called_once_with(result["mp4"])


# ---- broll_selections (confirmed AI/user-picked B-roll, optional) ----
# The one invariant that matters here: a B-roll pick can only ever change
# which BrollOverlay objects a slide's SlideVideoInput carries — never its
# script, its own audio_path, or how many times synthesize/compose_video is
# called. get_audio_duration_ms must also stay untouched (and unmocked
# tests must keep passing) when there is nothing to clamp against.


def test_no_broll_selections_never_calls_get_audio_duration_ms_per_slide(tmp_path):
    # Regression guard: broll_selections=None/[] must not add a real
    # ffprobe call in the per-slide loop — every existing caller/test that
    # doesn't mock get_audio_duration_ms would otherwise start hitting a
    # real subprocess against a file synthesize() never actually wrote.
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ) as mock_duration:
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                    )

    slide_inputs = mock_compose.call_args.args[0]
    assert slide_inputs[0].broll_overlays == ()
    # Only the one call export_outputs's video_duration_ms lookup makes.
    mock_duration.assert_called_once()


def test_broll_selection_becomes_broll_overlay_on_matching_slide(tmp_path):
    slides = _slides(2)
    selections = [
        {"slide_number": 2, "image_path": "broll.jpg", "start_ms": 200, "end_ms": 800},
    ]
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿一", "講稿二"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1500
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png", "img2.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        broll_selections=selections,
                                    )

    slide_inputs = mock_compose.call_args.args[0]
    assert slide_inputs[0].broll_overlays == ()  # slide 1: no matching selection
    assert len(slide_inputs[1].broll_overlays) == 1
    overlay = slide_inputs[1].broll_overlays[0]
    assert overlay.image_path == "broll.jpg"
    assert overlay.start_ms == 200
    assert overlay.end_ms == 800


def test_broll_selection_end_ms_clamped_to_real_audio_duration(tmp_path):
    slides = _slides(1)
    selections = [{"slide_number": 1, "image_path": "broll.jpg", "start_ms": 100, "end_ms": 9999}]
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1200
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        broll_selections=selections,
                                    )

    overlay = mock_compose.call_args.args[0][0].broll_overlays[0]
    assert overlay.end_ms == 1200  # clamped down from the requested 9999


def test_broll_selection_starting_past_audio_duration_is_dropped(tmp_path):
    slides = _slides(1)
    selections = [{"slide_number": 1, "image_path": "broll.jpg", "start_ms": 5000, "end_ms": 6000}]
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1200
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        broll_selections=selections,
                                    )

    assert mock_compose.call_args.args[0][0].broll_overlays == ()


# ---- avatar (mouth-flap overlays driven by real per-slide TimedChunk) ----
# Same invariant as B-roll above: the avatar mode can only ever change which
# AvatarOverlay objects a slide's SlideVideoInput carries — never its
# script, audio_path, or how many times synthesize/compose_video is called.


def test_avatar_mode_none_default_never_calls_get_audio_duration_ms_per_slide(tmp_path):
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ) as mock_duration:
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                    )

    assert mock_compose.call_args.args[0][0].avatar_overlays == ()
    mock_duration.assert_called_once()  # only export_outputs's video_duration_ms lookup


def test_avatar_mode_always_builds_overlays_from_real_chunks(tmp_path):
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        avatar_mode="always",
                                    )

    overlays = mock_compose.call_args.args[0][0].avatar_overlays
    assert len(overlays) == 1
    assert overlays[0].start_ms == 0
    assert overlays[0].end_ms == 1000
    assert overlays[0].image_path == default_asset_set().talk_open


def test_avatar_mode_keyframe_skips_slides_with_empty_script(tmp_path):
    slides = _slides(2)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿一", "   "]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch(
                        "ppt2course.pipeline.subprocess.run", return_value=_fake_run_ok()
                    ):
                        with patch("ppt2course.pipeline.compose_video") as mock_compose:
                            with patch(
                                "ppt2course.pipeline.export_outputs",
                                return_value={"mp4": "a", "srt": "b", "docx": "c"},
                            ):
                                with patch(
                                    "ppt2course.pipeline.os.path.getsize", return_value=1234
                                ):
                                    with patch(
                                        "ppt2course.pipeline.get_audio_duration_ms",
                                        return_value=1000,
                                    ):
                                        run_pipeline(
                                            "deck.pptx", ["img1.png", "img2.png"],
                                            str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                            ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                            avatar_mode="keyframe",
                                        )

    slide_inputs = mock_compose.call_args.args[0]
    assert len(slide_inputs[0].avatar_overlays) == 1  # "講稿一" -> has content
    assert slide_inputs[1].avatar_overlays == ()  # blank script (silent slide) -> skipped


def test_avatar_mode_custom_only_shows_on_listed_slides(tmp_path):
    slides = _slides(2)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿一", "講稿二"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png", "img2.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        avatar_mode="custom",
                                        avatar_custom_slides=[2],
                                    )

    slide_inputs = mock_compose.call_args.args[0]
    assert slide_inputs[0].avatar_overlays == ()
    assert len(slide_inputs[1].avatar_overlays) == 1


def test_avatar_position_size_margin_forwarded_to_compose_video(tmp_path):
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        avatar_mode="always",
                                        avatar_position="left",
                                        avatar_size="large",
                                        avatar_margin=8,
                                    )

    kwargs = mock_compose.call_args.kwargs
    assert kwargs["avatar_position"] == "left"
    assert kwargs["avatar_size"] == "large"
    assert kwargs["avatar_margin"] == 8


def test_avatar_asset_set_override_is_used_instead_of_default(tmp_path):
    slides = _slides(1)
    custom_assets = AvatarAssetSet(idle="custom_idle.png", talk_open="custom_open.png")
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        avatar_mode="always",
                                        avatar_asset_set=custom_assets,
                                    )

    overlay = mock_compose.call_args.args[0][0].avatar_overlays[0]
    assert overlay.image_path == "custom_open.png"


# ---- reading_pause_ms / closing_pause_ms / target_duration_ms / Ken Burns ----
# These only ever change SlideVideoInput.reading_pause_ms (flat mode) or,
# additionally, measure real narration length up front to auto-distribute
# pauses (target mode) -- never the script, chunks, or TTS call count.


def test_flat_reading_pause_applied_uniformly_to_every_slide(tmp_path):
    slides = _slides(2)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿一", "講稿二"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png", "img2.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        reading_pause_ms=800,
                                    )

    slide_inputs = mock_compose.call_args.args[0]
    assert [s.reading_pause_ms for s in slide_inputs] == [800, 800]


def test_closing_pause_added_only_to_the_last_slide(tmp_path):
    slides = _slides(2)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿一", "講稿二"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png", "img2.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        reading_pause_ms=500,
                                        closing_pause_ms=2000,
                                    )

    slide_inputs = mock_compose.call_args.args[0]
    assert [s.reading_pause_ms for s in slide_inputs] == [500, 2500]


def test_no_pause_settings_never_touches_reading_pause_ms(tmp_path):
    # Regression guard, same spirit as the broll/avatar "no-op is free"
    # tests: zero pause settings must leave SlideVideoInput exactly as it
    # was before this feature existed, and never call get_audio_duration_ms
    # ahead of compose_video.
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ) as mock_duration:
                                    outputs = run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                    )

    assert mock_compose.call_args.args[0][0].reading_pause_ms == 0
    mock_duration.assert_called_once()  # only export_outputs's video_duration_ms lookup
    assert "target_duration_reachable" not in outputs


def test_target_duration_shorter_than_narration_leaves_pauses_at_zero_and_unreachable(tmp_path):
    slides = _slides(2)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿一", "講稿二"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                # 2 calls to measure real narration (4000, 3000) +
                                # 1 final call for outputs' video_duration_ms.
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms",
                                    side_effect=[4000, 3000, 6500],
                                ):
                                    outputs = run_pipeline(
                                        "deck.pptx", ["img1.png", "img2.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        transition_duration_ms=500,
                                        target_duration_ms=5000,  # narration-only total is 6500ms
                                    )

    slide_inputs = mock_compose.call_args.args[0]
    assert [s.reading_pause_ms for s in slide_inputs] == [0, 0]
    assert outputs["target_duration_reachable"] is False


def test_target_duration_longer_than_narration_distributes_slack_as_reading_pause(tmp_path):
    slides = _slides(2)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿一", "講稿二"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms",
                                    side_effect=[4000, 3000, 8500],
                                ):
                                    outputs = run_pipeline(
                                        "deck.pptx", ["img1.png", "img2.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        transition_duration_ms=500,
                                        # narration-only total is 6500ms; +2000ms slack.
                                        target_duration_ms=8500,
                                    )

    slide_inputs = mock_compose.call_args.args[0]
    assert [s.reading_pause_ms for s in slide_inputs] == [1000, 1000]
    assert outputs["target_duration_reachable"] is True


def test_target_duration_mode_ignores_explicit_reading_and_closing_pause(tmp_path):
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms",
                                    side_effect=[4000, 5000],
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        reading_pause_ms=9999,
                                        closing_pause_ms=9999,
                                        target_duration_ms=5000,
                                    )

    # Auto-computed 1000ms (5000-4000), not the explicit 9999+9999.
    assert mock_compose.call_args.args[0][0].reading_pause_ms == 1000


def test_enable_ken_burns_forwarded_to_compose_video(tmp_path):
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", return_value=[TimedChunk("x", 0, 1000)]
                ):
                    with patch("ppt2course.pipeline.compose_video") as mock_compose:
                        with patch(
                            "ppt2course.pipeline.export_outputs",
                            return_value={"mp4": "a", "srt": "b", "docx": "c"},
                        ):
                            with patch("ppt2course.pipeline.os.path.getsize", return_value=1234):
                                with patch(
                                    "ppt2course.pipeline.get_audio_duration_ms", return_value=1000
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        enable_ken_burns=True,
                                    )

    assert mock_compose.call_args.kwargs["enable_ken_burns"] is True
