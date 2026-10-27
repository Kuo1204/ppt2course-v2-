// Orchestrates the whole client-side video-assembly pipeline: TTS -> per-
// slide ffmpeg segment (image + narration + pause, subtitle burned in) ->
// concat/crossfade into the main body -> logo overlay -> text watermark ->
// bgm mix -> intro/outro concat -> SRT + DOCX. Mirrors the *order* and
// *visual intent* of src/ppt2course/video.py's compose_video, not its
// literal code -- see BUILD_NOTES.md for the specific simplifications.
import { getFFmpeg, fetchFile } from "./ffmpeg.js";
import { synthesizeToWav } from "./tts.js";
import { wrapWithDictionary, parseDictionary } from "./wrap.js";
import { effectiveTransitionMs } from "./pacing.js";
import { buildSrt } from "./srt.js";
import { buildTranscriptDocx } from "./docx.js";

const FONT_URL =
  "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanstc/NotoSansTC%5Bwght%5D.ttf";
const FPS = 30;
const MIN_SLIDE_HOLD_MS = 500;

let fontWrittenOnThisFFmpeg = null; // weak marker: the FFmpeg instance we've already written font.ttf to

function hexToFfmpegColor(hex) {
  const clean = (hex || "#FFFFFF").replace("#", "");
  return `0x${clean}`;
}

