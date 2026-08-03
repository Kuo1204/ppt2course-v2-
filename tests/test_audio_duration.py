from unittest.mock import patch

import pytest

from ppt2course.audio_duration import AudioDurationError, get_audio_duration_ms


def _mock_which(found=True):
    return (lambda name: "/usr/bin/ffprobe") if found else (lambda name: None)


def _mock_run_result(returncode=0, stdout="", stderr=""):
    class Result:
        pass

    r = Result()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def test_returns_floor_milliseconds_from_ffprobe_output():
    with patch("ppt2course.audio_duration.shutil.which", side_effect=_mock_which()):
        with patch(
            "ppt2course.audio_duration.subprocess.run",
            return_value=_mock_run_result(stdout="12.345678\n"),
        ):
            assert get_audio_duration_ms("dummy.mp3") == 12345


def test_floor_truncates_not_rounds():
    with patch("ppt2course.audio_duration.shutil.which", side_effect=_mock_which()):
        with patch(
            "ppt2course.audio_duration.subprocess.run",
            return_value=_mock_run_result(stdout="1.9999\n"),
        ):
            assert get_audio_duration_ms("dummy.mp3") == 1999


def test_floor_exact_boundary_no_off_by_one():
    with patch("ppt2course.audio_duration.shutil.which", side_effect=_mock_which()):
        with patch(
            "ppt2course.audio_duration.subprocess.run",
            return_value=_mock_run_result(stdout="2.000000\n"),
        ):
            assert get_audio_duration_ms("dummy.mp3") == 2000


def test_constructs_expected_ffprobe_command():
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _mock_run_result(stdout="1.0\n")

    with patch("ppt2course.audio_duration.shutil.which", side_effect=_mock_which()):
        with patch("ppt2course.audio_duration.subprocess.run", side_effect=fake_run):
            get_audio_duration_ms("some/audio.mp3")

    assert captured["cmd"] == [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        "some/audio.mp3",
    ]


def test_raises_when_ffprobe_not_on_path():
    with patch("ppt2course.audio_duration.shutil.which", side_effect=_mock_which(found=False)):
        with patch("ppt2course.audio_duration.subprocess.run") as mock_run:
            with pytest.raises(AudioDurationError):
                get_audio_duration_ms("dummy.mp3")
            mock_run.assert_not_called()


def test_raises_on_nonzero_exit_code_with_stderr_message():
    with patch("ppt2course.audio_duration.shutil.which", side_effect=_mock_which()):
        with patch(
            "ppt2course.audio_duration.subprocess.run",
            return_value=_mock_run_result(returncode=1, stderr="No such file or directory"),
        ):
            with pytest.raises(AudioDurationError, match="No such file or directory"):
                get_audio_duration_ms("missing.mp3")


def test_raises_on_unparseable_output():
    with patch("ppt2course.audio_duration.shutil.which", side_effect=_mock_which()):
        with patch(
            "ppt2course.audio_duration.subprocess.run",
            return_value=_mock_run_result(stdout="N/A\n"),
        ):
            with pytest.raises(AudioDurationError):
                get_audio_duration_ms("dummy.mp3")
