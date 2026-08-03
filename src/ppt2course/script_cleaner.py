"""STEP 2.5: Script Cleaner — strip blank lines, dividers, markdown, and stray whitespace
before the text reaches TTS, so the synthesizer never reads out formatting noise.
"""

import re

DIVIDER_RE = re.compile(r"^([-_=*~])\1{2,}$")
PAREN_NUMBERED_RE = re.compile(r"^\(\d+\)\s*")
NUMBERED_RE = re.compile(r"^\d+[.\)]\s*")
BULLET_RE = re.compile(r"^[•\-●○]\s+")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
ITALIC_RE = re.compile(r"\*(.+?)\*")
ITALIC_UNDERSCORE_RE = re.compile(r"_(.+?)_")
CODE_RE = re.compile(r"`(.+?)`")
HEADER_RE = re.compile(r"^#+\s*")
WHITESPACE_RUN_RE = re.compile(r"\s+")


def _clean_line(raw: str) -> str:
    if DIVIDER_RE.match(raw.strip()):
        return ""

    # Only lstrip for now: leading-marker regexes (bullet/header) need any
    # trailing whitespace left intact so "- " (bullet, no content) still
    # matches as a bullet instead of losing its trailing space to an early
    # full strip and becoming an unrecognizable lone "-".
    line = raw.lstrip()

    line = PAREN_NUMBERED_RE.sub("", line)
    line = NUMBERED_RE.sub("", line)
    line = BULLET_RE.sub("", line)
    line = HEADER_RE.sub("", line)

    line = BOLD_RE.sub(r"\1", line)
    line = BOLD_UNDERSCORE_RE.sub(r"\1", line)
    line = ITALIC_RE.sub(r"\1", line)
    line = ITALIC_UNDERSCORE_RE.sub(r"\1", line)
    line = CODE_RE.sub(r"\1", line)

    line = WHITESPACE_RUN_RE.sub(" ", line)
    return line.strip()


def clean_script(text: str) -> str:
    lines = (_clean_line(raw) for raw in text.split("\n"))
    return "\n".join(line for line in lines if line)
