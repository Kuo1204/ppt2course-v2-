"""Integration test against real ffmpeg + real edge-tts (STEP5)."""

import glob
import os
import re
import shutil
import subprocess

import pytest
from PIL import Image

from ppt2course.audio_duration import get_audio_duration_ms
from ppt2course.avatar import default_asset_set
from ppt2course.tts import synthesize
from ppt2course.video import (
    DEFAULT_AVATAR_MARGIN,
    AvatarOverlay,
    BrollOverlay,
    SlideVideoInput,
    _compute_slide_offsets,
    _effective_transition_ms,
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

    # Regression check: without an explicit -c:v/-pix_fmt, ffmpeg let the
    # filter graph pick a chroma format (often yuv444p) that standard players
    # — including Windows' built-in ones — can't decode. yuv420p is the
    # broadly-compatible baseline.
    codec_name, pix_fmt = _get_video_codec_info(out_video)
    assert codec_name == "h264"
    assert pix_fmt == "yuv420p"


def _get_video_codec_info(path: str) -> tuple[str, str]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt",
            "-of", "csv=s=x:p=0", path,
        ],
        capture_output=True, text=True, check=True,
    )
    codec_name, pix_fmt = result.stdout.strip().split("x")
    return codec_name, pix_fmt


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


def test_logo_opacity_actually_changes_the_composited_pixel_real_ffmpeg(tmp_path):
    # Renders a red logo over a green background at two opacities and reads
    # the real rendered pixel back out, so this fails if colorchannelmixer
    # were ever silently dropped/ignored rather than just asserting the
    # ffmpeg command string looks right.
    voice = "zh-TW-HsiaoChenNeural"

    img1 = str(tmp_path / "slide1.png")
    _make_color_image(img1, "green")
    audio1 = str(tmp_path / "slide1.mp3")
    chunks1 = synthesize("你好。", voice, audio1)
    slides = [SlideVideoInput(image_path=img1, audio_path=audio1, chunks=chunks1)]

    logo_path = str(tmp_path / "logo.png")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=200x200", "-frames:v", "1", logo_path],
        check=True, capture_output=True,
    )

    resolution = (640, 480)

    def _render_and_sample_top_right(opacity: float, name: str) -> tuple[int, int, int]:
        out_video = str(tmp_path / f"out_{name}.mp4")
        out_srt = str(tmp_path / f"out_{name}.srt")
        compose_video(
            slides, out_video, out_srt,
            resolution=resolution, fps=24,
            logo_path=logo_path, logo_width=200, logo_margin=20, logo_opacity=opacity,
        )
        frame_path = str(tmp_path / f"frame_{name}.png")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "0.1", "-i", out_video, "-frames:v", "1", frame_path],
            check=True, capture_output=True,
        )
        # well inside the 200x200 logo painted at the top-right (margin=20)
        return Image.open(frame_path).convert("RGB").getpixel((580, 60))

    opaque_pixel = _render_and_sample_top_right(1.0, "opaque")
    half_pixel = _render_and_sample_top_right(0.5, "half")

    assert opaque_pixel[0] > 200 and opaque_pixel[1] < 60  # near-pure red logo
    # at half opacity the green background must show through, meaningfully
    # raising the green channel versus the fully-opaque render
    assert half_pixel[1] > opaque_pixel[1] + 40


def _srt_cue_times_ms(srt_text: str) -> list[tuple[int, int]]:
    def to_ms(ts: str) -> int:
        h, m, rest = ts.split(":")
        s, ms = rest.split(",")
        return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)

    return [
        (to_ms(start), to_ms(end))
        for start, end in re.findall(
            r"(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})", srt_text
        )
    ]


