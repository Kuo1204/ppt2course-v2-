"""Integration test against the real edge-tts service (STEP3)."""

from ppt2course.tts import synthesize


def test_synthesize_against_real_edge_tts_service(tmp_path):
    text = "你好嗎？我很好，謝謝。"
    out_path = tmp_path / "out.mp3"

    chunks = synthesize(text, "zh-TW-HsiaoChenNeural", str(out_path))

    assert out_path.exists()
    assert out_path.stat().st_size > 0

    assert "".join(c.text for c in chunks) == text

    for chunk in chunks:
        assert chunk.start_ms <= chunk.end_ms

    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.start_ms <= nxt.start_ms
