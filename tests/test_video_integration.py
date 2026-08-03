"""Integration test against real ffmpeg + real edge-tts (STEP5)."""

import glob
import os
import shutil
import subprocess

import pytest

from ppt2course.audio_duration import get_audio_duration_ms
from ppt2course.tts import synthesize
from ppt2course.video import (
    SlideVideoInput,
    _compute_slide_offsets,
    _total_duration_ms,
    compose_video,
)


def _ensure_ffmpeg_on_path() -> bool:
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return True

    candidates = glob.glob(
        os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin"
        )
    )
    for bin_dir in candidates:
        if os.path.exists(os.path.join(bin_dir, "ffmpeg.exe")):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return True

    return False


HAS_FFMPEG = _ensure_ffmpeg_on_path()

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not available")


def _make_color_image(path: str, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x240",
            "-frames:v",
            "1",
            "-y",
            path,
        ],
        check=True,
        capture_output=True,
    )


def test_compose_video_end_to_end_with_real_ffmpeg_and_edge_tts(tmp_path):
    voice = "zh-TW-HsiaoChenNeural"

    img1 = str(tmp_path / "slide1.png")
    img2 = str(tmp_path / "slide2.png")
    _make_color_image(img1, "red")
    _make_color_image(img2, "blue")

    audio1 = str(tmp_path / "slide1.mp3")
    audio2 = str(tmp_path / "slide2.mp3")
    chunks1 = synthesize("你好嗎？", voice, audio1)
    chunks2 = synthesize("我很好，謝謝。", voice, audio2)

    slides = [
        SlideVideoInput(image_path=img1, audio_path=audio1, chunks=chunks1),
        SlideVideoInput(image_path=img2, audio_path=audio2, chunks=chunks2),
    ]

    out_video = str(tmp_path / "out.mp4")
    out_srt = str(tmp_path / "out.srt")

    transition_ms = 500
    durations_ms = [get_audio_duration_ms(audio1), get_audio_duration_ms(audio2)]

    compose_video(slides, out_video, out_srt, transition_duration_ms=transition_ms)

    assert os.path.exists(out_video)
    assert os.path.getsize(out_video) > 0

    srt_text = open(out_srt, encoding="utf-8").read()
    assert "你好嗎" in srt_text
    assert "我很好" in srt_text

    expected_total_ms = _total_duration_ms(durations_ms, transition_ms)
    actual_ms = get_audio_duration_ms(out_video)
    assert abs(actual_ms - expected_total_ms) <= 200


def _get_resolution(path: str) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0", path,
        ],
        capture_output=True, text=True, check=True,
    )
    w, h = result.stdout.strip().split("x")
    return int(w), int(h)


def test_compose_video_with_logo_bgm_intro_outro_real_ffmpeg(tmp_path):
    voice = "zh-TW-HsiaoChenNeural"

    img1 = str(tmp_path / "slide1.png")
    _make_color_image(img1, "green")
    audio1 = str(tmp_path / "slide1.mp3")
    chunks1 = synthesize("你好嗎？", voice, audio1)

    slides = [SlideVideoInput(image_path=img1, audio_path=audio1, chunks=chunks1)]

    logo_path = str(tmp_path / "logo.png")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=200x200", "-frames:v", "1", logo_path],
        check=True, capture_output=True,
    )

    bgm_path = str(tmp_path / "bgm.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-q:a", "9", bgm_path],
        check=True, capture_output=True,
    )

    intro_path = str(tmp_path / "intro.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=25:duration=1",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest", intro_path],
        check=True, capture_output=True,
    )
    outro_path = str(tmp_path / "outro.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=800x600:rate=15:duration=1",
         "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-shortest", outro_path],
        check=True, capture_output=True,
    )

    out_video = str(tmp_path / "out.mp4")
    out_srt = str(tmp_path / "out.srt")
    resolution = (1280, 720)

    compose_video(
        slides, out_video, out_srt,
        resolution=resolution, fps=30,
        logo_path=logo_path, bgm_path=bgm_path,
        intro_path=intro_path, outro_path=outro_path,
    )

    assert os.path.exists(out_video)
    assert os.path.getsize(out_video) > 0
    assert _get_resolution(out_video) == resolution

    main_duration_s = get_audio_duration_ms(audio1) / 1000
    expected_total_s = 1.0 + main_duration_s + 1.0  # intro(1s) + main + outro(1s)
    actual_total_s = get_audio_duration_ms(out_video) / 1000
    assert abs(actual_total_s - expected_total_s) < 0.6
