"""STEP 4: Subtitle (SRT) generation from TTS timing chunks.

A "chunk" is whatever edge-tts's WordBoundary reports as one timed unit — for
CJK text that's usually one character, but the underlying tokenizer sometimes
groups multiple characters into a single boundary event (e.g. "世界"). Chunks
are atomic here: a cut can happen between chunks, never inside one.

Line-splitting follows the "Subtitle Segmentation Engine" design guideline:
candidate break points between chunks are scored (punctuation tier, transition
words, protected spans, plain length fallback), and a run of chunks between
two mandatory breaks (。！？) is split via dynamic programming to maximize
total score subject to every resulting line staying within max_display_width.
Real per-chunk timing (already known, unlike the guideline's TTS-after-the-fact
estimate) lets CPS be scored exactly rather than estimated.
"""

import re
import unicodedata
from dataclasses import dataclass, field

MIN_GAP_MS = 100
HARD_BREAK_CHARS = frozenset("。！？")
STRIP_TRAILING_CHARS = frozenset("。，、；")
KEEP_TRAILING_CHARS = frozenset("？！")

DEFAULT_TRANSITION_WORDS = (
    "因此", "但是", "然而", "如果", "因為", "而且", "並且", "以及", "而",
)

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
_DATE_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
_CHINESE_DATE_RE = re.compile(r"\d+年\d+月\d+日")
_URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
_URL_RE = re.compile(rf"https?://[{_URL_CHARS}]+|www\.[{_URL_CHARS}]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]+")
_ABBREVIATION_RE = re.compile(r"(?:Dr|Mr|Mrs|Ms|Prof|St|etc|vs|e\.g|i\.e)\.")

PROTECTED_SPAN_PATTERNS = (
    _NUMBER_RE, _DATE_RE, _TIME_RE, _CHINESE_DATE_RE, _URL_RE, _EMAIL_RE, _ABBREVIATION_RE,
)

_NEG_INF = float("-inf")


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


@dataclass(frozen=True)
class SegmentationConfig:
    max_display_width: int = 36
    preferred_display_width: int = 28
    length_penalty_coefficient: float = 2.0
    min_duration_ms: int = 1000
    max_duration_ms: int | None = None
    min_gap_ms: int = MIN_GAP_MS
    maximum_cps: float = 12.0
    cps_penalty: float = 50.0
    preferred_break_scores: dict = field(
        default_factory=lambda: {"，": 70, "、": 70, "；": 60, "：": 60}
    )
    transition_words: tuple = DEFAULT_TRANSITION_WORDS
    transition_word_score: float = 40.0
    length_limit_score: float = 10.0
    protected_span_score: float = -100.0


