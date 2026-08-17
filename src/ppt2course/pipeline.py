"""Pipeline orchestration — wires STEP1 through STEP6 into one call.

.pptx + slide images in, {base_name}.mp4 / .srt / .docx out. Any failure
along the way aborts immediately with a clear PipelineError rather than
falling back to a partial result — consistent with every module's own
"raise, don't fail silently" behavior.
"""

import dataclasses
import os
import subprocess

from ppt2course.audio_duration import AudioDurationError, get_audio_duration_ms
from ppt2course.avatar import (
    DEFAULT_AVATAR_MODE,
    AvatarAssetSet,
    build_avatar_track,
    default_asset_set,
    should_show_avatar,
)
from ppt2course.export import ExportError, export_outputs
from ppt2course.script_cleaner import clean_script
from ppt2course.script_gen import (
    DEFAULT_GEMINI_MODEL,
    ScriptGenerationError,
    ScriptMode,
    generate_script,
)
from ppt2course.tts import DEFAULT_RATE, DEFAULT_VOLUME, TtsError, synthesize
from ppt2course.upload import PptParseError, parse_ppt
from ppt2course.video import (
    DEFAULT_AVATAR_MARGIN,
    DEFAULT_AVATAR_POSITION,
    DEFAULT_AVATAR_SIZE,
    DEFAULT_BGM_VOLUME,
    DEFAULT_FONT_SIZE,
    DEFAULT_FPS,
    DEFAULT_LOGO_MARGIN,
    DEFAULT_LOGO_OPACITY,
    DEFAULT_LOGO_POSITION,
    DEFAULT_LOGO_WIDTH,
    DEFAULT_RESOLUTION,
    DEFAULT_SUBTITLE_MARGIN_V,
    DEFAULT_TRANSITION,
    DEFAULT_TRANSITION_DURATION_MS,
    AvatarOverlay,
    BrollOverlay,
    SlideVideoInput,
    VideoComposeError,
    compose_video,
)

# Private, but video.py is this pipeline's own rendering engine and these
# are exactly the formulas that decide "how much total video time do these
# narration durations actually produce" — reusing them here (rather than
# re-deriving the same transition-safety/offset math a second time) is what
# keeps target_duration_ms's estimate from ever silently drifting out of
# sync with what compose_video will actually render.
from ppt2course.video import _effective_transition_ms, _total_duration_ms

DEFAULT_SILENT_DURATION_MS = 2000


class PipelineError(Exception):
    pass


def _broll_overlays_for_slide(
    slide_number: int, selections: list[dict], get_duration_ms
) -> tuple[BrollOverlay, ...]:
    """Convert this slide's confirmed B-roll picks into BrollOverlay objects,
    clamped to its own real (post-TTS) audio duration.

    ``get_duration_ms`` is a zero-arg callable rather than a plain int so
    that a slide with no B-roll selections never pays for (or needs to mock,
    in tests) an extra ffprobe call — this stays a complete no-op for every
    caller that doesn't use the feature, jobs.py included.

    A selection whose window no longer fits — e.g. the script (and so the
    narration length) changed after the user picked a timing in the UI — is
    dropped rather than allowed to fail the whole job: like every other
    optional add-on in this pipeline, a B-roll problem must never take down
    core video generation.
    """
    matching = [s for s in selections if s.get("slide_number") == slide_number]
    if not matching:
        return ()

    duration_ms = get_duration_ms()
    overlays = []
    for selection in matching:
        try:
            start_ms = int(selection["start_ms"])
            end_ms = min(int(selection["end_ms"]), duration_ms)
            image_path = selection["image_path"]
        except (KeyError, TypeError, ValueError):
            continue
        if start_ms < 0 or start_ms >= duration_ms or end_ms <= start_ms:
            continue
        overlays.append(BrollOverlay(image_path=image_path, start_ms=start_ms, end_ms=end_ms))
    return tuple(overlays)


def _avatar_overlays_for_slide(
    slide_number: int,
    script_text: str,
    chunks: list,
    mode: str,
    custom_slide_numbers: tuple[int, ...],
    asset_set: AvatarAssetSet,
    get_duration_ms,
) -> tuple[AvatarOverlay, ...]:
    """Build this slide's avatar mouth-flap overlays, or none at all if the
    chosen mode skips this slide. ``get_duration_ms`` is a zero-arg callable
    for the same reason as ``_broll_overlays_for_slide``'s: a slide the
    avatar mode doesn't touch never pays for an extra ffprobe call.

    Never raises — an unexpected chunk/asset problem degrades to "no avatar
    on this slide" rather than failing the whole job, matching every other
    optional visual add-on in this pipeline.
    """
    if not should_show_avatar(mode, slide_number, script_text, custom_slide_numbers):
        return ()

    try:
        duration_ms = get_duration_ms()
        segments = build_avatar_track(chunks, duration_ms)
        return tuple(
            AvatarOverlay(
                image_path=asset_set.path_for(seg.state),
                start_ms=seg.start_ms,
                end_ms=seg.end_ms,
            )
            for seg in segments
            if seg.end_ms > seg.start_ms
        )
    except (AudioDurationError, VideoComposeError):
        return ()


