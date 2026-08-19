"""STEP 2: Generate Script — AI auto-generate / speaker notes / user-provided / AI polish.

AUTO and POLISH call Google's Gemini API directly from this backend — the
user's API key is passed in per-call and used here, it is not expected to be
handled client-side. NOTES/OWN stay fully deterministic and never touch the
network.
"""

import re
import time
from enum import Enum, auto

from google import genai

from ppt2course.upload import SlideContent

DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
MAX_RETRIES = 5


class ScriptMode(Enum):
    AUTO = auto()
    NOTES = auto()
    OWN = auto()
    POLISH = auto()


class ScriptGenerationError(Exception):
    pass


# AI-produced text (AUTO, POLISH) must not be blank — an AI call that
# returned nothing is a failure, unlike OWN where a user may deliberately
# leave a slide silent.
_REQUIRES_NON_EMPTY = {ScriptMode.AUTO, ScriptMode.POLISH}

_AUTO_PROMPT_TEMPLATE = """你是一位企業教育訓練講師。
請依照以下投影片內容,撰寫約30~60秒的口語化教學講稿。

要求:
- 不要逐字唸投影片文字,要用自然口語表達,像真人講師在課堂上說話
- 保留專有名詞、法規名稱的正確用字,不可竄改
- 使用繁體中文
- 只輸出講稿本身文字,不要加任何標題、說明或引言

投影片內容:
{content}
"""

_POLISH_PROMPT_TEMPLATE = """你是一位教育訓練講師的講稿修飾助手。
請將以下講稿修飾得更口語自然,適合教育訓練講師實際講課使用。

要求:
- 不要改變原意,不要增加或刪減重要資訊(例如法規名稱、數字、步驟)
- 只是把語氣改得更自然、更口語化,去除生硬的書面用語
- 使用繁體中文
- 只輸出修飾後的講稿文字,不要加任何說明或標題

原始講稿:
{content}
"""


def _generate_from_notes(slides: list[SlideContent]) -> list[str]:
    return [slide.notes for slide in slides]


def _generate_from_texts(
    mode: ScriptMode, slides: list[SlideContent], texts: list[str] | None
) -> list[str]:
    if texts is None:
        raise ScriptGenerationError(f"texts is required for {mode}")
    if len(texts) != len(slides):
        raise ScriptGenerationError(
            f"texts length ({len(texts)}) does not match slide count ({len(slides)})"
        )
    if mode in _REQUIRES_NON_EMPTY:
        for slide, text in zip(slides, texts):
            if not text.strip():
                raise ScriptGenerationError(
                    f"{mode} does not allow empty text (slide index {slide.index})"
                )
    return list(texts)


def _extract_retry_delay(error_str: str, default_seconds: int = 20) -> int:
    match = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+)", error_str)
    if match:
        return int(match.group(1)) + 2
    return default_seconds


def _is_rate_limit_error(error_str: str) -> bool:
    return "RESOURCE_EXHAUSTED" in error_str or "429" in error_str


# Google's own docs describe these as transient server-side conditions
# ("usually temporary... please try again later" is the literal 503
# message) — worth an automatic retry, unlike e.g. PERMISSION_DENIED or
# INVALID_ARGUMENT, which won't fix themselves no matter how many times
# you ask.
_TRANSIENT_SERVER_ERROR_MARKERS = ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED")


def _is_transient_server_error(error_str: str) -> bool:
    return any(marker in error_str for marker in _TRANSIENT_SERVER_ERROR_MARKERS)


def _transient_backoff_seconds(attempt: int) -> int:
    # Unlike a 429's quota response, these carry no retryDelay hint to
    # parse -- a capped exponential backoff (5s, 10s, 20s, 30s, 30s...)
    # gives a transient outage a real chance to clear without hammering
    # an already-overloaded model.
    return min(5 * (2 ** (attempt - 1)), 30)


def _call_gemini(
    prompt: str,
    api_key: str,
    model: str = DEFAULT_GEMINI_MODEL,
    max_retries: int = MAX_RETRIES,
) -> str:
    client = genai.Client(api_key=api_key)

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            return (response.text or "").strip()
        except Exception as exc:
            last_error = exc
            error_str = str(exc)
            if attempt < max_retries:
                if _is_rate_limit_error(error_str):
                    time.sleep(_extract_retry_delay(error_str))
                    continue
                if _is_transient_server_error(error_str):
                    time.sleep(_transient_backoff_seconds(attempt))
                    continue
            raise ScriptGenerationError(f"Gemini API call failed: {exc}") from exc

    raise ScriptGenerationError(f"Gemini API call failed: {last_error}") from last_error


def _generate_auto(
    slides: list[SlideContent], api_key: str | None, model: str
) -> list[str]:
    if not api_key:
        raise ScriptGenerationError("api_key is required for ScriptMode.AUTO")

    results = []
    for slide in slides:
        prompt = _AUTO_PROMPT_TEMPLATE.format(content=slide.text)
        text = _call_gemini(prompt, api_key, model)
        if not text.strip():
            raise ScriptGenerationError(
                f"Gemini returned an empty script for slide index {slide.index}"
            )
        results.append(text)
    return results


def _generate_polish(
    slides: list[SlideContent],
    texts: list[str] | None,
    api_key: str | None,
    model: str,
) -> list[str]:
    if not api_key:
        raise ScriptGenerationError("api_key is required for ScriptMode.POLISH")
    if texts is None:
        raise ScriptGenerationError("texts is required for ScriptMode.POLISH")
    if len(texts) != len(slides):
        raise ScriptGenerationError(
            f"texts length ({len(texts)}) does not match slide count ({len(slides)})"
        )

    results = []
    for slide, text in zip(slides, texts):
        if not text.strip():
            raise ScriptGenerationError(
                f"POLISH does not allow empty input text (slide index {slide.index})"
            )
        prompt = _POLISH_PROMPT_TEMPLATE.format(content=text)
        polished = _call_gemini(prompt, api_key, model)
        if not polished.strip():
            raise ScriptGenerationError(
                f"Gemini returned an empty polished script for slide index {slide.index}"
            )
        results.append(polished)
    return results


def generate_script(
    mode: ScriptMode,
    slides: list[SlideContent],
    texts: list[str] | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_GEMINI_MODEL,
) -> list[str]:
    if mode is ScriptMode.NOTES:
        return _generate_from_notes(slides)

    if mode is ScriptMode.OWN:
        return _generate_from_texts(mode, slides, texts)

    if mode is ScriptMode.AUTO:
        return _generate_auto(slides, api_key, model)

    return _generate_polish(slides, texts, api_key, model)
