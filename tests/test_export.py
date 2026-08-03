import pytest
from docx import Document

from ppt2course.export import ExportError, export_outputs


def _make_source_files(tmp_path):
    mp4_src = tmp_path / "src.mp4"
    srt_src = tmp_path / "src.srt"
    mp4_src.write_bytes(b"fake mp4 bytes")
    srt_src.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello", encoding="utf-8")
    return str(mp4_src), str(srt_src)


def test_export_outputs_copies_mp4_and_srt_with_shared_base_name(tmp_path):
    mp4_src, srt_src = _make_source_files(tmp_path)
    out_dir = tmp_path / "out"

    result = export_outputs(mp4_src, srt_src, ["講稿一"], str(out_dir), "課程")

    mp4_dest = out_dir / "課程.mp4"
    srt_dest = out_dir / "課程.srt"
    docx_dest = out_dir / "課程.docx"

    assert mp4_dest.read_bytes() == b"fake mp4 bytes"
    assert srt_dest.read_text(encoding="utf-8") == "1\n00:00:00,000 --> 00:00:01,000\nhello"
    assert docx_dest.exists()
    assert result == {
        "mp4": str(mp4_dest),
        "srt": str(srt_dest),
        "docx": str(docx_dest),
    }


def test_export_outputs_creates_output_directory_if_missing(tmp_path):
    mp4_src, srt_src = _make_source_files(tmp_path)
    out_dir = tmp_path / "does" / "not" / "exist"

    export_outputs(mp4_src, srt_src, ["講稿一"], str(out_dir), "課程")

    assert (out_dir / "課程.mp4").exists()


def test_docx_has_heading_and_script_per_slide(tmp_path):
    mp4_src, srt_src = _make_source_files(tmp_path)
    out_dir = tmp_path / "out"

    scripts = ["第一張投影片的講稿", "第二張投影片的講稿"]
    export_outputs(mp4_src, srt_src, scripts, str(out_dir), "課程")

    doc = Document(str(out_dir / "課程.docx"))
    paragraphs = [p.text for p in doc.paragraphs]

    assert "投影片 1" in paragraphs
    assert "第一張投影片的講稿" in paragraphs
    assert "投影片 2" in paragraphs
    assert "第二張投影片的講稿" in paragraphs
    assert paragraphs.index("投影片 1") < paragraphs.index("第一張投影片的講稿")
    assert paragraphs.index("第一張投影片的講稿") < paragraphs.index("投影片 2")


def test_raises_when_target_mp4_already_exists(tmp_path):
    mp4_src, srt_src = _make_source_files(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "課程.mp4").write_bytes(b"already here")

    with pytest.raises(ExportError):
        export_outputs(mp4_src, srt_src, ["講稿"], str(out_dir), "課程")


def test_raises_when_target_srt_already_exists(tmp_path):
    mp4_src, srt_src = _make_source_files(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "課程.srt").write_text("existing", encoding="utf-8")

    with pytest.raises(ExportError):
        export_outputs(mp4_src, srt_src, ["講稿"], str(out_dir), "課程")


def test_raises_when_target_docx_already_exists(tmp_path):
    mp4_src, srt_src = _make_source_files(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "課程.docx").write_bytes(b"existing")

    with pytest.raises(ExportError):
        export_outputs(mp4_src, srt_src, ["講稿"], str(out_dir), "課程")


def test_raises_when_source_mp4_missing(tmp_path):
    _, srt_src = _make_source_files(tmp_path)
    out_dir = tmp_path / "out"

    with pytest.raises(ExportError):
        export_outputs(str(tmp_path / "missing.mp4"), srt_src, ["講稿"], str(out_dir), "課程")


def test_no_partial_overwrite_check_blocks_when_only_one_file_conflicts(tmp_path):
    mp4_src, srt_src = _make_source_files(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "課程.docx").write_bytes(b"existing")

    with pytest.raises(ExportError):
        export_outputs(mp4_src, srt_src, ["講稿"], str(out_dir), "課程")

    # the mp4 must NOT have been written since the conflict check runs before any copy
    assert not (out_dir / "課程.mp4").exists()
