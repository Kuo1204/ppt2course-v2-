import json
from unittest.mock import patch

import pytest

from ppt2course.upload import SlideContent
from ppt2course.visual_analyzer import analyze_visual_needs


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