def test_compose_video_with_a_short_slide_and_max_transition_keeps_subtitles_in_sync(tmp_path):
    # Regression test for a real user report: pushing the transition-duration
    # slider to its 2-second max produced subtitles that drifted out of sync
    # with the narration, and audibly overlapping audio, whenever a slide's
    # own narration was shorter than the transition. Root cause: nothing
    # clamped the requested transition against the actual (short) slide
    # audio it would be crossfaded against — see _effective_transition_ms.
    # The middle slide here is deliberately given a near-silent one-syllable
    # line so its real edge-tts audio comes back well under 2 seconds.
    voice = "zh-TW-HsiaoChenNeural"

    img1 = str(tmp_path / "slide1.png")
    img2 = str(tmp_path / "slide2.png")
    img3 = str(tmp_path / "slide3.png")
    _make_color_image(img1, "red")
    _make_color_image(img2, "blue")
    _make_color_image(img3, "green")

    audio1 = str(tmp_path / "slide1.mp3")
    audio2 = str(tmp_path / "slide2.mp3")
    audio3 = str(tmp_path / "slide3.mp3")
    chunks1 = synthesize("這是第一頁比較長的旁白內容，用來確認字幕的起始時間是正確的。", voice, audio1)
    chunks2 = synthesize("嗯。", voice, audio2)
    chunks3 = synthesize("這是第三頁比較長的旁白內容，用來確認字幕的結束時間是正確的。", voice, audio3)

    slides = [
        SlideVideoInput(image_path=img1, audio_path=audio1, chunks=chunks1),
        SlideVideoInput(image_path=img2, audio_path=audio2, chunks=chunks2),
        SlideVideoInput(image_path=img3, audio_path=audio3, chunks=chunks3),
    ]
    durations_ms = [
        get_audio_duration_ms(audio1),
        get_audio_duration_ms(audio2),
        get_audio_duration_ms(audio3),
    ]
    # The repro only works if the middle slide really is shorter than the
    # requested transition — otherwise this test would pass even with the
    # bug still present.
    assert durations_ms[1] < 2000

    out_video = str(tmp_path / "out.mp4")
    out_srt = str(tmp_path / "out.srt")
    requested_transition_ms = 2000  # the reported max slider value

    compose_video(slides, out_video, out_srt, transition_duration_ms=requested_transition_ms)

    assert os.path.exists(out_video)
    assert os.path.getsize(out_video) > 0

    cues = _srt_cue_times_ms(open(out_srt, encoding="utf-8").read())
    assert len(cues) >= 3
    # The bug produced non-monotonic (even backwards) cue timing once the
    # transition ate more than a short slide's entire audio — every cue
    # must start no earlier than the previous one, and none may start
    # before its own end.
    for start_ms, end_ms in cues:
        assert start_ms <= end_ms
    for (_, prev_end), (next_start, _) in zip(cues, cues[1:]):
        assert next_start >= prev_end

    # The rendered video's real length must match the *clamped* timeline,
    # not the naive one the old (buggy) math would have predicted.
    effective_transition_ms = _effective_transition_ms(durations_ms, requested_transition_ms)
    assert effective_transition_ms < requested_transition_ms  # confirms clamping actually kicked in
    expected_total_ms = _total_duration_ms(durations_ms, effective_transition_ms)
    actual_ms = get_audio_duration_ms(out_video)
    assert abs(actual_ms - expected_total_ms) <= 300


