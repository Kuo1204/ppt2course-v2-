"""STEP 4: Subtitle (SRT) generation from TTS timing chunks.

Ported from the user's earlier Colab prototype's tiered sentence-splitting
algorithm (chosen explicitly over this project's own DP-based segmenter),
adapted to consume the TimedChunk list ppt2course.tts already produces —
exact per-character timestamps from edge-tts's WordBoundary alignment — so
no SRT-string round-trip / time-interpolation-from-a-parsed-string step is
needed the way the original SubMaker-based version required. Whenever a
char position falls inside a chunk's span, its time is linearly
interpolated across that chunk's [start_ms, end_ms], exactly mirroring the
original's assumption for multi-character WordBoundary events.

Splitting strategy (tiered "Smart Sentence Split"): within a max_chars
window, prefer breaking after a sentence-ending mark ("。！？!?"), then a
clause mark ("，、;:"), then a comma ("，。"); if none exist in-window,
search up to 2x the window for the nearest one; only hard-cut on character
count as a last resort. PROTECTED_PHRASES may never be split down the
middle — every cue stays a single display line even when protecting a
phrase pushes it past max_chars, rather than wrapping onto a second line.
"""

import re
from dataclasses import dataclass

MIN_GAP_MS = 100
CUE_TARGET_MAX_CHARS = 18

# Domain terms (workplace-training content) that must never be split across
# a cue or line boundary — ported verbatim from the original prototype.
PROTECTED_PHRASES = [
    "職場健康", "工作環境", "案例分析", "身心健康", "申訴機制",
    "薪資管理", "教育訓練", "公司制度", "工作要求",
]

_TIER1_PATTERN = re.compile(r"(?<=[。！？!?])")
_TIER2_PATTERN = re.compile(r"(?<=[，、;:])")
_TIER3_PATTERN = re.compile(r"(?<=[，。])")

_TRAILING_PUNCT_PATTERN = re.compile(r"[，。,、；;:.!?]+$")
_LEADING_PUNCT_PATTERN = re.compile(r"^[，。,、；;:.!?]+")


@dataclass(frozen=True)
class TimedChunk:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


def strip_cue_punctuation(text: str) -> str:
    """Removes leading/trailing punctuation from a cue's display text (mid-text
    punctuation, which aids natural reading rhythm, is left untouched)."""
    text = _TRAILING_PUNCT_PATTERN.sub("", text or "").strip()
    text = _LEADING_PUNCT_PATTERN.sub("", text).strip()
    return text


def _is_protected_break(text: str, pos: int) -> bool:
    for phrase in PROTECTED_PHRASES:
        start = 0
        while True:
            idx = text.find(phrase, start)
            if idx == -1:
                break
            if idx < pos < idx + len(phrase):
                return True
            start = idx + 1
    return False


def _shift_off_protected_phrase(text: str, pos: int) -> int:
    for phrase in PROTECTED_PHRASES:
        start = 0
        while True:
            idx = text.find(phrase, start)
            if idx == -1:
                break
            if idx < pos < idx + len(phrase):
                return idx if idx > 0 else idx + len(phrase)
            start = idx + 1
    return pos


def _safe_split_positions(text: str, pattern: re.Pattern) -> list[int]:
    positions = []
    cursor = 0
    for part in pattern.split(text):
        cursor += len(part)
        if 0 < cursor < len(text) and not _is_protected_break(text, cursor):
            positions.append(cursor)
    return positions


def split_text_into_chunks(text: str, max_chars: int = CUE_TARGET_MAX_CHARS) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    tier1 = _safe_split_positions(text, _TIER1_PATTERN)
    tier2 = _safe_split_positions(text, _TIER2_PATTERN)
    tier3 = _safe_split_positions(text, _TIER3_PATTERN)

    chunks = []
    start = 0
    n = len(text)
    while start < n:
        remaining = n - start
        if remaining <= max_chars:
            chunks.append(text[start:])
            break

        window_end = start + max_chars
        best = None
        for positions in (tier1, tier2, tier3):
            valid = [p for p in positions if start < p <= window_end]
            if valid:
                best = max(valid)
                break

        if best is None:
            search_limit = start + max_chars * 2
            for positions in (tier1, tier2, tier3):
                ahead = [p for p in positions if start < p <= search_limit]
                if ahead:
                    best = min(ahead)
                    break

        if best is None:
            best = _shift_off_protected_phrase(text, window_end)
            if best <= start:
                best = window_end

        chunks.append(text[start:best])
        start = best

    return _merge_tiny_trailing_chunks(chunks)


def _merge_tiny_trailing_chunks(chunks: list[str], min_chars: int = 4) -> list[str]:
    if not chunks:
        return chunks

    merged = []
    for c in chunks:
        if merged and len(c) < min_chars:
            merged[-1] = merged[-1] + c
        else:
            merged.append(c)

    if len(merged) >= 2 and len(merged[0]) < min_chars:
        merged = [merged[0] + merged[1]] + merged[2:]

    return merged


def _char_to_time(char_positions: list[tuple[int, int]], chunks: list[TimedChunk], char_pos: int) -> float:
    for i, (s, e) in enumerate(char_positions):
        if s <= char_pos <= e:
            if e == s:
                return chunks[i].start_ms
            frac = (char_pos - s) / (e - s)
            return chunks[i].start_ms + frac * (chunks[i].end_ms - chunks[i].start_ms)
    if char_pos <= 0:
        return chunks[0].start_ms if chunks else 0.0
    return chunks[-1].end_ms if chunks else 0.0


def generate_cues(
    chunks: list[TimedChunk],
    start_offset_ms: int = 0,
    max_chars: int = CUE_TARGET_MAX_CHARS,
) -> list[SubtitleCue]:
    if not chunks:
        return []

    full_text = "".join(c.text for c in chunks)
    if not full_text.strip():
        return []

    char_positions = []
    cursor = 0
    for c in chunks:
        char_positions.append((cursor, cursor + len(c.text)))
        cursor += len(c.text)

    text_chunks = split_text_into_chunks(full_text, max_chars=max_chars)

    cues = []
    cursor = 0
    index = 1
    for chunk_text in text_chunks:
        chunk_start_char = cursor
        chunk_end_char = cursor + len(chunk_text)
        cursor = chunk_end_char

        start_t = _char_to_time(char_positions, chunks, chunk_start_char)
        end_t = _char_to_time(char_positions, chunks, chunk_end_char)

        display_text = strip_cue_punctuation(chunk_text)
        if not display_text:
            continue

        cues.append(
            SubtitleCue(
                index=index,
                start_ms=start_offset_ms + round(start_t),
                end_ms=start_offset_ms + round(end_t),
                text=display_text,
            )
        )
        index += 1

    return cues


def _format_timestamp(ms: int) -> str:
    ms = max(0, ms)
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def cues_to_srt(cues: list[SubtitleCue]) -> str:
    if not cues:
        return ""
    blocks = [
        f"{cue.index}\r\n"
        f"{_format_timestamp(cue.start_ms)} --> {_format_timestamp(cue.end_ms)}\r\n"
        f"{cue.text}"
        for cue in cues
    ]
    return "\r\n\r\n".join(blocks)
