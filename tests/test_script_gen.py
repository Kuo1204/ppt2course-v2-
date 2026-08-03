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


def test_own_mode_returns_own_texts_as_is():
    slides = [slide(1), slide(2)]
    own_texts = ["講稿A", "講稿B"]
    assert generate_script(ScriptMode.OWN, slides, own_texts=own_texts) == ["講稿A", "講稿B"]


def test_own_mode_allows_empty_string_for_a_slide():
    slides = [slide(1), slide(2)]
    own_texts = ["講稿A", ""]
    assert generate_script(ScriptMode.OWN, slides, own_texts=own_texts) == ["講稿A", ""]


def test_own_mode_raises_when_own_texts_missing():
    slides = [slide(1)]
    with pytest.raises(ScriptGenerationError):
        generate_script(ScriptMode.OWN, slides, own_texts=None)


def test_own_mode_raises_when_length_mismatch():
    slides = [slide(1), slide(2)]
    with pytest.raises(ScriptGenerationError):
        generate_script(ScriptMode.OWN, slides, own_texts=["只有一個"])


def test_auto_mode_raises_not_implemented():
    slides = [slide(1)]
    with pytest.raises(NotImplementedError):
        generate_script(ScriptMode.AUTO, slides)


def test_polish_mode_raises_not_implemented():
    slides = [slide(1)]
    with pytest.raises(NotImplementedError):
        generate_script(ScriptMode.POLISH, slides)