def _reading_pauses_for_target_duration(
    narration_durations_ms: list[int],
    transition_duration_ms: int,
    target_duration_ms: int,
    avoid_voice_overlap: bool = False,
) -> tuple[list[int], bool]:
    """Decide each slide's reading_pause_ms so the rendered video lands as
    close as possible to ``target_duration_ms``, using only reading-pause
    silence — never a TTS rate change, and never shortening any slide's
    real narration.

    Returns ``(pauses_ms, reachable)``. If the target is shorter than the
    narration-only length already produces, that length is the floor —
    ``reachable`` comes back False and every pause is 0 (the closest this
    can get without cutting into real narration audio).
    """
    n = len(narration_durations_ms)
    if n == 0:
        return [], True

    effective_transition_ms = _effective_transition_ms(
        narration_durations_ms, transition_duration_ms
    )
    narration_only_total_ms = _total_duration_ms(narration_durations_ms, effective_transition_ms)
    if avoid_voice_overlap:
        # compose_video's avoid_voice_overlap no longer lets narration audio
        # borrow transition_duration_ms back from the next slide at each of
        # the n-1 transitions — the floor this video can't shrink below is
        # that much longer than the plain crossfade estimate above.
        narration_only_total_ms += (n - 1) * effective_transition_ms
    slack_ms = target_duration_ms - narration_only_total_ms

    if slack_ms <= 0:
        return [0] * n, slack_ms == 0

    per_slide_ms = slack_ms // n
    pauses = [per_slide_ms] * n
    pauses[-1] += slack_ms - per_slide_ms * n  # remainder -> exact target
    return pauses, True


def _generate_silent_audio(out_path: str, duration_ms: int) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
        "-t", str(duration_ms / 1000),
        "-q:a", "9",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise PipelineError(f"failed to generate silent placeholder audio: {result.stderr}")


