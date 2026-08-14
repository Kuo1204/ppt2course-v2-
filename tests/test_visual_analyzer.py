import json
from unittest.mock import patch

import pytest

from ppt2course.subtitle import TimedChunk
from ppt2course.upload import SlideContent
from ppt2course.visual_analyzer import analyze_visual_needs, suggest_broll_window_ms


def _slide(index=1, text="一般內容", **kwargs):
    return SlideContent(index=index, text=text, notes="", **kwargs)


def test_without_api_key_returns_nonfatal_local_recommendations():
    result = analyze_visual_needs([_slide()], ["旁白"])
    assert len(result.recommendations) == 1
    assert result.used_ai is False
    assert result.warnings


def test_heuristic_recommends_case_with_abstract_concept_but_not_every_slide():
    slides = [
        _slide(1, "案例：員工遭到孤立與壓力"),
        _slide(2, "課程名稱", title="第一章"),
    ]
    result = analyze_visual_needs(slides, ["案例情境說明", "章節開場"])
    assert result.recommendations[0].recommended is True
    assert result.recommendations[0].visual_need_score >= 61
    assert result.recommendations[1].recommended is False


def test_existing_visual_reduces_need_score():
    plain = analyze_visual_needs([_slide(text="溝通與信任")], ["抽象概念"]).recommendations[0]
    illustrated = analyze_visual_needs(
        [_slide(text="溝通與信任", image_count=1)], ["抽象概念"]
    ).recommendations[0]
    assert illustrated.visual_need_score < plain.visual_need_score


def test_ai_json_is_validated_and_threshold_is_computed_locally():
    rows = [{
        "slide_number": 1,
        "title": "職場孤立",
        "visual_need_score": 82,
        "reason": "情境圖片有助理解",
        "visual_type": "image",
        "keywords": ["workplace isolation", "employee stress"],
        "suggested_position": "during_slide",
    }]

    class Response:
        text = json.dumps(rows)

    class Models:
        def generate_content(self, **kwargs):
            return Response()

    class Client:
        models = Models()

    with patch("ppt2course.visual_analyzer.genai.Client", return_value=Client()):
        result = analyze_visual_needs([_slide()], ["旁白"], api_key="key")

    assert result.used_ai is True
    assert result.warnings == ()
    assert result.recommendations[0].recommended is True
    assert result.recommendations[0].keywords[0] == "workplace isolation"


def test_malformed_or_failed_ai_response_falls_back_instead_of_raising():
    with patch("ppt2course.visual_analyzer.genai.Client", side_effect=RuntimeError("quota")):
        result = analyze_visual_needs([_slide()], ["旁白"], api_key="key")
    assert result.used_ai is False
    assert len(result.recommendations) == 1
    assert "quota" in result.warnings[0]


def test_script_count_mismatch_is_a_caller_error():
    with pytest.raises(ValueError, match="scripts length"):
        analyze_visual_needs([_slide()], [])


# ---- script_anchor (real-timing basis for "AI 自動抓時間點") ----


def test_heuristic_script_anchor_is_a_real_trigger_term_from_the_script():
    result = analyze_visual_needs(
        [_slide(text="案例：員工遭到孤立與壓力")], ["這裡討論的是孤立與衝突的情境"]
    )
    anchor = result.recommendations[0].script_anchor
    assert anchor  # a trigger term was present, so this must not be empty
    assert anchor in "這裡討論的是孤立與衝突的情境"


def test_heuristic_script_anchor_is_empty_when_script_has_nothing_to_anchor_to():
    result = analyze_visual_needs([_slide(text="無關內容")], [""])
    assert result.recommendations[0].script_anchor == ""


