"""STEP 2: Generate Script — AI auto-generate / speaker notes / user-provided / AI polish.

The actual AI call (AUTO, POLISH) happens in the browser using the user's own
Gemini API key — the key never reaches this backend. By the time text arrives
here it is already finalized, so AUTO/OWN/POLISH share the same passthrough
path; only the empty-text validation differs per mode.
"""

from enum import Enum, auto

from ppt2course.upload import SlideContent


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


def generate_script(
    mode: ScriptMode,
    slides: list[SlideContent],
    texts: list[str] | None = None,
) -> list[str]:
    if mode is ScriptMode.NOTES:
        return _generate_from_notes(slides)

    return _generate_from_texts(mode, slides, texts)
