"""Optional AI visual advice with a deterministic, non-fatal fallback."""

from dataclasses import dataclass
import json
import re
from typing import Any

from google import genai

from ppt2course.subtitle import TimedChunk
from ppt2course.timeline_models import VisualAssetType, VisualRecommendation
from ppt2course.upload import SlideContent

DEFAULT_VISUAL_MODEL = "gemini-flash-latest"
# Placeholder visual window when no anchor could be timed against real
# narration audio (no anchor found, or the caller skipped TTS entirely) —
# a fixed, clearly-a-default window, never a random guess.
DEFAULT_BROLL_DURATION_MS = 4000
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z -]{2,}|[\u4e00-\u9fff]{2,8}")
_CASE_TERMS = ("案例", "情境", "case", "example", "故事")
_PROCESS_TERMS = ("流程", "步驟", "階段", "process", "workflow")
_SUMMARY_TERMS = ("總結", "重點", "回顧", "summary", "takeaway")
_CHAPTER_TERMS = ("章節", "chapter", "part ", "單元")
_ABSTRACT_TERMS = ("文化", "壓力", "溝通", "信任", "衝突", "孤立", "風險", "價值")
_ALL_TRIGGER_TERMS = _CASE_TERMS + _PROCESS_TERMS + _SUMMARY_TERMS + _CHAPTER_TERMS + _ABSTRACT_TERMS


@dataclass(frozen=True)
class VisualAnalysisResult:
    recommendations: tuple[VisualRecommendation, ...]
    warnings: tuple[str, ...] = ()
    used_ai: bool = False


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def _keywords(slide: SlideContent, script: str, limit: int = 3) -> tuple[str, ...]:
    candidates = _WORD_RE.findall(" ".join((slide.title, slide.text, script)))
    result: list[str] = []
    lowered: set[str] = set()
    for candidate in candidates:
        value = candidate.strip(" -")
        if len(value) >= 2 and value.lower() not in lowered:
            result.append(value)
            lowered.add(value.lower())
        if len(result) == limit:
            break
    return tuple(result) or (slide.title.strip() or f"slide {slide.index}",)


def _script_anchor(script: str) -> str:
    """A short verbatim excerpt of `script` marking where a visual is most
    relevant — prefers one of this module's own trigger terms (the actual
    reason a slide tends to get recommended), falling back to the first
    sizable word-like run in the script. Always a real substring of
    `script` (or "" if nothing in it qualifies) — never invented text.
    """
    lowered_script = script.lower()
    best_index = None
    best_term = ""
    for term in _ALL_TRIGGER_TERMS:
        idx = lowered_script.find(term.lower())
        if idx != -1 and (best_index is None or idx < best_index):
            best_index = idx
            best_term = script[idx : idx + len(term)]
    if best_term:
        return best_term

    match = _WORD_RE.search(script)
    return match.group(0).strip(" -") if match else ""


def _heuristic_recommendation(slide: SlideContent, script: str) -> VisualRecommendation:
    combined = " ".join((slide.title, slide.text, script))
    text_length = len(slide.text.strip())
    score = 20
    reasons: list[str] = []
    if _contains_any(combined, _CASE_TERMS):
        score += 35
        reasons.append("案例或情境內容適合搭配真實場景")
    if _contains_any(combined, _PROCESS_TERMS):
        score += 22
        reasons.append("流程內容可用視覺素材輔助理解")
    if _contains_any(combined, _ABSTRACT_TERMS):
        score += 25
        reasons.append("抽象概念加入情境畫面有助於理解")
    if _contains_any(combined, _SUMMARY_TERMS):
        score += 8
        reasons.append("重點整理可選擇性加強視覺記憶")
    if _contains_any(combined, _CHAPTER_TERMS) or (text_length <= 20 and slide.title):
        score -= 15
        reasons.append("章節頁通常保持簡潔即可")
    if text_length >= 120:
        score += 18
        reasons.append("文字量偏高，視覺切換可降低閱讀負擔")
    if slide.image_count or slide.has_chart:
        score -= 30
        reasons.append("投影片已有圖片或圖表")
    score = max(0, min(100, score))
    if not reasons:
        reasons.append("內容可直接由投影片與旁白呈現")
    return VisualRecommendation(
        slide_number=slide.index,
        title=slide.title or next(iter(slide.text.splitlines()), ""),
        visual_need_score=score,
        recommended=score >= 61,
        reason="；".join(reasons),
        visual_type=VisualAssetType.IMAGE,
        keywords=_keywords(slide, script),
        suggested_position="during_slide",
        script_anchor=_script_anchor(script),
    )


