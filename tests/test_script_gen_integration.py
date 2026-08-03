"""Integration test against the real Gemini API (STEP2 AUTO/POLISH).

Requires a real key in the GEMINI_API_KEY environment variable. Skips
gracefully if not set — never paste an API key into a test file or chat.
"""

import os

import pytest

from ppt2course.script_gen import ScriptMode, generate_script
from ppt2course.upload import SlideContent

API_KEY = os.environ.get("GEMINI_API_KEY")

pytestmark = pytest.mark.skipif(not API_KEY, reason="GEMINI_API_KEY not set")


def test_auto_mode_generates_real_script_from_gemini():
    slides = [SlideContent(index=1, text="資訊安全：密碼管理最佳實務", notes="")]
    result = generate_script(ScriptMode.AUTO, slides, api_key=API_KEY)
    assert len(result) == 1
    assert result[0].strip()


def test_polish_mode_polishes_real_script_via_gemini():
    slides = [SlideContent(index=1, text="", notes="")]
    original = "大家好。今天要講密碼安全。密碼要夠長。不要重複使用。謝謝。"
    result = generate_script(ScriptMode.POLISH, slides, texts=[original], api_key=API_KEY)
    assert len(result) == 1
    assert result[0].strip()
