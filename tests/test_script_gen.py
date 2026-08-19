from unittest.mock import patch

import pytest

from ppt2course.script_gen import ScriptGenerationError, ScriptMode, generate_script
from ppt2course.upload import SlideContent


def slide(index, text="slide text", notes=""):
    return SlideContent(index=index, text=text, notes=notes)


# ---- NOTES / OWN (unchanged, deterministic passthrough) ----

def test_notes_mode_returns_notes_for_each_slide():
    slides = [slide(1, notes="備忘稿1"), slide(2, notes="備忘稿2")]
    assert generate_script(ScriptMode.NOTES, slides) == ["備忘稿1", "備忘稿2"]


def test_notes_mode_empty_notes_returns_empty_string_for_that_slide():
    slides = [slide(1, notes=""), slide(2, notes="備忘稿2")]
    assert generate_script(ScriptMode.NOTES, slides) == ["", "備忘稿2"]


def test_notes_mode_empty_slides_list_returns_empty_list():
    assert generate_script(ScriptMode.NOTES, []) == []


def test_own_mode_returns_texts_as_is():
    slides = [slide(1), slide(2)]
    texts = ["講稿A", "講稿B"]
    assert generate_script(ScriptMode.OWN, slides, texts=texts) == ["講稿A", "講稿B"]


def test_own_mode_allows_empty_string_for_a_slide():
    slides = [slide(1), slide(2)]
    texts = ["講稿A", ""]
    assert generate_script(ScriptMode.OWN, slides, texts=texts) == ["講稿A", ""]


def test_own_mode_raises_when_texts_missing():
    slides = [slide(1)]
    with pytest.raises(ScriptGenerationError):
        generate_script(ScriptMode.OWN, slides, texts=None)


def test_own_mode_raises_when_length_mismatch():
    slides = [slide(1), slide(2)]
    with pytest.raises(ScriptGenerationError):
        generate_script(ScriptMode.OWN, slides, texts=["只有一個"])


# ---- AUTO / POLISH: real backend Gemini calls (mocked) ----

class FakeGenaiModels:
    def __init__(self, results):
        # results: list of either a string (success) or an Exception instance (to raise).
        # NOT copied: a new genai.Client() (and thus a new FakeGenaiModels) is
        # constructed on every _call_gemini() invocation, so the same queue
        # must be shared/mutated across all of them within one test.
        self.results = results
        self.calls = []

    def generate_content(self, model, contents):
        self.calls.append({"model": model, "contents": contents})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result

        class FakeResponse:
            pass

        r = FakeResponse()
        r.text = result
        return r


class FakeGenaiClient:
    def __init__(self, results):
        self.models = FakeGenaiModels(results)

    @classmethod
    def factory(cls, results):
        return lambda api_key: cls(results)


def _patch_genai(results):
    return patch(
        "ppt2course.script_gen.genai.Client",
        side_effect=FakeGenaiClient.factory(results),
    )


def test_auto_mode_calls_gemini_per_slide_and_returns_scripts():
    slides = [slide(1, text="投影片一內容"), slide(2, text="投影片二內容")]
    with _patch_genai(["AI講稿一", "AI講稿二"]):
        result = generate_script(ScriptMode.AUTO, slides, api_key="fake-key")
    assert result == ["AI講稿一", "AI講稿二"]


def test_auto_mode_raises_without_api_key():
    slides = [slide(1)]
    with pytest.raises(ScriptGenerationError):
        generate_script(ScriptMode.AUTO, slides, api_key=None)


def test_auto_mode_raises_on_empty_gemini_response():
    slides = [slide(1)]
    with _patch_genai([""]):
        with pytest.raises(ScriptGenerationError):
            generate_script(ScriptMode.AUTO, slides, api_key="fake-key")


def test_polish_mode_calls_gemini_per_slide_and_returns_polished_scripts():
    slides = [slide(1), slide(2)]
    texts = ["原始講稿一", "原始講稿二"]
    with _patch_genai(["修飾後講稿一", "修飾後講稿二"]):
        result = generate_script(ScriptMode.POLISH, slides, texts=texts, api_key="fake-key")
    assert result == ["修飾後講稿一", "修飾後講稿二"]


def test_polish_mode_prompt_contains_original_text():
    slides = [slide(1)]
    texts = ["請修飾我的講稿"]
    captured = {}

    def fake_ctor(api_key):
        client = FakeGenaiClient(["修飾後的講稿"])
        captured["client"] = client
        return client

    with patch("ppt2course.script_gen.genai.Client", side_effect=fake_ctor):
        generate_script(ScriptMode.POLISH, slides, texts=texts, api_key="fake-key")

    assert "請修飾我的講稿" in captured["client"].models.calls[0]["contents"]


def test_polish_mode_raises_without_api_key():
    slides = [slide(1)]
    with pytest.raises(ScriptGenerationError):
        generate_script(ScriptMode.POLISH, slides, texts=["文字"], api_key=None)


