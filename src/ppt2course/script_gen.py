"""STEP 2: Generate Script — AI auto-generate / speaker notes / user-provided / AI polish.

Only the two deterministic modes (NOTES, OWN) are implemented so far.
AUTO and POLISH require an LLM call and are scoped for a later session.
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


def _generate_from_notes(slides: list[SlideContent]) -> list[str]:
    return [slide.notes for slide in slides]


def _generate_from_own_texts(slides: list[SlideContent], own_texts: list[str] | None) -> list[str]:
    if own_texts is None:
        raise ScriptGenerationError("own_texts is required for ScriptMode.OWN")
    if len(own_texts) != len(slides):
        raise ScriptGenerationError(
            f"own_texts length ({len(own_texts)}) does not match slide count ({len(slides)})"
        )
    return list(own_texts)


def generate_script(
    mode: ScriptMode,
    slides: list[SlideContent],
    own_texts: list[str] | None = None,
) -> list[str]:
    if mode is ScriptMode.NOTES:
        return _generate_from_notes(slides)

    if mode is ScriptMode.OWN:
        return _generate_from_own_texts(slides, own_texts)

    raise NotImplementedError(f"{mode} is not implemented yet")
