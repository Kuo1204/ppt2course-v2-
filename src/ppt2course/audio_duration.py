"""STEP 3.5: Measure audio duration via ffprobe — single source of truth for timing."""

import math
import shutil
import subprocess


class AudioDurationError(Exception):
    pass


def get_audio_duration_ms(path: str, ffprobe_path: str = "ffprobe") -> int:
    if shutil.which(ffprobe_path) is None:
        raise AudioDurationError(f"ffprobe executable not found: {ffprobe_path!r}")

    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise AudioDurationError(f"failed to invoke ffprobe: {exc}") from exc

    if result.returncode != 0:
        raise AudioDurationError(
            f"ffprobe exited with code {result.returncode}: {result.stderr.strip()}"
        )

    output = result.stdout.strip()
    try:
        duration_seconds = float(output)
    except ValueError as exc:
        raise AudioDurationError(f"could not parse ffprobe output: {output!r}") from exc

    return math.floor(duration_seconds * 1000)
