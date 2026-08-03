"""STEP 4: Subtitle (SRT) generation from character-level TTS timestamps."""

from dataclasses import dataclass

MAX_CHARS_PER_LINE = 15
MIN_CUE_DURATION_MS = 1000
MIN_GAP_MS = 100
HARD_BREAK_CHARS = frozenset("。！？")
SOFT_BREAK_CHARS = frozenset("，、；")
STRIP_TRAILING_CHARS = frozenset("。，、；")
KEEP_TRAILING_CHARS = frozenset("？！")
CONTINUITY_BACKTRACK_LIMIT = 5


@dataclass(frozen=True)
class CharTiming:
    char: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


def _is_alnum(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _split_into_lines(chars: list[CharTiming]) -> list[list[CharTiming]]:
    lines: list[list[CharTiming]] = []
    buf: list[CharTiming] = []
    last_soft_idx: int | None = None

    for unit in chars:
        buf.append(unit)

        if unit.char in HARD_BREAK_CHARS:
            lines.append(buf)
            buf = []
            last_soft_idx = None
            continue

        if unit.char in SOFT_BREAK_CHARS:
            last_soft_idx = len(buf) - 1

        if len(buf) > MAX_CHARS_PER_LINE:
            if last_soft_idx is not None:
                lines.append(buf[: last_soft_idx + 1])
                buf = buf[last_soft_idx + 1 :]
                last_soft_idx = None
            else:
                cut = _find_continuity_boundary(buf)
                lines.append(buf[:cut])
                buf = buf[cut:]

    if buf:
        lines.append(buf)

    return lines


def _find_continuity_boundary(buf: list[CharTiming]) -> int:
    for back in range(CONTINUITY_BACKTRACK_LIMIT):
        pos = MAX_CHARS_PER_LINE - back
        if pos <= 0:
            break
        left = buf[pos - 1].char
        right = buf[pos].char
        if not (_is_alnum(left) and _is_alnum(right)):
            return pos
    return MAX_CHARS_PER_LINE


def _merge_short_lines(lines: list[list[CharTiming]]) -> list[list[CharTiming]]:
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
            if len(current) + len(nxt) > MAX_CHARS_PER_LINE:
                break
            current = current + nxt
            pending[idx] = current
            del pending[idx + 1]
        idx += 1
    return pending


def _line_text(line: list[CharTiming]) -> str:
    text = "".join(unit.char for unit in line)
    if text and text[-1] in STRIP_TRAILING_CHARS:
        text = text[:-1]
    return text


def generate_cues(chars: list[CharTiming], start_offset_ms: int = 0) -> list[SubtitleCue]:
    if not chars:
        return []

    lines = _split_into_lines(chars)
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
