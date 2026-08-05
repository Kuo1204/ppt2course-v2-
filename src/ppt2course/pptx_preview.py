"""STEP 1 preview helper: render real slide thumbnails from an uploaded
.pptx so the upload step can show an actual visual preview (matching the
per-slide image upload field's thumbnail strip) instead of just a filename.

This is purely a UI convenience — it never touches the render pipeline's
own output and nothing it produces is persisted.

Two-stage conversion: LibreOffice headless converts the whole deck to a
single PDF (asking soffice to convert straight to PNG only ever yields the
first slide — PNG has no multi-page concept — so PDF is the only reliable
multi-slide intermediate), then PyMuPDF (fitz) rasterizes each PDF page to
a PNG at thumbnail resolution.
"""

import os
import shutil
import subprocess
import tempfile

import fitz

DEFAULT_THUMBNAIL_WIDTH = 480
CONVERT_TIMEOUT_SEC = 120

# LibreOffice's own installer doesn't reliably put soffice on PATH, even
# though it's a very common self-contained Windows install location — fall
# back to the well-known paths before giving up.
_WINDOWS_SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]


class PptxPreviewError(Exception):
    pass


def find_soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("soffice.exe")
    if found:
        return found
    for candidate in _WINDOWS_SOFFICE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def render_pptx_thumbnails(
    pptx_path: str, thumbnail_width: int = DEFAULT_THUMBNAIL_WIDTH
) -> list[bytes]:
    """Returns one PNG (as raw bytes) per slide, in slide order."""
    soffice = find_soffice()
    if soffice is None:
        raise PptxPreviewError(
            "LibreOffice (soffice) not found — cannot render a PPTX preview"
        )

    with tempfile.TemporaryDirectory(prefix="ppt2course_pptx_preview_") as tmp_dir:
        cmd = [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp_dir, pptx_path]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=CONVERT_TIMEOUT_SEC
            )
        except subprocess.TimeoutExpired as exc:
            raise PptxPreviewError(
                f"LibreOffice conversion timed out after {CONVERT_TIMEOUT_SEC}s"
            ) from exc

        if result.returncode != 0:
            raise PptxPreviewError(
                f"LibreOffice conversion failed (exit {result.returncode}): {result.stderr}"
            )

        pdf_name = os.path.splitext(os.path.basename(pptx_path))[0] + ".pdf"
        pdf_path = os.path.join(tmp_dir, pdf_name)
        if not os.path.exists(pdf_path):
            raise PptxPreviewError(
                f"LibreOffice reported success but the expected PDF is missing: {pdf_path}"
            )

        doc = fitz.open(pdf_path)
        try:
            thumbnails = []
            for page in doc:
                zoom = thumbnail_width / page.rect.width
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                thumbnails.append(pix.tobytes("png"))
            return thumbnails
        finally:
            doc.close()