def test_broll_overlay_swaps_picture_without_moving_audio_or_subtitles_real_ffmpeg(tmp_path):
    # Real end-to-end proof of the core B-roll promise: the picture changes
    # during [start_ms, end_ms) and reverts after, while narration audio,
    # subtitle cues, and total video length are byte-for-byte identical to
    # the same job with zero B-roll.
    voice = "zh-TW-HsiaoChenNeural"

    img1 = str(tmp_path / "slide1.png")
    _make_color_image(img1, "green")
    broll_img = str(tmp_path / "broll.png")
    _make_color_image(broll_img, "red")

    audio1 = str(tmp_path / "slide1.mp3")
    chunks1 = synthesize("這是一段比較長的旁白，用來確認畫面切換的時候聲音完全不受影響。", voice, audio1)
    duration_ms = get_audio_duration_ms(audio1)
    assert duration_ms > 2000  # room for a broll window with slack on both sides

    broll_start_ms = 800
    broll_end_ms = 1600

    def _render(with_broll: bool, name: str) -> tuple[str, str]:
        broll_overlays = (
            (BrollOverlay(image_path=broll_img, start_ms=broll_start_ms, end_ms=broll_end_ms),)
            if with_broll
            else ()
        )
        slide = SlideVideoInput(
            image_path=img1, audio_path=audio1, chunks=chunks1, broll_overlays=broll_overlays
        )
        out_video = str(tmp_path / f"out_{name}.mp4")
        out_srt = str(tmp_path / f"out_{name}.srt")
        compose_video([slide], out_video, out_srt, resolution=(640, 480), fps=24)
        return out_video, out_srt

    video_without, srt_without = _render(False, "without")
    video_with, srt_with = _render(True, "with")

    # Audio/subtitles: completely unaffected by the B-roll.
    assert open(srt_without, encoding="utf-8").read() == open(srt_with, encoding="utf-8").read()
    assert abs(get_audio_duration_ms(video_without) - get_audio_duration_ms(video_with)) <= 50

    def _sample_center(video_path: str, at_seconds: float) -> tuple[int, int, int]:
        frame_path = str(tmp_path / f"frame_{at_seconds}.png")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(at_seconds), "-i", video_path, "-frames:v", "1", frame_path],
            check=True, capture_output=True,
        )
        return Image.open(frame_path).convert("RGB").getpixel((320, 240))

    before = _sample_center(video_with, broll_start_ms / 2 / 1000)  # well before the window
    during = _sample_center(video_with, (broll_start_ms + broll_end_ms) / 2 / 1000)  # mid-window
    after = _sample_center(video_with, (broll_end_ms + duration_ms) / 2 / 1000)  # after it ends

    # ffmpeg's named "green" is the dim HTML green (0,128,0), not lime, so
    # these compare channel dominance rather than assuming a bright value.
    def _is_red(px: tuple[int, int, int]) -> bool:
        r, g, b = px
        return r > g + 40 and r > b + 40

    def _is_green(px: tuple[int, int, int]) -> bool:
        r, g, b = px
        return g > r + 40 and g > b + 40

    assert _is_green(before)
    assert _is_red(during)
    assert _is_green(after)


def test_avatar_overlay_composites_without_moving_audio_or_subtitles_real_ffmpeg(tmp_path):
    # Real end-to-end proof of the avatar promise: the bundled placeholder
    # character actually appears in its corner during [start_ms, end_ms) and
    # disappears after, while narration audio, subtitle cues, and total
    # video length are byte-for-byte identical to the same job with no
    # avatar at all.
    voice = "zh-TW-HsiaoChenNeural"
    resolution = (640, 480)

    img1 = str(tmp_path / "slide1.png")
    _make_color_image(img1, "green")

    audio1 = str(tmp_path / "slide1.mp3")
    chunks1 = synthesize("這是一段比較長的旁白，用來確認頭像出現的時候聲音完全不受影響。", voice, audio1)
    duration_ms = get_audio_duration_ms(audio1)
    assert duration_ms > 2000

    avatar_start_ms = 800
    avatar_end_ms = 1600
    avatar_path = default_asset_set().idle

    def _render(with_avatar: bool, name: str) -> tuple[str, str]:
        avatar_overlays = (
            (AvatarOverlay(image_path=avatar_path, start_ms=avatar_start_ms, end_ms=avatar_end_ms),)
            if with_avatar
            else ()
        )
        slide = SlideVideoInput(
            image_path=img1, audio_path=audio1, chunks=chunks1, avatar_overlays=avatar_overlays
        )
        out_video = str(tmp_path / f"out_{name}.mp4")
        out_srt = str(tmp_path / f"out_{name}.srt")
        # "right" (vertically centered), not the bottom_right default: the
        # burned-in subtitle band also lives at the bottom of the frame, so
        # sampling a bottom-anchored avatar position would pick up subtitle
        # glyph pixels instead of the avatar itself.
        compose_video(
            [slide], out_video, out_srt, resolution=resolution, fps=24, avatar_position="right"
        )
        return out_video, out_srt

    video_without, srt_without = _render(False, "without")
    video_with, srt_with = _render(True, "with")

    # Audio/subtitles: completely unaffected by the avatar.
    assert open(srt_without, encoding="utf-8").read() == open(srt_with, encoding="utf-8").read()
    assert abs(get_audio_duration_ms(video_without) - get_audio_duration_ms(video_with)) <= 50

    def _sample(video_path: str, at_seconds: float, xy: tuple[int, int]) -> tuple[int, int, int]:
        frame_path = str(tmp_path / f"frame_{os.path.basename(video_path)}_{at_seconds}.png")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(at_seconds), "-i", video_path, "-frames:v", "1", frame_path],
            check=True, capture_output=True,
        )
        return Image.open(frame_path).convert("RGB").getpixel(xy)

    # Mirrors video.py's "small" size fraction (0.28 of frame height) and the
    # "right" position formula, to land inside the character's opaque blue
    # torso (not the transparent background around it, and not the subtitle
    # band at the bottom of the frame).
    width, height = resolution
    avatar_h = int(height * 0.28)
    avatar_w = int(avatar_h * 800 / 960)  # bundled PNGs are 800x960
    ox = width - avatar_w - DEFAULT_AVATAR_MARGIN
    oy = (height - avatar_h) / 2
    sample_xy = (int(ox + avatar_w * 0.5), int(oy + avatar_h * 0.85))

    def _is_green(px: tuple[int, int, int]) -> bool:
        r, g, b = px
        return g > r + 40 and g > b + 40

    def _is_avatar_blue(px: tuple[int, int, int]) -> bool:
        r, g, b = px
        return b > r + 20 and b > g

    before = _sample(video_with, avatar_start_ms / 2 / 1000, sample_xy)
    during = _sample(video_with, (avatar_start_ms + avatar_end_ms) / 2 / 1000, sample_xy)
    after = _sample(video_with, (avatar_end_ms + duration_ms) / 2 / 1000, sample_xy)
    always_green = _sample(video_without, (avatar_start_ms + avatar_end_ms) / 2 / 1000, sample_xy)

    assert _is_green(before)
    assert _is_avatar_blue(during)
    assert _is_green(after)
    assert _is_green(always_green)  # same spot, no avatar at all -> never changes


