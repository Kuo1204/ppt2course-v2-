from unittest.mock import patch

import pytest

from ppt2course.export import ExportError
from ppt2course.pipeline import (
    DEFAULT_SENTENCE_PAUSE_MS,
    DEFAULT_SILENT_DURATION_MS,
    PipelineError,
    run_pipeline,
)
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
                            "ppt2course.pipeline.export_outputs", return_value=expected
                        ) as mock_export:
                            result = run_pipeline(
                                "deck.pptx", ["img1.png", "img2.png"],
                                str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                logo_path="logo.png", logo_opacity=0.6, bgm_path="bgm.mp3",
                                subtitle_margin_v=200, logo_position="bottom-right",
                            )

    assert result == expected
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
                            run_pipeline(
                                "deck.pptx", ["img1.png"],
                                str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                custom_dict_path="custom_dict.txt",
                            )

    assert mock_compose.call_args.kwargs["custom_dict_path"] == "custom_dict.txt"


# ---- sentence-pause narration splicing ----


def test_single_sentence_script_synthesizes_directly_no_pause_splicing(tmp_path):
    # No 。！？ at all -> split_into_sentences returns exactly one piece ->
    # falls straight through to a single synthesize() call, matching the
    # pre-existing (no-pause) behavior exactly.
    slides = _slides(1)
    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=["講稿沒有句尾標點"]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize",
                    return_value=[TimedChunk("x", 0, 1000)],
                ) as mock_synth:
                    with patch("ppt2course.pipeline.subprocess.run") as mock_run:
                        with patch("ppt2course.pipeline.compose_video"):
                            with patch(
                                "ppt2course.pipeline.export_outputs",
                                return_value={"mp4": "a", "srt": "b", "docx": "c"},
                            ):
                                run_pipeline(
                                    "deck.pptx", ["img1.png"],
                                    str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                    ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                )

    mock_synth.assert_called_once()
    mock_run.assert_not_called()


def test_multi_sentence_script_synthesizes_each_sentence_with_pause_spliced_between(tmp_path):
    slides = _slides(1)
    text = "第一句。第二句！"

    def fake_synth(sentence_text, voice, out_path, rate, volume):
        with open(out_path, "wb") as f:
            f.write(b"fake-mp3")
        if sentence_text == "第一句。":
            return [TimedChunk("第一句", 0, 500), TimedChunk("。", 500, 500)]
        return [TimedChunk("第二句", 0, 400), TimedChunk("！", 400, 400)]

    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=[text]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize", side_effect=fake_synth
                ) as mock_synth:
                    with patch(
                        "ppt2course.pipeline.get_audio_duration_ms", side_effect=[520, 410]
                    ):
                        with patch(
                            "ppt2course.pipeline.subprocess.run", return_value=_fake_run_ok()
                        ) as mock_run:
                            with patch("ppt2course.pipeline.compose_video") as mock_compose:
                                with patch(
                                    "ppt2course.pipeline.export_outputs",
                                    return_value={"mp4": "a", "srt": "b", "docx": "c"},
                                ):
                                    run_pipeline(
                                        "deck.pptx", ["img1.png"],
                                        str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                        ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                        sentence_pause_ms=250,
                                    )

    assert mock_synth.call_count == 2
    assert [c.args[0] for c in mock_synth.call_args_list] == ["第一句。", "第二句！"]

    # one ffmpeg call to trim each of the 2 sentences down to its real
    # WordBoundary content span, one to render the silent gap, one to
    # splice everything (sentence 1 + gap + sentence 2) back together
    assert mock_run.call_count == 4

    slide_inputs = mock_compose.call_args.args[0]
    chunks = slide_inputs[0].chunks
    # sentence 2's chunks are offset by sentence 1's real audio duration
    # (520ms, from the mocked get_audio_duration_ms) plus the 250ms pause.
    assert [(c.text, c.start_ms, c.end_ms) for c in chunks] == [
        ("第一句", 0, 500),
        ("。", 500, 500),
        ("第二句", 770, 1170),
        ("！", 1170, 1170),
    ]


def test_sentence_pause_ms_zero_disables_pause_splicing_even_with_multiple_sentences(tmp_path):
    slides = _slides(1)
    text = "第一句。第二句！"

    with patch("ppt2course.pipeline.parse_ppt", return_value=slides):
        with patch("ppt2course.pipeline.generate_script", return_value=[text]):
            with patch("ppt2course.pipeline.clean_script", side_effect=lambda t: t):
                with patch(
                    "ppt2course.pipeline.synthesize",
                    return_value=[TimedChunk("x", 0, 1000)],
                ) as mock_synth:
                    with patch("ppt2course.pipeline.subprocess.run") as mock_run:
                        with patch("ppt2course.pipeline.compose_video"):
                            with patch(
                                "ppt2course.pipeline.export_outputs",
                                return_value={"mp4": "a", "srt": "b", "docx": "c"},
                            ):
                                run_pipeline(
                                    "deck.pptx", ["img1.png"],
                                    str(tmp_path / "work"), str(tmp_path / "out"), "課程",
                                    ScriptMode.NOTES, "zh-TW-HsiaoChenNeural",
                                    sentence_pause_ms=0,
                                )

    mock_synth.assert_called_once()
    assert mock_synth.call_args.args[0] == text
    mock_run.assert_not_called()


def test_default_sentence_pause_ms_is_used_when_not_specified(tmp_path):
    assert DEFAULT_SENTENCE_PAUSE_MS > 0