def test_polish_mode_raises_when_texts_missing():
    slides = [slide(1)]
    with pytest.raises(ScriptGenerationError):
        generate_script(ScriptMode.POLISH, slides, texts=None, api_key="fake-key")


def test_polish_mode_raises_when_length_mismatch():
    slides = [slide(1), slide(2)]
    with pytest.raises(ScriptGenerationError):
        generate_script(ScriptMode.POLISH, slides, texts=["只有一個"], api_key="fake-key")


def test_polish_mode_raises_on_empty_input_text():
    slides = [slide(1), slide(2)]
    with pytest.raises(ScriptGenerationError):
        generate_script(ScriptMode.POLISH, slides, texts=["有內容", ""], api_key="fake-key")


def test_polish_mode_raises_on_empty_gemini_response():
    slides = [slide(1)]
    with _patch_genai([""]):
        with pytest.raises(ScriptGenerationError):
            generate_script(ScriptMode.POLISH, slides, texts=["原始文字"], api_key="fake-key")


# ---- retry-on-rate-limit behavior ----

def test_retries_on_rate_limit_error_then_succeeds():
    slides = [slide(1)]
    rate_limit_error = Exception('429 RESOURCE_EXHAUSTED {"retryDelay": "1s"}')
    with _patch_genai([rate_limit_error, "終於成功了"]):
        with patch("ppt2course.script_gen.time.sleep") as mock_sleep:
            result = generate_script(ScriptMode.AUTO, slides, api_key="fake-key")
    assert result == ["終於成功了"]
    mock_sleep.assert_called_once()


def test_raises_after_exhausting_retries_on_persistent_rate_limit():
    slides = [slide(1)]
    rate_limit_error = Exception("429 RESOURCE_EXHAUSTED")
    with _patch_genai([rate_limit_error] * 5):
        with patch("ppt2course.script_gen.time.sleep"):
            with pytest.raises(ScriptGenerationError):
                generate_script(ScriptMode.AUTO, slides, api_key="fake-key")


def test_non_rate_limit_error_raises_immediately_without_retry():
    slides = [slide(1)]
    other_error = Exception("PERMISSION_DENIED: invalid API key")
    with _patch_genai([other_error, "should not reach here"]):
        with patch("ppt2course.script_gen.time.sleep") as mock_sleep:
            with pytest.raises(ScriptGenerationError):
                generate_script(ScriptMode.AUTO, slides, api_key="fake-key")
    mock_sleep.assert_not_called()


# ---- retry-on-transient-server-error behavior (503 UNAVAILABLE etc.) ----
#
# Regression coverage for a real user report: "script generation failed:
# Gemini API call failed: 503 UNAVAILABLE ... This model is currently
# experiencing high demand ... Please try again later." — that message is
# Google's own description of a transient condition, but the old retry
# logic only ever recognized 429/RESOURCE_EXHAUSTED and raised immediately
# on everything else, including this one.

def test_retries_on_503_unavailable_then_succeeds():
    slides = [slide(1)]
    overloaded_error = Exception(
        "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is "
        "currently experiencing high demand. Spikes in demand are usually "
        "temporary. Please try again later.', 'status': 'UNAVAILABLE'}}"
    )
    with _patch_genai([overloaded_error, "終於成功了"]):
        with patch("ppt2course.script_gen.time.sleep") as mock_sleep:
            result = generate_script(ScriptMode.AUTO, slides, api_key="fake-key")
    assert result == ["終於成功了"]
    mock_sleep.assert_called_once()


def test_retries_on_internal_and_deadline_exceeded_errors():
    slides = [slide(1)]
    internal_error = Exception("500 INTERNAL")
    deadline_error = Exception("504 DEADLINE_EXCEEDED")
    with _patch_genai([internal_error, deadline_error, "成功"]):
        with patch("ppt2course.script_gen.time.sleep"):
            result = generate_script(ScriptMode.AUTO, slides, api_key="fake-key")
    assert result == ["成功"]


def test_raises_after_exhausting_retries_on_persistent_503():
    slides = [slide(1)]
    overloaded_error = Exception("503 UNAVAILABLE")
    with _patch_genai([overloaded_error] * 5):
        with patch("ppt2course.script_gen.time.sleep"):
            with pytest.raises(ScriptGenerationError, match="503 UNAVAILABLE"):
                generate_script(ScriptMode.AUTO, slides, api_key="fake-key")


def test_503_retry_uses_capped_exponential_backoff_not_retry_delay_parsing():
    # No retryDelay field in a 503 response (unlike 429's quota metadata) --
    # the backoff schedule is a fixed capped exponential, not parsed from
    # the error text.
    slides = [slide(1)]
    overloaded_error = Exception("503 UNAVAILABLE")
    with _patch_genai([overloaded_error, overloaded_error, overloaded_error, "成功"]):
        with patch("ppt2course.script_gen.time.sleep") as mock_sleep:
            generate_script(ScriptMode.AUTO, slides, api_key="fake-key")
    assert [c.args[0] for c in mock_sleep.call_args_list] == [5, 10, 20]
