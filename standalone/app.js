import { parseNumberedScript } from "./lib/scriptParser.js";
import { runPipeline } from "./lib/pipeline.js";
import {
  scaledFontSize,
  scaledLogoWidth,
  scaledLogoMargin,
  FONT_SIZES,
  LOGO_SIZES,
  TRANSITIONS,
  RESOLUTIONS,
  LOGO_POSITIONS,
} from "./lib/scale.js";
import { readingPausesForTargetDuration } from "./lib/pacing.js";

const $ = (id) => document.getElementById(id);

window.addEventListener("error", (e) => console.error("[window error]", e.message, e.filename, e.lineno));
window.addEventListener("unhandledrejection", (e) => console.error("[unhandled rejection]", e.reason));

// ---- static select population -------------------------------------------
function fillSelect(el, options, selectedValue) {
  el.innerHTML = "";
  for (const opt of options) {
    const o = document.createElement("option");
    o.value = opt.value;
    o.textContent = opt.label;
    if (opt.value === selectedValue) o.selected = true;
    el.appendChild(o);
  }
}

fillSelect($("transition-select"), TRANSITIONS, "fade");
fillSelect(
  $("transition-duration-select"),
  [0, 300, 500, 800, 1000, 1500, 2000].map((v) => ({ value: v, label: v === 0 ? "無轉場（直接切換）" : `${v} ms` })),
  500
);
fillSelect($("resolution-select"), RESOLUTIONS, "1920x1080");
fillSelect($("font-size-select"), FONT_SIZES, 64);
fillSelect($("logo-position"), LOGO_POSITIONS, "top-right");
fillSelect($("logo-size"), LOGO_SIZES, 160);

// ---- app state -------------------------------------------------------------
const state = {
  imageFiles: [],
  thumbUrls: [],
  scriptMode: "paste", // "paste" | "perSlide"
  slideTexts: [],
  watermarkDecision: null, // null | "yes" | "no"
};

// ---- Step 1: slides --------------------------------------------------------
$("slide-input").addEventListener("change", (e) => {
  state.imageFiles = Array.from(e.target.files || []);
  state.thumbUrls.forEach((u) => URL.revokeObjectURL(u));
  state.thumbUrls = state.imageFiles.map((f) => URL.createObjectURL(f));
  renderThumbs();
  syncSlideTextCount();
  renderPerSlideTextareas();
});

function renderThumbs() {
  const countHint = $("slide-count-hint");
  countHint.textContent = state.imageFiles.length > 0 ? `已上傳 ${state.imageFiles.length} 張投影片` : "尚未上傳任何圖片";
  const grid = $("slide-thumbs");
  grid.innerHTML = "";
  state.thumbUrls.forEach((url, i) => {
    const div = document.createElement("div");
    div.className = "thumb-item";
    div.innerHTML = `<img src="${url}" alt="第${i + 1}頁" /><span>第 ${i + 1} 頁</span>`;
    grid.appendChild(div);
  });
}

function syncSlideTextCount() {
  const n = state.imageFiles.length;
  const next = new Array(n).fill("");
  for (let i = 0; i < Math.min(n, state.slideTexts.length); i += 1) next[i] = state.slideTexts[i];
  state.slideTexts = next;
}

// ---- Step 2: script source --------------------------------------------------
document.querySelectorAll("#step2 .toggle-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#step2 .toggle-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.scriptMode = btn.dataset.mode;
    $("paste-mode-panel").style.display = state.scriptMode === "paste" ? "" : "none";
    $("per-slide-mode-panel").style.display = state.scriptMode === "perSlide" ? "" : "none";
  });
});
document.querySelector('#step2 .toggle-btn[data-mode="paste"]').classList.add("active");

$("paste-parse-btn").addEventListener("click", () => {
  const raw = $("paste-textarea").value;
  const errorEl = $("paste-error");
  if (state.imageFiles.length === 0) {
    errorEl.textContent = "請先在 Step 01 上傳投影片圖片。";
    return;
  }
  const result = parseNumberedScript(raw, state.imageFiles.length);
  if (result.error) {
    errorEl.textContent = result.error;
    return;
  }
  errorEl.textContent = "";
  state.slideTexts = result.texts;
  state.scriptMode = "perSlide";
  document.querySelectorAll("#step2 .toggle-btn").forEach((b) => b.classList.remove("active"));
  document.querySelector('#step2 .toggle-btn[data-mode="perSlide"]').classList.add("active");
  $("paste-mode-panel").style.display = "none";
  $("per-slide-mode-panel").style.display = "";
  renderPerSlideTextareas();
});