def run_pipeline(
    pptx_path: str,
    image_paths: list[str],
    work_dir: str,
    out_dir: str,
    base_name: str,
    script_mode: ScriptMode,
    voice: str,
    voice_rate: str = DEFAULT_RATE,
    voice_volume: str = DEFAULT_VOLUME,
    texts: list[str] | None = None,
    gemini_api_key: str | None = None,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    transition: str = DEFAULT_TRANSITION,
    transition_duration_ms: int = DEFAULT_TRANSITION_DURATION_MS,
    resolution: tuple[int, int] = DEFAULT_RESOLUTION,
    fps: int = DEFAULT_FPS,
    font_size: int = DEFAULT_FONT_SIZE,
    subtitle_margin_v: int = DEFAULT_SUBTITLE_MARGIN_V,
    logo_path: str | None = None,
    logo_width: int = DEFAULT_LOGO_WIDTH,
    logo_margin: int = DEFAULT_LOGO_MARGIN,
    logo_opacity: float = DEFAULT_LOGO_OPACITY,
    logo_position: str = DEFAULT_LOGO_POSITION,
    bgm_path: str | None = None,
    bgm_volume: float = DEFAULT_BGM_VOLUME,
    intro_path: str | None = None,
    outro_path: str | None = None,
    custom_dict_path: str | None = None,
    broll_selections: list[dict] | None = None,
    avatar_mode: str = DEFAULT_AVATAR_MODE,
    avatar_position: str = DEFAULT_AVATAR_POSITION,
    avatar_size: str = DEFAULT_AVATAR_SIZE,
    avatar_margin: int = DEFAULT_AVATAR_MARGIN,
    avatar_custom_slides: list[int] | None = None,
    avatar_asset_set: AvatarAssetSet | None = None,
    reading_pause_ms: int = 0,
    closing_pause_ms: int = 0,
    target_duration_ms: int | None = None,
    avoid_voice_overlap: bool = False,
) -> dict[str, str | int]:
    try:
        slides = parse_ppt(pptx_path)
    except PptParseError as exc:
        raise PipelineError(f"failed to parse PPT: {exc}") from exc

    if len(image_paths) != len(slides):
        raise PipelineError(
            f"image count ({len(image_paths)}) does not match slide count ({len(slides)})"
        )

    try:
        scripts = generate_script(
            script_mode, slides, texts=texts, api_key=gemini_api_key, model=gemini_model
        )
    except ScriptGenerationError as exc:
        raise PipelineError(f"script generation failed: {exc}") from exc

    cleaned_scripts = [clean_script(s) for s in scripts]

    os.makedirs(work_dir, exist_ok=True)

    resolved_avatar_custom_slides = tuple(avatar_custom_slides or ())
    resolved_avatar_asset_set = avatar_asset_set or default_asset_set()

    slide_inputs = []
    for slide, image_path, script_text in zip(slides, image_paths, cleaned_scripts):
        audio_path = os.path.join(work_dir, f"slide_{slide.index:03d}.mp3")

        if script_text.strip():
            try:
                chunks = synthesize(script_text, voice, audio_path, rate=voice_rate, volume=voice_volume)
            except TtsError as exc:
                raise PipelineError(
                    f"TTS synthesis failed for slide {slide.index}: {exc}"
                ) from exc
        else:
            _generate_silent_audio(audio_path, DEFAULT_SILENT_DURATION_MS)
            chunks = []

        broll_overlays = _broll_overlays_for_slide(
            slide.index, broll_selections or [], lambda p=audio_path: get_audio_duration_ms(p)
        )
        avatar_overlays = _avatar_overlays_for_slide(
            slide.index,
            script_text,
            chunks,
            avatar_mode,
            resolved_avatar_custom_slides,
            resolved_avatar_asset_set,
            lambda p=audio_path: get_audio_duration_ms(p),
        )
        slide_inputs.append(
            SlideVideoInput(
                image_path=image_path,
                audio_path=audio_path,
                chunks=chunks,
                broll_overlays=broll_overlays,
                avatar_overlays=avatar_overlays,
            )
        )

    # Reading pause is purely additive on top of each slide's already-final
    # audio/chunks/overlays above — nothing about how those were built
    # changes here, only how long the slide's *picture* (and, via silent
    # padding inside compose_video, its audio track) holds before the next
    # slide's transition begins.
    target_duration_reachable = None
    if target_duration_ms is not None:
        # Only this mode needs each slide's real narration length measured
        # up front (the flat reading_pause_ms/closing_pause_ms path below
        # needs no such measurement — apad's whole_dur figures the padding
        # out from the real file at render time) — and only this mode's
        # explicit reading_pause_ms/closing_pause_ms inputs are overridden
        # by the auto-computed distribution, so the two levers can't
        # silently fight each other over the same slide.
        narration_durations_ms = [get_audio_duration_ms(si.audio_path) for si in slide_inputs]
        pauses_ms, target_duration_reachable = _reading_pauses_for_target_duration(
            narration_durations_ms,
            transition_duration_ms,
            target_duration_ms,
            avoid_voice_overlap=avoid_voice_overlap,
        )
        slide_inputs = [
            dataclasses.replace(si, reading_pause_ms=p) for si, p in zip(slide_inputs, pauses_ms)
        ]
    elif reading_pause_ms > 0 or closing_pause_ms > 0:
        slide_inputs = [
            dataclasses.replace(
                si,
                reading_pause_ms=reading_pause_ms + (closing_pause_ms if i == len(slide_inputs) - 1 else 0),
            )
            for i, si in enumerate(slide_inputs)
        ]

    video_path = os.path.join(work_dir, "course.mp4")
    srt_path = os.path.join(work_dir, "course.srt")

    try:
        compose_video(
            slide_inputs,
            video_path,
            srt_path,
            transition=transition,
            transition_duration_ms=transition_duration_ms,
            resolution=resolution,
            fps=fps,
            font_size=font_size,
            subtitle_margin_v=subtitle_margin_v,
            logo_path=logo_path,
            logo_width=logo_width,
            logo_margin=logo_margin,
            logo_opacity=logo_opacity,
            logo_position=logo_position,
            bgm_path=bgm_path,
            bgm_volume=bgm_volume,
            intro_path=intro_path,
            outro_path=outro_path,
            custom_dict_path=custom_dict_path,
            avatar_position=avatar_position,
            avatar_size=avatar_size,
            avatar_margin=avatar_margin,
            avoid_voice_overlap=avoid_voice_overlap,
        )
    except VideoComposeError as exc:
        raise PipelineError(f"video composition failed: {exc}") from exc

    try:
        outputs = export_outputs(video_path, srt_path, cleaned_scripts, out_dir, base_name)
    except ExportError as exc:
        raise PipelineError(f"export failed: {exc}") from exc

    # Surfaced on the results screen ("影片容量/時長/講稿字數") once the job
    # is done — measured off the actual exported mp4 (not the work_dir copy)
    # so this always matches what the user is about to download.
    outputs["video_size_bytes"] = os.path.getsize(outputs["mp4"])
    outputs["video_duration_ms"] = get_audio_duration_ms(outputs["mp4"])
    outputs["script_char_count"] = sum(len(s) for s in cleaned_scripts)
    if target_duration_reachable is not None:
        # False means the requested target was shorter than the real
        # narration-only length already produces — video_duration_ms above
        # is then the shortest this deck can honestly get without cutting
        # into narration audio, not the requested target.
        outputs["target_duration_reachable"] = target_duration_reachable
    return outputs
