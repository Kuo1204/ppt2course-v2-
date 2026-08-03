import pytest

from ppt2course.script_gen import ScriptGenerationError, ScriptMode, generate_script
from ppt2course.upload import SlideContent


def slide(index, notes=""):
    return SlideContent(index=index, text=f"slide {index} text", notes=notes)


def test_notes_mode_returns_notes_for_each_slide():
    slides = [slide(1, notes="備忘稿1"), slide(2, notes="備忘稿2")]
    assert generate_script(ScriptMode.NOTES, slides) == ["備忘稿1", "備忘稿2"]


def test_notes_mode_empty_notes_returns_empty_string_for_that_slide():
    slides = [slide(1, notes=""), slide(2, notes="備忘稿2")]
    assert generate_script(ScriptMode.NOTES, slides) == ["", "備忘稿2"]


def test_notes_mode_empty_slides_list_returns_empty_list():
    assert generate_script(ScriptMode.NOTES, []) == []


@pytest.mark.parametrize("mode", [ScriptMode.OWN, ScriptMode.AUTO, ScriptMode.POLISH])
def test_passthrough_modes_return_texts_as_is(mode):
    slides = [slide(1), slide(2)]
    texts = ["講稿A", "講稿B"]
    assert generate_script(mode, slides, texts=texts) == ["講稿A", "講稿B"]


@pytest.mark.parametrize("mode", [ScriptMode.OWN, ScriptMode.AUTO, ScriptMode.POLISH])
def test_passthrough_modes_raise_when_texts_missing(mode):
    slides = [slide(1)]
    with pytest.raises(ScriptGenerationError):
        generate_script(mode, slides, texts=None)


@pytest.mark.parametrize("mode", [ScriptMode.OWN, ScriptMode.AUTO, ScriptMode.POLISH])
def test_passthrough_modes_raise_when_length_mismatch(mode):
    slides = [slide(1), slide(2)]
    with pytest.raises(ScriptGenerationError):
        generate_script(mode, slides, texts=["只有一個"])


def test_own_mode_allows_empty_string_for_a_slide():
    slides = [slide(1), slide(2)]
    texts = ["講稿A", ""]
    assert generate_script(ScriptMode.OWN, slides, texts=texts) == ["講稿A", ""]


@pytest.mark.parametrize("mode", [ScriptMode.AUTO, ScriptMode.POLISH])
def test_ai_modes_reject_empty_string(mode):
    slides = [slide(1), slide(2)]
    texts = ["講稿A", ""]
    with pytest.raises(ScriptGenerationError):
        generate_script(mode, slides, texts=texts)


@pytest.mark.parametrize("mode", [ScriptMode.AUTO, ScriptMode.POLISH])
def test_ai_modes_reject_whitespace_only_string(mode):
    slides = [slide(1), slide(2)]
    texts = ["講稿A", "   "]
    with pytest.raises(ScriptGenerationError):
        generate_script(mode, slides, texts=texts)
