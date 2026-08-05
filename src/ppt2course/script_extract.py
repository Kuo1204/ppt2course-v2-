"""Extracts plain text from an uploaded script file (.txt / .docx) so it can
be fed straight into the same page-marker parser the paste-a-script UI uses.
"""

import io
import os

from docx import Document

SUPPORTED_EXTENSIONS = {".txt", ".docx"}


class ScriptExtractionError(Exception):
    pass


def extract_text_from_file(filename: str, content: bytes) -> str:
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".txt":
        # utf-8-sig strips a leading BOM if present (common from Windows
        # editors saving "UTF-8 with BOM") and is identical to plain utf-8
        # otherwise. A stray BOM before the text broke the "^第" page-marker
        # regex on line 1 only, since it isn't whitespace the regex skips.
        return content.decode("utf-8-sig", errors="replace")

    if ext == ".docx":
        try:
            doc = Document(io.BytesIO(content))
        except Exception as exc:
            raise ScriptExtractionError(f"invalid .docx file: {exc}") from exc
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)

    raise ScriptExtractionError(
        f"unsupported file type {ext!r}; expected one of {sorted(SUPPORTED_EXTENSIONS)}"
    )
