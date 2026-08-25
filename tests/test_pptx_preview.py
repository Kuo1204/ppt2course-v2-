"""Tests for the STEP 1 preview helper (approximate, LibreOffice-free
rendering straight out of python-pptx's own shape data)."""

import io

import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Inches, Pt

from ppt2course.pptx_preview import PptxPreviewError, render_pptx_thumbnails

BLANK_LAYOUT = 6


def _make_pptx(tmp_path, slide_texts, name="deck.pptx"):
    prs = Presentation()
    for text in slide_texts:
        slide = prs.slides.add_slide(prs.slide_layouts[BLANK_LAYOUT])
        box = slide.shapes.add_textbox(Pt(50), Pt(50), Pt(400), Pt(80))
        box.text_frame.text = text
    path = tmp_path / name
    prs.save(str(path))
    return str(path)


def _make_solid_png_bytes(color=(0, 120, 200), size=(40, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_renders_one_png_thumbnail_per_slide_in_order(tmp_path):
    path = _make_pptx(tmp_path, ["第一頁", "第二頁", "第三頁"])

    thumbnails = render_pptx_thumbnails(path)

    assert len(thumbnails) == 3
    for png_bytes in thumbnails:
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG file signature


def test_thumbnail_width_matches_requested_width(tmp_path):
    path = _make_pptx(tmp_path, ["單頁投影片"])

    thumbnails = render_pptx_thumbnails(path, thumbnail_width=320)

    import fitz

    pix = fitz.Pixmap(thumbnails[0])
    assert pix.width == 320


def test_single_slide_deck_returns_one_thumbnail(tmp_path):
    path = _make_pptx(tmp_path, ["只有一頁"])

    thumbnails = render_pptx_thumbnails(path)

    assert len(thumbnails) == 1


def test_raises_pptx_preview_error_for_a_corrupt_file(tmp_path):
    bad = tmp_path / "not_really_a_pptx.pptx"
    bad.write_bytes(bytes(range(256)) * 20)

    with pytest.raises(PptxPreviewError):
        render_pptx_thumbnails(str(bad))


def test_different_slides_produce_different_thumbnails(tmp_path):
    """Each slide's own text must actually reach its own page -- this is
    the case the old is_reflowable-mode PyMuPDF PPTX opener silently got
    wrong (every slide's text got flattened onto a single page)."""
    path = _make_pptx(tmp_path, ["ONLY ON SLIDE ONE", "ONLY ON SLIDE TWO"])

    thumbnails = render_pptx_thumbnails(path)

    assert len(thumbnails) == 2
    assert thumbnails[0] != thumbnails[1]


def test_embedded_picture_is_drawn_into_the_thumbnail(tmp_path):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[BLANK_LAYOUT])
    image_path = tmp_path / "pic.png"
    image_path.write_bytes(_make_solid_png_bytes())
    slide.shapes.add_picture(str(image_path), Inches(1), Inches(1), width=Inches(2))
    pptx_path = tmp_path / "with_picture.pptx"
    prs.save(str(pptx_path))

    thumbnails = render_pptx_thumbnails(str(pptx_path))

    assert len(thumbnails) == 1

    import fitz

    pix = fitz.Pixmap(thumbnails[0])
    # The picture was placed with a distinct solid color on an otherwise
    # blank (white) slide -- somewhere in the pixmap should be a non-white
    # pixel roughly matching it, proving the image actually got composited
    # in rather than silently skipped.
    samples = pix.samples
    found_non_white = any(
        samples[i] != 255 or samples[i + 1] != 255 or samples[i + 2] != 255
        for i in range(0, len(samples), pix.n)
    )
    assert found_non_white


def test_title_and_subtitle_placeholders_are_rendered(tmp_path):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "職場霸凌防治重點介紹"
    slide.placeholders[1].text = "副標題示範文字"
    pptx_path = tmp_path / "title_slide.pptx"
    prs.save(str(pptx_path))

    thumbnails = render_pptx_thumbnails(str(pptx_path))

    assert len(thumbnails) == 1
    assert thumbnails[0][:8] == b"\x89PNG\r\n\x1a\n"