// Escapes drawtext's `text=` value the same way video.py's
// _escape_drawtext_text does (backslash, colon, percent, single quote),
// plus a comma (which would otherwise terminate the filter early inside the
// -vf chain) -- while leaving literal newline characters alone, since those
// are how multi-line wrapped subtitles are expressed to drawtext.
function escapeDrawtext(text) {
  return text
    .replace(/\\/g, "\\\\")
    .replace(/:/g, "\\:")
    .replace(/%/g, "\\%")
    .replace(/,/g, "\\,")
    .replace(/'/g, "'\\''");
}

function parseResolution(resolutionValue) {
  const [w, h] = resolutionValue.split("x").map(Number);
  return { width: w, height: h };
}

async function ensureFont(ffmpeg, onProgress) {
  if (fontWrittenOnThisFFmpeg === ffmpeg) return;
  onProgress("下載中文字幕字型（僅需一次，之後由瀏覽器快取）...");
  const resp = await fetch(FONT_URL);
  if (!resp.ok) throw new Error(`無法下載字幕字型：HTTP ${resp.status}`);
  const bytes = new Uint8Array(await resp.arrayBuffer());
  await ffmpeg.writeFile("font.ttf", bytes);
  fontWrittenOnThisFFmpeg = ffmpeg;
}

function extOf(file) {
  const m = /\.([a-zA-Z0-9]+)$/.exec(file.name || "");
  return m ? m[1].toLowerCase() : "png";
}

// Builds one slide's standalone .mp4 segment: the still image, scaled/
// padded to the output resolution, held for exactly the narration (rate-
// adjusted) + reading-pause duration, with the wrapped subtitle burned in
// via drawtext. Returns { fileName, durationMs }.
async function buildSlideSegment(ffmpeg, opts) {
  const {
    index,
    imageFile,
    text,
    rate,
    volume,
    readingPauseMs,
    width,
    height,
    subtitle,
    dictTerms,
  } = opts;

  const imgName = `slide_src_${index}.${extOf(imageFile)}`;
  const wavName = `nar_src_${index}.wav`;
  const adjWavName = `nar_adj_${index}.wav`;
  const segName = `seg_${index}.mp4`;

  await ffmpeg.writeFile(imgName, await fetchFile(imageFile));

  const { wavBytes, durationMs: rawDurationMs } = await synthesizeToWav(text, () => {});
  await ffmpeg.writeFile(wavName, wavBytes);

  // RATE_MIN/MAX (-50..+100) and VOLUME_MIN/MAX (-50..+50) from the real
  // app map exactly onto ffmpeg atempo's valid single-filter range
  // [0.5, 2.0] and a sane volume multiplier range, so no filter-chaining
  // is needed for either.
  const atempo = Math.min(2.0, Math.max(0.5, 1 + rate / 100));
  const volumeFactor = Math.min(2.0, Math.max(0, 1 + volume / 100));
  const adjustedNarrationMs = rawDurationMs / atempo;
  const pauseMs = Math.max(0, Math.round(readingPauseMs || 0));
  const visualDurationMs = adjustedNarrationMs + pauseMs;

  // Rate/volume-adjust the narration, then append reading-pause silence so
  // the segment's own audio track is already exactly as long as the slide
  // should stay on screen -- `-shortest` against the looped still image
  // then naturally yields that exact segment duration.
  if (pauseMs > 0) {
    await ffmpeg.exec([
      "-i", wavName,
      "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
      "-filter_complex",
      `[0:a]atempo=${atempo.toFixed(4)},volume=${volumeFactor.toFixed(4)}[a0];` +
        `[1:a]atrim=duration=${(pauseMs / 1000).toFixed(3)}[a1];` +
        `[a0][a1]concat=n=2:v=0:a=1[aout]`,
      "-map", "[aout]",
      adjWavName,
    ]);
  } else {
    await ffmpeg.exec([
      "-i", wavName,
      "-filter:a", `atempo=${atempo.toFixed(4)},volume=${volumeFactor.toFixed(4)}`,
      adjWavName,
    ]);
  }

  const maxCharsPerLine = Math.max(6, Math.floor((width * 0.86) / subtitle.fontSizePx));
  const lines = wrapWithDictionary(text, maxCharsPerLine, dictTerms);
  const marginV = Math.round((height * subtitle.positionPercent) / 100);
  const borderW = Math.max(1, Math.round(subtitle.fontSizePx / 32)) + (subtitle.bold ? 2 : 0);
  const lineSpacing = Math.round(subtitle.fontSizePx * 0.25);

  const vfParts = [
    `scale=${width}:${height}:force_original_aspect_ratio=decrease`,
    `pad=${width}:${height}:(ow-iw)/2:(oh-ih)/2:color=black`,
    "setsar=1",
    `fps=${FPS}`,
  ];
  if (lines.length > 0) {
    const escapedText = escapeDrawtext(lines.join("\n"));
    vfParts.push(
      `drawtext=fontfile=font.ttf:text='${escapedText}':fontsize=${subtitle.fontSizePx}:` +
        `fontcolor=${hexToFfmpegColor(subtitle.fontColor)}:bordercolor=${hexToFfmpegColor(subtitle.outlineColor)}:` +
        `borderw=${borderW}:line_spacing=${lineSpacing}:x=(w-text_w)/2:y=h-text_h-${marginV}`
    );
  }

  // IMPORTANT: `-shortest` alone does NOT reliably cut a `-loop 1` still-
  // image input down to the audio's length in this ffmpeg.wasm build
  // (single-threaded ffmpeg-core 0.12.6, ffmpeg 5.1.4) -- confirmed by an
  // isolated test where it produced the same ~6.03s output regardless of
  // a 4.70s input audio track (and regardless of whether drawtext was
  // present). An explicit `-t <seconds>` sidesteps that unreliable
  // shortest-stream inference entirely; `-shortest` is left in too as a
  // harmless belt-and-suspenders in case the exact frame count rounds
  // either input a few ms short.
  const visualDurationSec = (visualDurationMs / 1000).toFixed(3);
  await ffmpeg.exec([
    "-loop", "1",
    "-i", imgName,
    "-i", adjWavName,
    "-vf", vfParts.join(","),
    "-t", visualDurationSec,
    "-r", String(FPS),
    "-c:v", "libx264",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    "-shortest",
    segName,
  ]);

  return { fileName: segName, durationMs: visualDurationMs };
}

// Concatenates segments with a straight cut (no overlap) via the concat
// demuxer -- used when transitionDurationMs is 0, or when "avoid voice
// overlap" is on (see BUILD_NOTES.md for why that option forces a cut
// instead of trying to replicate the real backend's fade-video-but-not-
// audio nuance).
async function concatCut(ffmpeg, segmentNames, outName) {
  const listText = segmentNames.map((n) => `file '${n}'`).join("\n");
  await ffmpeg.writeFile("concat_list.txt", new TextEncoder().encode(listText));
  await ffmpeg.exec([
    "-f", "concat",
    "-safe", "0",
    "-i", "concat_list.txt",
    "-c", "copy",
    outName,
  ]);
}

// Pairwise xfade/acrossfade accumulation: fold segments together one at a
// time (accum = xfade(accum, next)) rather than one giant N-input filter
// graph, so ffmpeg.wasm only ever has two inputs open at once and progress
// can be reported incrementally.
async function concatCrossfade(ffmpeg, segments, transitionName, requestedMs, onProgress) {
  const durations = segments.map((s) => s.durationMs);
  const effectiveMs = effectiveTransitionMs(durations, requestedMs);

  let accumName = segments[0].fileName;
  let accumDurationMs = durations[0];

  for (let i = 1; i < segments.length; i += 1) {
    onProgress(`合併轉場 ${i}/${segments.length - 1}...`);
    const segDurationMs = durations[i];
    const pairMs = Math.max(
      0,
      Math.min(effectiveMs, Math.floor(Math.min(accumDurationMs, segDurationMs) / 2) - MIN_SLIDE_HOLD_MS)
    );
    const outName = `accum_${i}.mp4`;
    if (pairMs <= 0) {
      // Not enough room for any crossfade between this pair -- fall back
      // to a plain concat for just this join.
      await concatCut(ffmpeg, [accumName, segments[i].fileName], outName);
      accumDurationMs = accumDurationMs + segDurationMs;
    } else {
      const transitionSec = (pairMs / 1000).toFixed(3);
      const offsetSec = ((accumDurationMs - pairMs) / 1000).toFixed(3);
      await ffmpeg.exec([
        "-i", accumName,
        "-i", segments[i].fileName,
        "-filter_complex",
        `[0:v][1:v]xfade=transition=${transitionName}:duration=${transitionSec}:offset=${offsetSec}[v];` +
          `[0:a][1:a]acrossfade=d=${transitionSec}:c1=tri:c2=tri[a]`,
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        outName,
      ]);
      accumDurationMs = accumDurationMs + segDurationMs - pairMs;
    }
    accumName = outName;
  }
  return accumName;
}

async function addLogoOverlay(ffmpeg, inName, outName, logoFile, { opacity, position, widthPx, marginPx }) {
  const logoName = `logo.${extOf(logoFile)}`;
  await ffmpeg.writeFile(logoName, await fetchFile(logoFile));
  const overlays = {
    "top-left": [`${marginPx}`, `${marginPx}`],
    "top-right": [`W-w-${marginPx}`, `${marginPx}`],
    "bottom-left": [`${marginPx}`, `H-h-${marginPx}`],
    "bottom-right": [`W-w-${marginPx}`, `H-h-${marginPx}`],
  };
  const [x, y] = overlays[position] || overlays["top-right"];
  await ffmpeg.exec([
    "-i", inName,
    "-i", logoName,
    "-filter_complex",
    `[1:v]scale=${widthPx}:-1,format=rgba,colorchannelmixer=aa=${opacity.toFixed(2)}[logo];[0:v][logo]overlay=${x}:${y}`,
    "-c:a", "copy",
    "-pix_fmt", "yuv420p",
    outName,
  ]);
}

async function addTextWatermark(ffmpeg, inName, outName, { text, xPercent, yPercent, opacity, fontSizePx }) {
  const escapedText = escapeDrawtext(text);
  const outlineOpacity = Math.min(opacity + 0.3, 1.0);
  const xExpr = `(w-text_w)*${(xPercent / 100).toFixed(3)}`;
  const yExpr = `(h-text_h)*${(yPercent / 100).toFixed(3)}`;
  await ffmpeg.exec([
    "-i", inName,
    "-vf",
    `drawtext=fontfile=font.ttf:text='${escapedText}':fontsize=${fontSizePx}:` +
      `fontcolor=white@${opacity.toFixed(2)}:bordercolor=black@${outlineOpacity.toFixed(2)}:borderw=2:x=${xExpr}:y=${yExpr}`,
    "-c:a", "copy",
    "-pix_fmt", "yuv420p",
    outName,
  ]);
}

async function mixBgm(ffmpeg, inName, outName, bgmFile, bgmVolume) {
  const bgmName = `bgm.${extOf(bgmFile)}`;
  await ffmpeg.writeFile(bgmName, await fetchFile(bgmFile));
  await ffmpeg.exec([
    "-i", inName,
    "-stream_loop", "-1",
    "-i", bgmName,
    "-filter_complex",
    `[1:a]volume=${bgmVolume.toFixed(2)}[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]`,
    "-map", "0:v",
    "-map", "[aout]",
    "-c:v", "copy",
    "-c:a", "aac",
    "-shortest",
    outName,
  ]);
}

async function concatWithIntroOutro(ffmpeg, mainName, outName, introFile, outroFile, width, height) {
  const inputs = [];
  if (introFile) inputs.push({ label: "intro", name: `intro.${extOf(introFile)}`, file: introFile });
  inputs.push({ label: "main", name: mainName, file: null });
  if (outroFile) inputs.push({ label: "outro", name: `outro.${extOf(outroFile)}`, file: outroFile });

  for (const input of inputs) {
    if (input.file) await ffmpeg.writeFile(input.name, await fetchFile(input.file));
  }

  const args = [];
  for (const input of inputs) args.push("-i", input.name);

  const filterParts = [];
  const mapLabels = [];
  inputs.forEach((input, i) => {
    filterParts.push(
      `[${i}:v]scale=${width}:${height}:force_original_aspect_ratio=decrease,` +
        `pad=${width}:${height}:(ow-iw)/2:(oh-ih)/2:color=black,fps=${FPS},setsar=1,format=yuv420p[v${i}];` +
        `[${i}:a]aformat=sample_rates=44100:channel_layouts=stereo[a${i}]`
    );
    mapLabels.push(`[v${i}][a${i}]`);
  });
  filterParts.push(`${mapLabels.join("")}concat=n=${inputs.length}:v=1:a=1[outv][outa]`);

  args.push(
    "-filter_complex", filterParts.join(";"),
    "-map", "[outv]",
    "-map", "[outa]",
    "-c:v", "libx264",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    outName
  );
  await ffmpeg.exec(args);
}

// Computes the on-screen timeline offsets (for the .srt) the same way the
// render does: a straight cut has no overlap; a crossfade overlaps adjacent
// slides by the same effective transition length used for rendering.
function computeTimelineOffsets(durationsMs, effectiveMs) {
  const offsets = [0];
  for (let i = 1; i < durationsMs.length; i += 1) {
    offsets.push(offsets[i - 1] + durationsMs[i - 1] - effectiveMs);
  }
  return offsets;
}

// state: see app.js for the exact shape collected from the form.
export async function runPipeline(state, onProgress) {
  const { width, height } = parseResolution(state.resolution);
  const n = state.slideTexts.length;
  const dictTerms = parseDictionary(state.customDict);

  onProgress("載入 ffmpeg.wasm...", 0.02);
  const ffmpeg = await getFFmpeg((msg) => {
    console.debug("[ffmpeg]", msg);
    onProgress(msg, null, true);
  });
  await ensureFont(ffmpeg, (msg) => onProgress(msg, 0.04));

  const useCut = state.avoidVoiceOverlap || state.transitionDurationMs <= 0;

  const segments = [];
  for (let i = 0; i < n; i += 1) {
    onProgress(`產生第 ${i + 1}/${n} 頁語音與畫面...`, 0.05 + (0.55 * i) / n);
    const seg = await buildSlideSegment(ffmpeg, {
      index: i,
      imageFile: state.imageFiles[i],
      text: state.slideTexts[i],
      rate: state.voiceRate,
      volume: state.voiceVolume,
      readingPauseMs: state.readingPauseMsPerSlide[i],
      width,
      height,
      subtitle: state.subtitle,
      dictTerms,
    });
    segments.push(seg);
  }

  onProgress("合併投影片片段...", 0.62);
  let mainName;
  let effectiveMsForSrt;
  const durations = segments.map((s) => s.durationMs);
  if (useCut || n === 1) {
    mainName = "main_cut.mp4";
    await concatCut(ffmpeg, segments.map((s) => s.fileName), mainName);
    effectiveMsForSrt = 0;
  } else {
    effectiveMsForSrt = effectiveTransitionMs(durations, state.transitionDurationMs);
    mainName = await concatCrossfade(ffmpeg, segments, state.transition, state.transitionDurationMs, (msg) =>
      onProgress(msg, 0.65)
    );
  }

  let current = mainName;

  if (state.watermarkDecision === "yes" && state.logoFile) {
    onProgress("加入圖片浮水印（Logo）...", 0.75);
    const next = "with_logo.mp4";
    await addLogoOverlay(ffmpeg, current, next, state.logoFile, state.logo);
    current = next;
  }

  if (state.watermarkDecision === "yes" && state.watermarkEnabled && state.watermarkText.trim()) {
    onProgress("加入文字浮水印...", 0.8);
    const next = "with_watermark.mp4";
    await addTextWatermark(ffmpeg, current, next, state.watermark);
    current = next;
  }

  if (state.bgmFile) {
    onProgress("混合背景音樂...", 0.85);
    const next = "with_bgm.mp4";
    await mixBgm(ffmpeg, current, next, state.bgmFile, state.bgmVolume);
    current = next;
  }

  if (state.introFile || state.outroFile) {
    onProgress("加入片頭／片尾...", 0.9);
    const next = "final.mp4";
    await concatWithIntroOutro(ffmpeg, current, next, state.introFile, state.outroFile, width, height);
    current = next;
  }

  onProgress("讀取輸出影片...", 0.94);
  const mp4Data = await ffmpeg.readFile(current);
  const mp4Blob = new Blob([mp4Data.buffer], { type: "video/mp4" });

  onProgress("產生字幕檔 (.srt)...", 0.96);
  const offsets = computeTimelineOffsets(durations, effectiveMsForSrt);
  const cues = state.slideTexts.map((text, i) => {
    const maxCharsPerLine = Math.max(6, Math.floor((width * 0.86) / state.subtitle.fontSizePx));
    const lines = wrapWithDictionary(text, maxCharsPerLine, dictTerms);
    return {
      startMs: offsets[i],
      endMs: offsets[i] + durations[i],
      lines: lines.length ? lines : [""],
    };
  });
  const srtText = buildSrt(cues);

  onProgress("產生講稿 Word 檔 (.docx)...", 0.98);
  const docxBlob = await buildTranscriptDocx(state.baseName || "課程", state.slideTexts);

  onProgress("完成！", 1);
  return { mp4Blob, srtText, docxBlob };
}