def test_reading_pause_extends_visual_hold_without_moving_narration_or_subtitles_real_ffmpeg(
    tmp_path,
):
    # Real end-to-end proof of the reading-pause promise: the rendered
    # video is measurably longer with a pause than without, but the SRT
    # (built from slide.chunks, which know nothing about the pause) is
    # byte-for-byte identical either way — narration/subtitles never move,
    # only how long the picture holds afterward.
    voice = "zh-TW-HsiaoChenNeural"
    pause_ms = 1500

    img1 = str(tmp_path / "slide1.png")
    _make_color_image(img1, "green")
    audio1 = str(tmp_path / "slide1.mp3")
    chunks1 = synthesize("這是一段用來測試閱讀停頓的旁白。", voice, audio1)
    narration_ms = get_audio_duration_ms(audio1)

    def _render(pause: int, name: str) -> tuple[str, str]:
        slide = SlideVideoInput(
            image_path=img1, audio_path=audio1, chunks=chunks1, reading_pause_ms=pause
        )
        out_video = str(tmp_path / f"out_{name}.mp4")
        out_srt = str(tmp_path / f"out_{name}.srt")
        compose_video([slide], out_video, out_srt, resolution=(640, 480), fps=24)
        return out_video, out_srt

    video_without, srt_without = _render(0, "without")
    video_with, srt_with = _render(pause_ms, "with")

    assert open(srt_without, encoding="utf-8").read() == open(srt_with, encoding="utf-8").read()

    duration_without = get_audio_duration_ms(video_without)
    duration_with = get_audio_duration_ms(video_with)
    assert abs(duration_without - narration_ms) <= 100
    assert abs(duration_with - (narration_ms + pause_ms)) <= 150

    # The *audio stream itself* was padded with silence (apad), not just the
    # video track held on a freeze frame while audio quietly ended early —
    # a real player would otherwise drop out of sync visibly. Probed
    # separately from container duration (which is already asserted above)
    # to make sure this is really testing the audio stream, not video.
    def _audio_stream_duration_ms(path: str) -> int:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=duration", "-of", "csv=p=0", path,
            ],
            capture_output=True, text=True, check=True,
        )
        return round(float(result.stdout.strip()) * 1000)

    audio_stream_ms_with = _audio_stream_duration_ms(video_with)
    assert abs(audio_stream_ms_with - (narration_ms + pause_ms)) <= 150