def test_ai_script_anchor_is_used_when_it_is_a_real_substring():
    rows = [{
        "slide_number": 1, "title": "職場孤立", "visual_need_score": 82,
        "reason": "情境圖片有助理解", "visual_type": "image",
        "keywords": ["workplace isolation"], "suggested_position": "during_slide",
        "script_anchor": "孤立",
    }]

    class Response:
        text = json.dumps(rows)

    class Models:
        def generate_content(self, **kwargs):
            return Response()

    class Client:
        models = Models()

    with patch("ppt2course.visual_analyzer.genai.Client", return_value=Client()):
        result = analyze_visual_needs([_slide()], ["員工被孤立的情況"], api_key="key")

    assert result.recommendations[0].script_anchor == "孤立"


def test_ai_script_anchor_hallucination_falls_back_to_heuristic_extraction():
    # Gemini claims an anchor that never actually appears in this slide's
    # script — must not be trusted verbatim.
    rows = [{
        "slide_number": 1, "title": "職場孤立", "visual_need_score": 82,
        "reason": "情境圖片有助理解", "visual_type": "image",
        "keywords": ["workplace isolation"], "suggested_position": "during_slide",
        "script_anchor": "完全不存在的文字",
    }]

    class Response:
        text = json.dumps(rows)

    class Models:
        def generate_content(self, **kwargs):
            return Response()

    class Client:
        models = Models()

    with patch("ppt2course.visual_analyzer.genai.Client", return_value=Client()):
        result = analyze_visual_needs([_slide()], ["員工被孤立的情況"], api_key="key")

    assert result.recommendations[0].script_anchor != "完全不存在的文字"
    assert result.recommendations[0].script_anchor in "員工被孤立的情況" or (
        result.recommendations[0].script_anchor == ""
    )


# ---- suggest_broll_window_ms ----
# The "Script + Subtitle/word timestamp" mechanism itself: real per-character
# TimedChunk timing (the same alignment tts.py's synthesize() produces),
# never a random guess or a proportional-position estimate.


def _char_chunks(text: str, ms_per_char: int = 100) -> list[TimedChunk]:
    return [TimedChunk(ch, i * ms_per_char, (i + 1) * ms_per_char) for i, ch in enumerate(text)]


def test_suggest_window_uses_real_chunk_timing_for_a_single_char_anchor():
    script = "職場霸凌可能包含孤立"
    chunks = _char_chunks(script)  # each char = 100ms, in order
    idx = script.index("孤")
    start_ms, end_ms = suggest_broll_window_ms(script, "孤", chunks)
    assert start_ms == idx * 100
    assert end_ms == (idx + 1) * 100


def test_suggest_window_spans_a_multi_char_anchor():
    script = "職場霸凌可能包含孤立與壓力"
    chunks = _char_chunks(script)
    idx = script.index("孤立")
    start_ms, end_ms = suggest_broll_window_ms(script, "孤立", chunks)
    assert start_ms == idx * 100
    assert end_ms == (idx + 2) * 100


def test_suggest_window_handles_multi_char_word_boundary_chunks():
    # Real edge-tts WordBoundary events can group multiple characters into
    # one chunk (e.g. "世界") — not always one-char-per-chunk.
    script = "介紹世界的多元文化"
    chunks = [
        TimedChunk("介紹", 0, 400),
        TimedChunk("世界", 400, 900),
        TimedChunk("的多元文化", 900, 2000),
    ]
    start_ms, end_ms = suggest_broll_window_ms(script, "多元文化", chunks)
    assert start_ms == 900
    assert end_ms == 2000


def test_suggest_window_falls_back_to_default_when_anchor_is_empty():
    start_ms, end_ms = suggest_broll_window_ms("任何內容", "", [], default_duration_ms=3000)
    assert (start_ms, end_ms) == (0, 3000)


def test_suggest_window_falls_back_to_default_when_anchor_not_in_script():
    script = "任何內容"
    chunks = _char_chunks(script)
    start_ms, end_ms = suggest_broll_window_ms(script, "不存在", chunks, default_duration_ms=2500)
    assert (start_ms, end_ms) == (0, 2500)
