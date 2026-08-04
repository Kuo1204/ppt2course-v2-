"""STEP 3: TTS synthesis via edge-tts — yields audio plus WordBoundary timestamps.

edge-tts's WordBoundary stream skips punctuation entirely (no event, no
duration) and sometimes groups multiple characters into one boundary event
(e.g. "世界"). This module re-aligns those events against the original
script text, inserting zero-duration chunks for whatever got skipped so the
result is a complete, ordered TimedChunk list STEP4 can consume directly.
"""

import asyncio

import edge_tts

from ppt2course.subtitle import TimedChunk

TICKS_PER_MS = 10_000
DEFAULT_RATE = "+0%"
DEFAULT_VOLUME = "+0%"


class TtsError(Exception):
    pass


def _ticks_to_ms(ticks: int) -> int:
    return ticks // TICKS_PER_MS


async def _stream(
    text: str, voice: str, out_path: str, rate: str = DEFAULT_RATE, volume: str = DEFAULT_VOLUME
) -> list[dict]:
    communicate = edge_tts.Communicate(text, voice, boundary="WordBoundary", rate=rate, volume=volume)
    audio_bytes = bytearray()
    events: list[dict] = []

    try:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                events.append(chunk)
    except Exception as exc:
        raise TtsError(f"edge-tts streaming failed: {exc}") from exc

    with open(out_path, "wb") as f:
        f.write(audio_bytes)

    return events


def _align_with_original_text(original_text: str, events: list[dict]) -> list[TimedChunk]:
    chunks: list[TimedChunk] = []
    cursor = 0
    prev_end_ms = 0

    for event in events:
        event_text = event["text"]
        start_ms = _ticks_to_ms(event["offset"])
        end_ms = _ticks_to_ms(event["offset"] + event["duration"])

        idx = original_text.find(event_text, cursor)
        if idx == -1:
            raise TtsError(
                f"could not align WordBoundary text {event_text!r} with original script"
            )

        for skipped in original_text[cursor:idx]:
            chunks.append(TimedChunk(text=skipped, start_ms=prev_end_ms, end_ms=prev_end_ms))

        chunks.append(TimedChunk(text=event_text, start_ms=start_ms, end_ms=end_ms))
        prev_end_ms = end_ms
        cursor = idx + len(event_text)

    for skipped in original_text[cursor:]:
        chunks.append(TimedChunk(text=skipped, start_ms=prev_end_ms, end_ms=prev_end_ms))

    return chunks


def synthesize(
    text: str, voice: str, out_path: str, rate: str = DEFAULT_RATE, volume: str = DEFAULT_VOLUME
) -> list[TimedChunk]:
    events = asyncio.run(_stream(text, voice, out_path, rate=rate, volume=volume))
    return _align_with_original_text(text, events)


async def _stream_audio_only(
    text: str, voice: str, rate: str = DEFAULT_RATE, volume: str = DEFAULT_VOLUME
) -> bytes:
    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    audio_bytes = bytearray()

    try:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes.extend(chunk["data"])
    except Exception as exc:
        raise TtsError(f"edge-tts streaming failed: {exc}") from exc

    return bytes(audio_bytes)


def synthesize_preview(
    text: str, voice: str, rate: str = DEFAULT_RATE, volume: str = DEFAULT_VOLUME
) -> bytes:
    """Short voice-sample synthesis with no WordBoundary alignment — used for
    the "preview this voice" button, where only the audio itself matters."""
    return asyncio.run(_stream_audio_only(text, voice, rate=rate, volume=volume))
