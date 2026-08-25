"""STEP 1 preview helper: render approximate slide thumbnails from an
uploaded .pptx so the upload step can show a visual preview (matching the
per-slide image upload field's thumbnail strip) instead of just a filename.

This is purely a UI convenience — it never touches the render pipeline's
own output and nothing it produces is persisted.

No external renderer (LibreOffice, PowerPoint, ...) is used or required:
this platform's containers can't reliably install one (see git history for
why), so instead each slide is redrawn from scratch straight out of
python-pptx's own shape data (text, pictures, solid fills) onto a fresh
PyMuPDF page, using PyMuPDF's built-in CJK-capable fallback font. This is
an *approximation* of the real slide — font metrics, exact wrapping,
gradients/shadows, charts, tables, SmartArt and nested group shapes are not
reproduced faithfully (most are skipped outright) — good enough for "does
this look like the deck I meant to upload", not a substitute for opening
the file in PowerPoint.
"""

import fitz
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.exc import PackageNotFoundError

DEFAULT_THUMBNAIL_WIDTH = 480

# EMUs (English Metric Units) are python-pptx's native length unit for shape
# geometry (914400 per inch); PyMuPDF page/shape coordinates are in points
# (72 per inch). Every position/size below is converted EMU -> points using
# this ratio (then further scaled to the requested thumbnail width).
_POINTS_PER_EMU = 72 / 914400

# Built into PyMuPDF/MuPDF itself (a bundled CJK fallback font, not a system
# font) -- renders Traditional/Simplified Chinese, Japanese and Korean
# without needing any font file on disk.
_CJK_FONTNAME = "china-ss"

_DEFAULT_FONT_SIZE_PT = 18
_FALLBACK_FILL_COLOR = (0.85, 0.85, 0.85)  # light gray, for shapes with a
# solid fill but no text/picture (plain rectangles, lines used as dividers)


class PptxPreviewError(Exception):
    pass


def _shape_rect_pt(shape, scale: float) -> "fitz.Rect":
    left = (shape.left or 0) * _POINTS_PER_EMU * scale
    top = (shape.top or 0) * _POINTS_PER_EMU * scale
    width = (shape.width or 0) * _POINTS_PER_EMU * scale
    height = (shape.height or 0) * _POINTS_PER_EMU * scale
    return fitz.Rect(left, top, left + width, top + height)


def _first_explicit_font_size_pt(shape) -> float | None:
    """Returns the first run's explicit font size found in this text frame
    (in points), or None if every run just inherits its size from the
    slide layout/theme -- python-pptx has no way to resolve that inherited
    value, so callers fall back to a fixed default instead."""
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if run.font.size is not None:
                return run.font.size.pt
    return None


def _first_explicit_font_color(shape) -> tuple[float, float, float] | None:
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            color = run.font.color
            try:
                if color.type is not None and color.rgb is not None:
                    rgb = color.rgb
                    return (rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
            except (AttributeError, TypeError, KeyError):
                # Theme-color references (color.theme_color) don't resolve
                # to an RGB triple through python-pptx -- fall back below
                # rather than fail the whole thumbnail over a color.
                continue
    return None


def _draw_picture(page, shape, rect: "fitz.Rect") -> None:
    try:
        image_bytes = shape.image.blob
    except Exception:
        # Linked (not embedded) images, or an unreadable image part -- skip
        # this one shape rather than fail the whole slide's thumbnail.
        return
    try:
        page.insert_image(rect, stream=image_bytes)
    except Exception:
        pass


def _draw_text(page, shape, rect: "fitz.Rect", scale: float) -> None:
    text = shape.text_frame.text
    if not text.strip():
        return
    font_size = _first_explicit_font_size_pt(shape)
    font_size = (font_size or _DEFAULT_FONT_SIZE_PT) * scale
    font_size = max(font_size, 4)  # a vanishingly small size makes
    # insert_textbox silently draw nothing on some builds -- clamp to a
    # readable floor instead of losing the text entirely.
    color = _first_explicit_font_color(shape) or (0, 0, 0)
    try:
        page.insert_textbox(
            rect, text, fontsize=font_size, fontname=_CJK_FONTNAME, color=color
        )
    except Exception:
        pass


def _draw_fallback_fill(page, shape, rect: "fitz.Rect") -> None:
    try:
        if shape.fill.type is None:
            return
        from pptx.enum.shapes import MSO_FILL_TYPE

        if shape.fill.type != MSO_FILL_TYPE.SOLID:
            return
        rgb = shape.fill.fore_color.rgb
        color = (rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    except Exception:
        color = _FALLBACK_FILL_COLOR
    try:
        page.draw_rect(rect, color=None, fill=color)
    except Exception:
        pass


def _draw_shape(page, shape, scale: float) -> None:
    if shape.width is None or shape.height is None:
        return
    rect = _shape_rect_pt(shape, scale)
    if rect.is_empty:
        return

    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        _draw_picture(page, shape, rect)
        return

    has_text = getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip()
    if has_text:
        _draw_text(page, shape, rect, scale)
        return

    # Everything else (plain rectangles/lines used as dividers, tables,
    # charts, SmartArt, unpictured placeholders, ...) -- draw at most a
    # flat approximation of a solid fill; anything more (an actual table
    # grid, a chart) is out of scope for an approximate preview.
    _draw_fallback_fill(page, shape, rect)


def render_pptx_thumbnails(
    pptx_path: str, thumbnail_width: int = DEFAULT_THUMBNAIL_WIDTH
) -> list[bytes]:
    """Returns one PNG (as raw bytes) per slide, in slide order."""
    try:
        prs = Presentation(pptx_path)
    except PackageNotFoundError as exc:
        raise PptxPreviewError(f"not a valid .pptx file: {exc}") from exc
    except Exception as exc:
        raise PptxPreviewError(f"failed to open pptx: {exc}") from exc

    slide_width_emu = prs.slide_width
    slide_height_emu = prs.slide_height
    if not slide_width_emu or not slide_height_emu:
        raise PptxPreviewError("pptx reports no slide dimensions")

    scale = thumbnail_width / (slide_width_emu * _POINTS_PER_EMU)
    page_width_pt = thumbnail_width
    page_height_pt = slide_height_emu * _POINTS_PER_EMU * scale

    doc = fitz.open()
    try:
        thumbnails = []
        for slide in prs.slides:
            page = doc.new_page(width=page_width_pt, height=page_height_pt)
            # Draw in the deck's own z-order so later shapes correctly
            # paint over earlier ones, same as PowerPoint would render them.
            for shape in slide.shapes:
                _draw_shape(page, shape, scale)
            pix = page.get_pixmap()
            thumbnails.append(pix.tobytes("png"))
        return thumbnails
    finally:
        doc.close()
