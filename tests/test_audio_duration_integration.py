"""Integration test against a real ffprobe/ffmpeg install (STEP3.5).

Locates ffmpeg's bin directory even when the current shell's PATH hasn't
picked up a just-completed install yet, by falling back to the known
winget package install location and prepending it to this process's PATH.
"""

import glob
import os
import shutil
import subprocess

import pytest

from ppt2course.audio_duration import get_audio_duration_ms


def _ensure_ffmpeg_on_path() -> bool:
    if shutil.which("ffprobe") and shutil.which("ffmpeg"):
        return True

    candidates = glob.glob(
        os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin"
        )
    )
    for bin_dir in candidates:
        if os.path.exists(os.path.join(bin_dir, "ffprobe.exe")):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return True

    return False


HAS_FFMPEG = _ensure_ffmpeg_on_path()

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not available")


def test_get_audio_duration_ms_against_real_ffprobe(tmp_path):
    audio_path = tmp_path / "silence.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            "2",
            "-y",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
    )

    duration_ms = get_audio_duration_ms(str(audio_path))

    assert isinstance(duration_ms, int)
    assert 1900 <= duration_ms <= 2100
