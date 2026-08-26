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

# AUTO/POLISH used to call Gemini once *per slide* -- for an N-slide deck
# that's N requests against the API's own per-day quota (the free tier is as
# low as 20 requests/day/model), so a single 10-slide generation could burn
# half a day's quota by itself. Batching every slide into one prompt/one
# response (see _AUTO_BATCH_PROMPT_TEMPLATE / _POLISH_BATCH_PROMPT_TEMPLATE
# and _parse_batch_response below) turns a whole deck into exactly one
# request regardless of slide count. This used to also run every slide
# concurrently to keep total wall-clock under the platform's gateway
# timeout; that concern is now handled at the HTTP layer instead (see
# preview_jobs.py's submit-then-poll), so a single request taking as long as
# the whole deck used to collectively take is no longer a problem here.


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

_AUTO_BATCH_PROMPT_TEMPLATE = """你是一位企業教育訓練講師。
請依照以下每一頁投影片的內容,各自撰寫約30~60秒的口語化教學講稿。

要求:
- 不要逐字唸投影片文字,要用自然口語表達,像真人講師在課堂上說話
- 保留專有名詞、法規名稱的正確用字,不可竄改
- 使用繁體中文
- 每一頁的講稿前面都必須先單獨一行輸出「===SLIDE {{n}}===」(n 為下方對應的投影片編號),
  接著才是該頁講稿本身;不要加任何其他標題、說明或引言;不可省略任何一頁

投影片內容(共 {count} 頁):

{sections}
"""

_POLISH_BATCH_PROMPT_TEMPLATE = """你是一位教育訓練講師的講稿修飾助手。
請將以下每一頁講稿分別修飾得更口語自然,適合教育訓練講師實際講課使用。

要求:
- 不要改變原意,不要增加或刪減重要資訊(例如法規名稱、數字、步驟)
- 只是把語氣改得更自然、更口語化,去除生硬的書面用語
- 使用繁體中文
- 每一頁修飾後的講稿前面都必須先單獨一行輸出「===SLIDE {{n}}===」(n 為下方對應的投影片編號),
  接著才是該頁講稿本身;不要加任何其他說明或標題;不可省略任何一頁

原始講稿(共 {count} 頁):

{sections}
"""

# Matches the "===SLIDE {n}===" markers the prompts above ask Gemini to
# prefix each slide's script with -- tolerant of whitespace variance since
# it's parsing free-form model output, not a format we control on both ends.
_SLIDE_MARKER_RE = re.compile(r"===\s*SLIDE\s+(\d+)\s*===", re.IGNORECASE)


def _build_batch_sections(slides: list[SlideContent], contents: list[str]) -> str:
    return "\n\n".join(
        f"===SLIDE {slide.index}===\n{content}" for slide, content in zip(slides, contents)
    )


def _parse_batch_response(response_text: str, slides: list[SlideContent]) -> list[str]:
    """Splits one batched Gemini response back out into one script per slide,
    keyed by the "===SLIDE {n}===" markers the prompt asked for -- returned
    in ``slides`` order regardless of what order the model produced them in.
    Raises if the model dropped a slide rather than silently returning a
    short result list (which would then get zipped against the wrong
    slides)."""
    parts = _SLIDE_MARKER_RE.split(response_text)
    by_index: dict[int, str] = {}
    # re.split on a pattern with one capture group returns
    # [text_before_first_match, group1, text1, group2, text2, ...] -- the
    # leading chunk (parts[0], anything before the first marker) is discarded.
    for i in range(1, len(parts), 2):
        by_index[int(parts[i])] = parts[i + 1].strip()

    missing = [slide.index for slide in slides if slide.index not in by_index]
    if missing:
        raise ScriptGenerationError(
            f"Gemini response did not include a script for slide index(es): {missing}"
        )
    return [by_index[slide.index] for slide in slides]


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
    if not slides:
        return []

    sections = _build_batch_sections(slides, [slide.text for slide in slides])
    prompt = _AUTO_BATCH_PROMPT_TEMPLATE.format(count=len(slides), sections=sections)
    response_text = _call_gemini(prompt, api_key, model)
    results = _parse_batch_response(response_text, slides)
    for slide, text in zip(slides, results):
        if not text.strip():
            raise ScriptGenerationError(
                f"Gemini returned an empty script for slide index {slide.index}"
            )
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
    if not slides:
        return []

    for slide, text in zip(slides, texts):
        if not text.strip():
            raise ScriptGenerationError(
                f"POLISH does not allow empty input text (slide index {slide.index})"
            )

    sections = _build_batch_sections(slides, texts)
    prompt = _POLISH_BATCH_PROMPT_TEMPLATE.format(count=len(slides), sections=sections)
    response_text = _call_gemini(prompt, api_key, model)
    results = _parse_batch_response(response_text, slides)
    for slide, polished in zip(slides, results):
        if not polished.strip():
            raise ScriptGenerationError(
                f"Gemini returned an empty polished script for slide index {slide.index}"
            )
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
