"""STEP 4: Subtitle (SRT) generation from TTS timing chunks.

A "chunk" is whatever edge-tts's WordBoundary reports as one timed unit — for
CJK text that's usually one character, but the underlying tokenizer sometimes
groups multiple characters into a single boundary event (e.g. "世界"). Chunks
are treated as atomic here: a cut can happen between chunks, never inside one.
"""

from dataclasses import dataclass

MAX_CHARS_PER_LINE = 15
MIN_CUE_DURATION_MS = 1000
MIN_GAP_MS = 100
HARD_BREAK_CHARS = frozenset("。！？")
SOFT_BREAK_CHARS = frozenset("，、；")
STRIP_TRAILING_CHARS = frozenset("。，、；")
KEEP_TRAILING_CHARS = frozenset("？！")


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


def _chunk_list_len(units: list[TimedChunk]) -> int:
    return sum(len(u.text) for u in units)


def _split_into_lines(chunks: list[TimedChunk]) -> list[list[TimedChunk]]:
    lines: list[list[TimedChunk]] = []
    buf: list[TimedChunk] = []
    buf_len = 0
    last_soft_idx: int | None = None

    for unit in chunks:
        if unit.text in HARD_BREAK_CHARS:
            buf.append(unit)
            lines.append(buf)
            buf = []
            buf_len = 0
            last_soft_idx = None
            continue

        if buf and buf_len + len(unit.text) > MAX_CHARS_PER_LINE:
            if last_soft_idx is not None:
                lines.append(buf[: last_soft_idx + 1])
                buf = buf[last_soft_idx + 1 :]
                buf_len = _chunk_list_len(buf)
                last_soft_idx = None
            else:
                lines.append(buf)
                buf = []
                buf_len = 0

        buf.append(unit)
        buf_len += len(unit.text)

        if unit.text in SOFT_BREAK_CHARS:
            last_soft_idx = len(buf) - 1

    if buf:
        lines.append(buf)

    return lines


def _merge_short_lines(lines: list[list[TimedChunk]]) -> list[list[TimedChunk]]:
    pending = list(lines)
    idx = 0
    while idx < len(pending):
        current = pending[idx]
        while True:
            duration = current[-1].end_ms - current[0].start_ms
            has_next = (idx + 1) < len(pending)
            if duration >= MIN_CUE_DURATION_MS or not has_next:
                break
            nxt = pending[idx + 1]
            if _chunk_list_len(current) + _chunk_list_len(nxt) > MAX_CHARS_PER_LINE:
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


def generate_cues(chunks: list[TimedChunk], start_offset_ms: int = 0) -> list[SubtitleCue]:
    if not chunks:
        return []

    lines = _split_into_lines(chunks)
    lines = _merge_short_lines(lines)

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
        if cues[i].end_ms > cues[i + 1].start_ms - MIN_GAP_MS:
            cues[i] = SubtitleCue(
                index=cues[i].index,
                start_ms=cues[i].start_ms,
                end_ms=cues[i + 1].start_ms - MIN_GAP_MS,
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