function renderPerSlideTextareas() {
  const container = $("per-slide-textareas");
  container.innerHTML = "";
  state.imageFiles.forEach((_, i) => {
    const wrap = document.createElement("div");
    wrap.className = "per-slide-item";
    const label = document.createElement("label");
    label.textContent = `第 ${i + 1} 頁講稿`;
    const textarea = document.createElement("textarea");
    textarea.rows = 3;
    textarea.value = state.slideTexts[i] || "";
    textarea.addEventListener("input", () => {
      state.slideTexts[i] = textarea.value;
    });
    wrap.appendChild(label);
    wrap.appendChild(textarea);
    container.appendChild(wrap);
  });
}

// ---- Step 3: voice & transition ---------------------------------------------
$("rate-range").addEventListener("input", (e) => {
  $("rate-label").textContent = `${e.target.value >= 0 ? "+" : ""}${e.target.value}%`;
});
$("volume-range").addEventListener("input", (e) => {
  $("volume-label").textContent = `${e.target.value >= 0 ? "+" : ""}${e.target.value}%`;
});

// ---- Step 4: watermark decision ---------------------------------------------
$("watermark-yes-btn").addEventListener("click", () => {
  state.watermarkDecision = "yes";
  $("watermark-yes-btn").classList.add("active");
  $("watermark-no-btn").classList.remove("active");
  $("watermark-detail").style.display = "";
});
$("watermark-no-btn").addEventListener("click", () => {
  state.watermarkDecision = "no";
  $("watermark-no-btn").classList.add("active");
  $("watermark-yes-btn").classList.remove("active");
  $("watermark-detail").style.display = "none";
});

const liveLabelPairs = [
  ["logo-opacity", "logo-opacity-label", (v) => `${v}%`],
  ["watermark-x", "watermark-x-label", (v) => `${v}%`],
  ["watermark-y", "watermark-y-label", (v) => `${v}%`],
  ["watermark-opacity", "watermark-opacity-label", (v) => `${v}%`],
  ["watermark-font-size", "watermark-font-size-label", (v) => `${v}px`],
  ["subtitle-position", "subtitle-position-label", (v) => `${v}%`],
  ["reading-pause", "reading-pause-label", (v) => `${Number(v).toFixed(1)} 秒`],
  ["closing-pause", "closing-pause-label", (v) => `${Number(v).toFixed(1)} 秒`],
  ["bgm-volume", "bgm-volume-label", (v) => `${v}%`],
];
for (const [inputId, labelId, fmt] of liveLabelPairs) {
  const input = $(inputId);
  const label = $(labelId);
  input.addEventListener("input", () => {
    label.textContent = fmt(input.value);
  });
}