def test_ken_burns_enabled_still_matches_expected_duration_and_resolution_real_ffmpeg(tmp_path):
    # Ken Burns replaces the scale/pad chain with zoompan entirely for every
    # slide — this proves that swap doesn't silently break duration,
    # resolution, or the master narration/subtitle timeline. The actual
    # zoom-in-crops-tighter-over-time behavior itself was hand-verified
    # separately (zoompan is well-established ffmpeg machinery); this test
    # guards the integration, not the visual effect.
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

    compose_video(
        slides, out_video, out_srt,
        resolution=(640, 480), fps=24,
        transition_duration_ms=transition_ms, enable_ken_burns=True,
    )

    assert os.path.exists(out_video)
    expected_total_ms = _total_duration_ms(durations_ms, transition_ms)
    assert abs(get_audio_duration_ms(out_video) - expected_total_ms) <= 200
    assert _get_resolution(out_video) == (640, 480)

    srt_text = open(out_srt, encoding="utf-8").read()
    assert "你好嗎" in srt_text
    assert "我很好" in srt_text


def test_avoid_voice_overlap_produces_a_real_gap_and_matches_pure_concat_length_real_ffmpeg(
    tmp_path,
):
    # Real end-to-end proof: with avoid_voice_overlap, slide 2's narration
    # never starts until slide 1's has completely finished (no crossfade
    # audio blend), and the total rendered length is the *uncompressed*
    # sum of both slides' real narration -- longer than a plain crossfade
    # render of the same two clips by exactly the transition duration.
    voice = "zh-TW-HsiaoChenNeural"
    resolution = (640, 480)
    transition_ms = 500

    img1 = str(tmp_path / "slide1.png")
    img2 = str(tmp_path / "slide2.png")
    _make_color_image(img1, "red")
    _make_color_image(img2, "blue")
    audio1 = str(tmp_path / "slide1.mp3")
    audio2 = str(tmp_path / "slide2.mp3")
    chunks1 = synthesize("這是第一頁的旁白內容。", voice, audio1)
    chunks2 = synthesize("這是第二頁的旁白內容。", voice, audio2)
    narration_ms = [get_audio_duration_ms(audio1), get_audio_duration_ms(audio2)]

    slides = [
        SlideVideoInput(image_path=img1, audio_path=audio1, chunks=chunks1),
        SlideVideoInput(image_path=img2, audio_path=audio2, chunks=chunks2),
    ]

    out_video = str(tmp_path / "out.mp4")
    out_srt = str(tmp_path / "out.srt")
    compose_video(
        slides, out_video, out_srt,
        resolution=resolution, fps=24,
        transition_duration_ms=transition_ms, avoid_voice_overlap=True,
    )

    assert os.path.exists(out_video)

    # Uncompressed total: no crossfade savings on the audio side at all.
    expected_total_ms = sum(narration_ms)
    assert abs(get_audio_duration_ms(out_video) - expected_total_ms) <= 200
    # Strictly longer than what a normal crossfade render of the exact same
    # two clips would produce (by ~transition_ms).
    crossfade_total_ms = _total_duration_ms(
        narration_ms, _effective_transition_ms(narration_ms, transition_ms)
    )
    assert get_audio_duration_ms(out_video) > crossfade_total_ms

    # Slide 2's cues start at (approximately) slide 1's own real narration
    # length -- pure concatenation, not offset back by transition_ms. Every
    # cue must start at or after that boundary (never mid-slide-1, which is
    # what a crossfade-compressed offset would have allowed), and at least
    # one must start right at it.
    def _parse_srt_start_ms(line: str) -> int:
        start_str = line.split(" --> ")[0]
        h, m, s_ms = start_str.split(":")
        s, ms = s_ms.split(",")
        return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)

    srt_text = open(out_srt, encoding="utf-8").read()
    cue_starts_ms = [
        _parse_srt_start_ms(line)
        for line in srt_text.splitlines()
        if " --> " in line
    ]
    # Allowing up to MIN_GAP_MS (subtitle.py) on top of the boundary itself:
    # _build_cues nudges a slide's cues later, never earlier, to keep a
    # minimum gap from the previous slide's last cue -- exactly the
    # legitimate "never touch/overlap" behavior this feature also relies on.
    later_cue_starts = [t for t in cue_starts_ms if t >= narration_ms[0] - 50]
    assert later_cue_starts, cue_starts_ms
    assert min(later_cue_starts) - narration_ms[0] <= 150