def _prompt(slides: list[SlideContent], scripts: list[str]) -> str:
    payload = [
        {
            "slide_number": slide.index, "title": slide.title, "text": slide.text,
            "script": script, "image_count": slide.image_count,
            "has_chart": slide.has_chart, "text_length": len(slide.text),
        }
        for slide, script in zip(slides, scripts)
    ]
    return (
        "你是教育訓練影片的視覺導演。判斷哪些頁面真的需要額外圖片或 B-roll，"
        "不要每頁都推薦。只回傳 JSON array，每頁一筆，欄位為 slide_number, title, "
        "visual_need_score(0-100), reason, visual_type(image/video), keywords(1-3), "
        "suggested_position(during_slide), script_anchor。script_anchor 必須是從該頁 "
        "script 欄位「逐字」擷取的 2-8 字短語，標記畫面最適合出現在旁白的哪個位置；"
        "找不到適合的位置就回傳空字串，不要自己編造不存在於 script 裡的文字。"
        "recommended 由程式依 61 分門檻決定。\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    fence = chr(96) * 3
    if stripped.startswith(fence):
        stripped = re.sub(
            rf"^{re.escape(fence)}(?:json)?\s*|\s*{re.escape(fence)}$",
            "", stripped, flags=re.IGNORECASE,
        )
    return json.loads(stripped)


def _ai_recommendations(
    slides: list[SlideContent], scripts: list[str], api_key: str, model: str
) -> tuple[VisualRecommendation, ...]:
    response = genai.Client(api_key=api_key).models.generate_content(
        model=model, contents=_prompt(slides, scripts)
    )
    rows = _extract_json(response.text or "")
    if not isinstance(rows, list) or len(rows) != len(slides):
        raise ValueError("Gemini response must contain exactly one row per slide")
    slide_positions = {slide.index: position for position, slide in enumerate(slides)}
    results: list[VisualRecommendation] = []
    seen_numbers: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Gemini recommendation rows must be objects")
        number = int(row["slide_number"])
        if number not in slide_positions or number in seen_numbers:
            raise ValueError(f"Gemini returned invalid or duplicate slide_number {number}")
        seen_numbers.add(number)
        position = slide_positions[number]
        slide = slides[position]
        score = max(0, min(100, int(row["visual_need_score"])))
        visual_type = VisualAssetType(str(row.get("visual_type", "image")).lower())
        keywords = tuple(
            str(item).strip() for item in row.get("keywords", []) if str(item).strip()
        )[:3] or _keywords(slide, scripts[position])
        # Gemini is asked for a verbatim script excerpt, but "asked" isn't
        # "guaranteed" — re-derive locally rather than trust an anchor that
        # turns out not to actually be in this slide's own script text.
        script_text = scripts[position]
        raw_anchor = str(row.get("script_anchor") or "").strip()
        script_anchor = raw_anchor if raw_anchor and raw_anchor in script_text else _script_anchor(
            script_text
        )
        results.append(
            VisualRecommendation(
                slide_number=number, title=str(row.get("title") or slide.title),
                visual_need_score=score, recommended=score >= 61,
                reason=str(row.get("reason") or "AI 視覺分析建議"),
                visual_type=visual_type, keywords=keywords,
                suggested_position=str(row.get("suggested_position") or "during_slide"),
                script_anchor=script_anchor,
            )
        )
    return tuple(sorted(results, key=lambda item: item.slide_number))


def analyze_visual_needs(
    slides: list[SlideContent], scripts: list[str], api_key: str | None = None,
    model: str = DEFAULT_VISUAL_MODEL,
) -> VisualAnalysisResult:
    """Analyze each slide; AI failures safely degrade to local heuristics."""
    if len(slides) != len(scripts):
        raise ValueError("scripts length must match slide count")
    fallback = tuple(
        _heuristic_recommendation(slide, script) for slide, script in zip(slides, scripts)
    )
    if not api_key:
        return VisualAnalysisResult(
            recommendations=fallback,
            warnings=("Gemini API Key 未設定，已使用本機規則產生建議。",),
        )
    try:
        return VisualAnalysisResult(
            recommendations=_ai_recommendations(slides, scripts, api_key, model),
            used_ai=True,
        )
    except Exception as exc:
        return VisualAnalysisResult(
            recommendations=fallback,
            warnings=(f"Gemini 視覺分析失敗，已改用本機規則：{exc}",),
        )


def suggest_broll_window_ms(
    script: str,
    anchor: str,
    chunks: list[TimedChunk],
    default_duration_ms: int = DEFAULT_BROLL_DURATION_MS,
) -> tuple[int, int]:
    """Suggest a slide-local [start_ms, end_ms) B-roll window from where
    ``anchor`` actually falls in ``script``'s real, TTS-produced timing —
    the "Script + Subtitle/word timestamp" approach, not a random guess or a
    proportional-text-position estimate.

    ``chunks`` must be the TimedChunk list ``tts.synthesize()`` produced for
    this exact ``script`` string: tts.py's WordBoundary alignment guarantees
    walking ``chunks`` in order reconstructs ``script`` character-for-
    character, which is what lets this walk character offsets straight into
    real millisecond timestamps.

    Falls back to the very start of the narration when there's no anchor,
    or it isn't actually covered by ``script``/``chunks`` (e.g. mismatched
    inputs) — never raises, this is advisory only. The caller (and, as a
    second line of defense, pipeline.py's own clamp against real audio
    duration) still has the final say.
    """
    if not anchor:
        return (0, default_duration_ms)
    start_idx = script.find(anchor)
    if start_idx == -1:
        return (0, default_duration_ms)
    end_idx = start_idx + len(anchor)

    cursor = 0
    start_ms: int | None = None
    end_ms: int | None = None
    for chunk in chunks:
        chunk_start_idx = cursor
        chunk_end_idx = cursor + len(chunk.text)
        if start_ms is None and chunk_end_idx > start_idx:
            start_ms = chunk.start_ms
        if chunk_start_idx < end_idx:
            end_ms = chunk.end_ms
        cursor = chunk_end_idx
        if cursor >= end_idx:
            break

    if start_ms is None:
        return (0, default_duration_ms)
    if end_ms is None or end_ms <= start_ms:
        end_ms = start_ms + default_duration_ms
    return (start_ms, end_ms)