// ---- Step 5: generate --------------------------------------------------------
function appendLog(message) {
  const log = $("progress-log");
  const line = document.createElement("div");
  line.textContent = message;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function setProgress(fraction) {
  if (fraction == null) return;
  $("progress-bar-inner").style.width = `${Math.round(fraction * 100)}%`;
}

function validate() {
  const errors = [];
  if (state.imageFiles.length === 0) errors.push("請先上傳投影片圖片（Step 01）。");
  if (state.slideTexts.length !== state.imageFiles.length) errors.push("講稿頁數與投影片頁數不一致，請重新確認（Step 02）。");
  const blankSlides = state.slideTexts
    .map((t, i) => (!t || !t.trim() ? i + 1 : null))
    .filter((v) => v !== null);
  if (blankSlides.length > 0) {
    errors.push(`第 ${blankSlides.join("、")} 頁尚未輸入講稿，請完成每一頁的講稿後再產生影片。`);
  }
  if (state.watermarkDecision !== "yes" && state.watermarkDecision !== "no") {
    errors.push("請在 Step 04 選擇是否要加上浮水印。");
  } else if (state.watermarkDecision === "yes") {
    const logoFile = $("logo-input").files[0] || null;
    const wmEnabled = $("watermark-enabled").checked;
    const wmText = $("watermark-text").value.trim();
    if (!logoFile && !(wmEnabled && wmText)) {
      errors.push("已選擇「要」加浮水印，請至少設定圖片浮水印或文字浮水印其中一項。");
    }
  }
  return errors;
}

function collectState() {
  const resolution = $("resolution-select").value;
  const [width] = resolution.split("x").map(Number);
  const fontSizeRef = Number($("font-size-select").value);
  const [, height] = resolution.split("x").map(Number);

  const readingPauseSec = Number($("reading-pause").value);
  const closingPauseSec = Number($("closing-pause").value);
  const targetDurationEnabled = $("target-duration-enabled").checked;
  const targetDurationSec = Number($("target-duration").value);
  const avoidVoiceOverlap = $("avoid-voice-overlap").checked;
  const transitionDurationMs = Number($("transition-duration-select").value);

  // reading-pause-per-slide is resolved after TTS raw durations are known
  // when targetDurationEnabled; otherwise it's the flat value (+closing
  // pause on the last slide only), mirroring pipeline.py's two branches.
  const n = state.imageFiles.length;
  const flatPauses = new Array(n).fill(Math.round(readingPauseSec * 1000));
  if (n > 0) flatPauses[n - 1] += Math.round(closingPauseSec * 1000);

  return {
    imageFiles: state.imageFiles,
    slideTexts: state.slideTexts,
    baseName: $("base-name-input").value.trim() || "課程",
    voiceRate: Number($("rate-range").value),
    voiceVolume: Number($("volume-range").value),
    transition: $("transition-select").value,
    transitionDurationMs,
    resolution,
    avoidVoiceOverlap,
    targetDurationEnabled,
    targetDurationSec,
    readingPauseMsPerSlide: flatPauses, // overwritten below if target-duration mode is on
    customDict: $("custom-dict").value,
    subtitle: {
      fontSizePx: scaledFontSize(fontSizeRef, width, height),
      positionPercent: Number($("subtitle-position").value),
      fontColor: $("subtitle-font-color").value,
      outlineColor: $("subtitle-outline-color").value,
      bold: $("subtitle-bold").checked,
    },
    watermarkDecision: state.watermarkDecision,
    logoFile: $("logo-input").files[0] || null,
    logo: {
      opacity: Number($("logo-opacity").value) / 100,
      position: $("logo-position").value,
      widthPx: scaledLogoWidth(Number($("logo-size").value), width),
      marginPx: scaledLogoMargin(width),
    },
    watermarkEnabled: $("watermark-enabled").checked,
    watermarkText: $("watermark-text").value,
    watermark: {
      text: $("watermark-text").value,
      xPercent: Number($("watermark-x").value),
      yPercent: Number($("watermark-y").value),
      opacity: Number($("watermark-opacity").value) / 100,
      fontSizePx: scaledFontSize(Number($("watermark-font-size").value), width, height),
    },
    bgmFile: $("bgm-input").files[0] || null,
    bgmVolume: Number($("bgm-volume").value) / 100,
    introFile: $("intro-input").files[0] || null,
    outroFile: $("outro-input").files[0] || null,
  };
}

$("generate-btn").addEventListener("click", async () => {
  const errors = validate();
  const summary = $("validation-summary");
  if (errors.length > 0) {
    summary.textContent = errors.join(" ");
    summary.classList.add("error");
    return;
  }
  summary.textContent = "";
  summary.classList.remove("error");

  const jobState = collectState();

  // Resolve the target-duration auto-distribute using the real app's
  // pacing formula. This needs each slide's *raw* narration duration,
  // which isn't known until TTS runs -- so for the target-duration mode we
  // do a first pass estimate using a rough chars-per-second rate, then let
  // pipeline.js's own per-slide durations stand (the UI control is a best-
  // effort target, same as the real backend's "可能無法完全達成" framing).
  if (jobState.targetDurationEnabled) {
    const roughMsPerChar = 260; // rough Mandarin TTS speaking rate estimate
    const roughDurations = jobState.slideTexts.map((t) => Math.max(600, t.trim().length * roughMsPerChar));
    const { pausesMs } = readingPausesForTargetDuration(
      roughDurations,
      jobState.transitionDurationMs,
      jobState.targetDurationSec * 1000,
      jobState.avoidVoiceOverlap
    );
    jobState.readingPauseMsPerSlide = pausesMs;
  }

  $("generate-btn").disabled = true;
  $("progress-panel").style.display = "";
  $("result-panel").style.display = "none";
  $("progress-log").innerHTML = "";
  setProgress(0);

  try {
    const { mp4Blob, srtText, docxBlob } = await runPipeline(jobState, (message, fraction, isFfmpegLog) => {
      if (!isFfmpegLog) appendLog(message);
      setProgress(fraction);
    });

    const srtBlob = new Blob([srtText], { type: "text/plain;charset=utf-8" });
    const mp4Url = URL.createObjectURL(mp4Blob);
    const srtUrl = URL.createObjectURL(srtBlob);
    const docxUrl = URL.createObjectURL(docxBlob);

    const base = jobState.baseName;
    const mp4Link = $("download-mp4");
    mp4Link.href = mp4Url;
    mp4Link.download = `${base}.mp4`;
    const srtLink = $("download-srt");
    srtLink.href = srtUrl;
    srtLink.download = `${base}.srt`;
    const docxLink = $("download-docx");
    docxLink.href = docxUrl;
    docxLink.download = `${base}.docx`;

    $("result-panel").style.display = "";
    appendLog("全部完成，可以下載檔案了。");
  } catch (err) {
    console.error(err);
    appendLog(`發生錯誤：${err.message || err}`);
    summary.textContent = `產生失敗：${err.message || err}`;
    summary.classList.add("error");
  } finally {
    $("generate-btn").disabled = false;
  }
});