def _display_width_char(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _display_width(text: str) -> int:
    return sum(_display_width_char(c) for c in text)


def _chunk_list_display_width(units: list[TimedChunk]) -> int:
    return sum(_display_width(u.text) for u in units)


def find_protected_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for pattern in PROTECTED_SPAN_PATTERNS:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _is_inside_protected_span(offset: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < offset < end for start, end in spans)


def find_transition_word_offsets(text: str, transition_words: tuple) -> set[int]:
    offsets: set[int] = set()
    for word in transition_words:
        start = 0
        while True:
            idx = text.find(word, start)
            if idx == -1:
                break
            offsets.add(idx)
            start = idx + 1
    return offsets


def _chunk_boundary_offsets(run: list[TimedChunk]) -> list[int]:
    offsets = [0]
    for chunk in run:
        offsets.append(offsets[-1] + len(chunk.text))
    return offsets


def _boundary_score(run, j, boundary_offsets, protected_spans, transition_offsets, config):
    offset = boundary_offsets[j]
    prev_char = run[j - 1].text[-1] if run[j - 1].text else ""
    if prev_char in config.preferred_break_scores:
        return config.preferred_break_scores[prev_char]
    if offset in transition_offsets:
        return config.transition_word_score
    if _is_inside_protected_span(offset, protected_spans):
        return config.protected_span_score
    return config.length_limit_score


def _segment_value(run, i, j, config, boundary_offsets, protected_spans, transition_offsets, is_internal_cut):
    width = _chunk_list_display_width(run[i:j])
    length_score = -abs(width - config.preferred_display_width) * config.length_penalty_coefficient

    duration_ms = run[j - 1].end_ms - run[i].start_ms
    cps_score = 0.0
    if duration_ms > 0:
        real_cps = width / (duration_ms / 1000)
        if real_cps > config.maximum_cps:
            cps_score = -config.cps_penalty

    boundary_score = 0.0
    if is_internal_cut:
        boundary_score = _boundary_score(run, j, boundary_offsets, protected_spans, transition_offsets, config)

    return length_score + cps_score + boundary_score


def _try_segment_run_dp(run, config, boundary_offsets, protected_spans, transition_offsets, allow_protected):
    n = len(run)
    dp_score = [_NEG_INF] * (n + 1)
    dp_prev: list[int | None] = [None] * (n + 1)
    dp_score[0] = 0.0

    def is_excluded(j):
        if j == 0 or j == n:
            return False
        offset = boundary_offsets[j]
        return _is_inside_protected_span(offset, protected_spans) and not allow_protected

    for j in range(1, n + 1):
        if is_excluded(j):
            continue
        for i in range(j):
            if dp_score[i] == _NEG_INF:
                continue
            width = _chunk_list_display_width(run[i:j])
            if width > config.max_display_width:
                continue
            value = _segment_value(
                run, i, j, config, boundary_offsets, protected_spans, transition_offsets, j < n
            )
            score = dp_score[i] + value
            if score > dp_score[j]:
                dp_score[j] = score
                dp_prev[j] = i

    if dp_score[n] == _NEG_INF:
        return None

    cuts = []
    j = n
    while j > 0:
        i = dp_prev[j]
        cuts.append((i, j))
        j = i
    cuts.reverse()
    return [run[i:j] for i, j in cuts]


def _segment_run_greedy_fallback(run: list[TimedChunk], config: SegmentationConfig) -> list[list[TimedChunk]]:
    lines: list[list[TimedChunk]] = []
    buf: list[TimedChunk] = []
    buf_width = 0
    for chunk in run:
        chunk_width = _display_width(chunk.text)
        if buf and buf_width + chunk_width > config.max_display_width:
            lines.append(buf)
            buf = []
            buf_width = 0
        buf.append(chunk)
        buf_width += chunk_width
    if buf:
        lines.append(buf)
    return lines


def _segment_run(run: list[TimedChunk], config: SegmentationConfig) -> list[list[TimedChunk]]:
    if not run:
        return []

    if _chunk_list_display_width(run) <= config.max_display_width:
        return [run]

    joined_text = "".join(c.text for c in run)
    boundary_offsets = _chunk_boundary_offsets(run)
    protected_spans = find_protected_spans(joined_text)
    transition_offsets = find_transition_word_offsets(joined_text, config.transition_words)

    result = _try_segment_run_dp(
        run, config, boundary_offsets, protected_spans, transition_offsets, allow_protected=False
    )
    if result is None:
        result = _try_segment_run_dp(
            run, config, boundary_offsets, protected_spans, transition_offsets, allow_protected=True
        )
    if result is None:
        result = _segment_run_greedy_fallback(run, config)
    return result


def _split_into_lines(chunks: list[TimedChunk], config: SegmentationConfig) -> list[list[TimedChunk]]:
    lines: list[list[TimedChunk]] = []
    run: list[TimedChunk] = []

    for unit in chunks:
        if unit.text in HARD_BREAK_CHARS:
            segments = _segment_run(run, config)
            if segments:
                segments[-1] = segments[-1] + [unit]
                lines.extend(segments)
            else:
                lines.append([unit])
            run = []
            continue
        run.append(unit)

    if run:
        lines.extend(_segment_run(run, config))

    return lines


def _merge_short_lines(
    lines: list[list[TimedChunk]], config: SegmentationConfig
) -> list[list[TimedChunk]]:
    pending = list(lines)
    idx = 0
    while idx < len(pending):
        current = pending[idx]
        while True:
            duration = current[-1].end_ms - current[0].start_ms
            has_next = (idx + 1) < len(pending)
            if duration >= config.min_duration_ms or not has_next:
                break
            nxt = pending[idx + 1]
            if _chunk_list_display_width(current) + _chunk_list_display_width(nxt) > config.max_display_width:
                break
            current = current + nxt
            pending[idx] = current
            del pending[idx + 1]
        idx += 1
    return pending


def _line_text(line: list[TimedChunk]) -> str:
    text = "".join(unit.text for unit in line)
    if text and text[-1] in STRIP_TRAILING_CHARS:
        text = text[:-1]
    return text


def generate_cues(
    chunks: list[TimedChunk],
    start_offset_ms: int = 0,
    config: SegmentationConfig = SegmentationConfig(),
) -> list[SubtitleCue]:
    if not chunks:
        return []

    lines = _split_into_lines(chunks, config)
    lines = _merge_short_lines(lines, config)

    cues = [
        SubtitleCue(
            index=0,
            start_ms=line[0].start_ms + start_offset_ms,
            end_ms=line[-1].end_ms + start_offset_ms,
            text=_line_text(line),
        )
        for line in lines
    ]

    for i in range(len(cues) - 1):
        if cues[i].end_ms > cues[i + 1].start_ms - config.min_gap_ms:
            cues[i] = SubtitleCue(
                index=cues[i].index,
                start_ms=cues[i].start_ms,
                end_ms=cues[i + 1].start_ms - config.min_gap_ms,
                text=cues[i].text,
            )

    return [
        SubtitleCue(index=i + 1, start_ms=c.start_ms, end_ms=c.end_ms, text=c.text)
        for i, c in enumerate(cues)
    ]


def _format_timestamp(ms: int) -> str:
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def cues_to_srt(cues: list[SubtitleCue]) -> str:
    blocks = [
        f"{cue.index}\r\n"
        f"{_format_timestamp(cue.start_ms)} --> {_format_timestamp(cue.end_ms)}\r\n"
        f"{cue.text}"
        for cue in cues
    ]
    return "\r\n\r\n".join(blocks)
