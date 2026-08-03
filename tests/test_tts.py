from unittest.mock import patch

import pytest

from ppt2course.subtitle import TimedChunk
from ppt2course.tts import TtsError, synthesize


class FakeCommunicate:
    def __init__(self, chunks, *, raise_during_stream=False):
        self._chunks = chunks
        self._raise = raise_during_stream

    def stream(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk
            if self._raise:
                raise RuntimeError("simulated network failure")

        return gen()


def word_boundary(text, start_ms, end_ms):
    return {
        "type": "WordBoundary",
        "offset": start_ms * 10_000,
        "duration": (end_ms - start_ms) * 10_000,
        "text": text,
    }


def audio(data: bytes):
    return {"type": "audio", "data": data}


def _patched(chunks, **kwargs):
    return patch(
        "ppt2course.tts.edge_tts.Communicate",
        lambda text, voice, boundary=None: FakeCommunicate(chunks, **kwargs),
    )


def test_writes_concatenated_audio_bytes_to_out_path(tmp_path):
    chunks = [audio(b"abc"), audio(b"def"), word_boundary("你", 0, 200)]
    out_path = tmp_path / "out.mp3"
    with _patched(chunks):
        synthesize("你", "zh-TW-HsiaoChenNeural", str(out_path))
    assert out_path.read_bytes() == b"abcdef"


def test_aligns_events_and_fills_skipped_punctuation_with_zero_duration(tmp_path):
    text = "你好嗎？我很好。"
    chunks = [
        word_boundary("你", 0, 200),
        word_boundary("好", 200, 400),
        word_boundary("嗎", 400, 600),
        word_boundary("我", 800, 1000),
        word_boundary("很", 1000, 1200),
        word_boundary("好", 1200, 1400),
    ]
    with _patched(chunks):
        result = synthesize(text, "zh-TW-HsiaoChenNeural", str(tmp_path / "out.mp3"))

    assert result == [
        TimedChunk("你", 0, 200),
        TimedChunk("好", 200, 400),
        TimedChunk("嗎", 400, 600),
        TimedChunk("？", 600, 600),
        TimedChunk("我", 800, 1000),
        TimedChunk("很", 1000, 1200),
        TimedChunk("好", 1200, 1400),
        TimedChunk("。", 1400, 1400),
    ]


def test_multi_character_word_boundary_kept_as_single_chunk(tmp_path):
    text = "你好，世界"
    chunks = [
        word_boundary("你", 0, 200),
        word_boundary("好", 200, 400),
        word_boundary("世界", 600, 1000),
    ]
    with _patched(chunks):
        result = synthesize(text, "zh-TW-HsiaoChenNeural", str(tmp_path / "out.mp3"))

    assert result == [
        TimedChunk("你", 0, 200),
        TimedChunk("好", 200, 400),
        TimedChunk("，", 400, 400),
        TimedChunk("世界", 600, 1000),
    ]


def test_leading_punctuation_before_any_event_defaults_to_zero(tmp_path):
    text = "。你好"
    chunks = [
        word_boundary("你", 100, 300),
        word_boundary("好", 300, 500),
    ]
    with _patched(chunks):
        result = synthesize(text, "zh-TW-HsiaoChenNeural", str(tmp_path / "out.mp3"))

    assert result[0] == TimedChunk("。", 0, 0)
    assert result[1:] == [TimedChunk("你", 100, 300), TimedChunk("好", 300, 500)]


def test_raises_tts_error_when_event_text_cannot_be_aligned(tmp_path):
    chunks = [word_boundary("完全不相關", 0, 200)]
    with _patched(chunks):
        with pytest.raises(TtsError):
            synthesize("你好", "zh-TW-HsiaoChenNeural", str(tmp_path / "out.mp3"))


def test_raises_tts_error_on_stream_failure(tmp_path):
    chunks = [word_boundary("你", 0, 200)]
    with _patched(chunks, raise_during_stream=True):
        with pytest.raises(TtsError):
            synthesize("你好", "zh-TW-HsiaoChenNeural", str(tmp_path / "out.mp3"))
