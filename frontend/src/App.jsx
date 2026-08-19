import { useEffect, useMemo, useRef, useState } from "react";
import QRCode from "qrcode";
import "./App.css";
import {
  analyzeVisuals,
  createJob,
  downloadUrl,
  extractScriptText,
  fetchPptxNotes,
  fetchPptxPreview,
  fetchVoicePreview,
  generateScriptPreview,
  getJobStatus,
  searchMedia,
} from "./api";
import { parseNumberedScript } from "./scriptParser";
import { resolveDownloadUrl } from "./urlUtils";

const SCRIPT_MODES = [
  { value: "OWN", label: "我自己輸入逐頁講稿", needsTexts: true, needsApiKey: false },
  { value: "NOTES", label: "使用投影片備忘稿", needsTexts: false, needsApiKey: false },
  { value: "AUTO", label: "AI 自動生成講稿", needsTexts: false, needsApiKey: true },
  { value: "POLISH", label: "AI 潤飾我輸入的講稿", needsTexts: true, needsApiKey: true },
];

const VOICES = [
  { value: "zh-TW-HsiaoChenNeural", label: "曉臻 (繁中·女聲)" },
  { value: "zh-TW-YunJheNeural", label: "雲哲 (繁中·男聲)" },
  { value: "zh-CN-XiaoxiaoNeural", label: "曉曉 (簡中·女聲)" },
  { value: "zh-CN-YunxiNeural", label: "雲希 (簡中·男聲)" },
  { value: "en-US-AriaNeural", label: "Aria (English·Female)" },
];

const TRANSITIONS = [
  { value: "fade", label: "淡入淡出 (fade)" },
  { value: "dissolve", label: "溶解 (dissolve)" },
  { value: "wipeleft", label: "向左擦除 (wipeleft)" },
  { value: "wiperight", label: "向右擦除 (wiperight)" },
  { value: "slideleft", label: "向左滑動 (slideleft)" },
  { value: "slideright", label: "向右滑動 (slideright)" },
  { value: "circlecrop", label: "圓形裁切 (circlecrop)" },
];

const RESOLUTIONS = [
  { value: "1920x1080", label: "1920 x 1080 (FHD)" },
  { value: "1280x720", label: "1280 x 720 (HD)" },
  { value: "3840x2160", label: "3840 x 2160 (4K)" },
];

// Values are reference pixel sizes calibrated against a 1080p frame.
// ffmpeg/libass burns FontSize as a literal pixel count against the
// video's actual PlayResY (its real output height) — so a fixed literal
// value would look proportionally tiny at 4K and oversized at 720p.
// scaledFontSize() rescales each reference value by the selected output
// resolution's height so the caption occupies the same fraction of the
// frame no matter which resolution is chosen. The reference values
// themselves were picked from a real ffmpeg render: 22px (the old
// default) only produced ~1.5%-of-frame-height ink — legible in a
// screenshot crop, but too small once actually played back — so these
// are calibrated to real, comfortably-readable subtitle proportions
// (roughly 3%-7% of frame height) instead.
const FONT_SIZES = [
  { value: 46, label: "小" },
  { value: 64, label: "中" },
  { value: 84, label: "大" },
  { value: 106, label: "特大" },
];

function scaledFontSize(referencePx, resolutionValue) {
  const height = Number(resolutionValue.split("x")[1]);
  return Math.max(1, Math.round((referencePx * height) / 1080));
}

const TRANSITION_DURATIONS = [0, 300, 500, 800, 1000, 1500, 2000];

const RATE_MIN = -50;
const RATE_MAX = 100;
const VOLUME_MIN = -50;
const VOLUME_MAX = 50;

// Floors above 0 so a logo can never be dialed all the way down to invisible
// without the user realizing it's still "on" — a barely-there watermark
// beats a silently-vanished one.
const LOGO_OPACITY_MIN = 10;
const LOGO_OPACITY_MAX = 100;
const LOGO_OPACITY_DEFAULT = 100;

const LOGO_POSITIONS = [
  { value: "top-left", label: "左上" },
  { value: "top-right", label: "右上" },
  { value: "bottom-left", label: "左下" },
  { value: "bottom-right", label: "右下" },
];
const LOGO_POSITION_DEFAULT = "top-right";

// Reference pixel width/margin calibrated against a 1920px-wide baseline
// frame. ffmpeg's overlay filter (video.py's _add_logo_overlay) takes both
// as literal pixel counts against the real output resolution, so without
// scaling, the same literal values would look proportionally tiny at 4K
// and comparatively huge at 720p — the same class of bug fixed for
// subtitle font size earlier. scaledLogoWidth/scaledLogoMargin rescale
// both by the selected output resolution's width so the watermark keeps
// the same relative size and the same relative distance from the corner
// no matter which resolution is chosen. 160 matches the old hardcoded
// logo_width default exactly.
const LOGO_SIZES = [
  { value: 100, label: "小" },
  { value: 160, label: "中" },
  { value: 240, label: "大" },
  { value: 340, label: "特大" },
];
const LOGO_SIZE_DEFAULT = LOGO_SIZES[1].value;
const LOGO_MARGIN_REFERENCE = 12; // hugs the corner; see DEFAULT_LOGO_MARGIN in video.py

function scaledLogoWidth(referencePx, resolutionValue) {
  const width = Number(resolutionValue.split("x")[0]);
  return Math.max(1, Math.round((referencePx * width) / 1920));
}

function scaledLogoMargin(resolutionValue) {
  const width = Number(resolutionValue.split("x")[0]);
  return Math.max(1, Math.round((LOGO_MARGIN_REFERENCE * width) / 1920));
}

// Mirrors the backend's ffmpeg overlay=x:y corner placement (video.py's
// _LOGO_POSITION_OVERLAYS) so the little preview box actually matches
// where the logo lands in the real video.
function logoPositionStyle(position, edge) {
  switch (position) {
    case "top-left":
      return { top: edge, left: edge };
    case "bottom-left":
      return { bottom: edge, left: edge };
    case "bottom-right":
      return { bottom: edge, right: edge };
    case "top-right":
    default:
      return { top: edge, right: edge };
  }
}

// Expressed as % of frame height (distance from the bottom edge) rather
// than a raw pixel count, so the same slider position looks the same
// whether the job renders at 720p or 4K — the backend still ultimately
// takes a pixel MarginV, computed from this percent against whatever
// resolution is selected at submit time. 3% reproduces the previous
// hardcoded 30px-at-1080p look for anyone who never touches the slider.
const SUBTITLE_POSITION_MIN = 2;
const SUBTITLE_POSITION_MAX = 30;
const SUBTITLE_POSITION_DEFAULT = 3;

// Mirrors video.py's DEFAULT_SUBTITLE_FONT_COLOR/DEFAULT_SUBTITLE_OUTLINE_COLOR
// — white text with a black outline, the look every existing video already
// has, so leaving these untouched must render byte-for-byte the same.
const SUBTITLE_FONT_COLOR_DEFAULT = "#FFFFFF";
const SUBTITLE_OUTLINE_COLOR_DEFAULT = "#000000";
const SUBTITLE_BOLD_DEFAULT = false;

// Mirrors avatar.py's AvatarMode/AVATAR_MODES.
const AVATAR_MODES = [
  { value: "none", label: "不使用" },
  { value: "keyframe", label: "關鍵頁面（有講稿內容的頁面才顯示）" },
  { value: "always", label: "全程顯示" },
  { value: "custom", label: "自訂頁面" },
];
// Mirrors video.py's AVATAR_POSITIONS.
const AVATAR_POSITIONS = [
  { value: "bottom_right", label: "右下" },
  { value: "bottom_left", label: "左下" },
  { value: "right", label: "右側" },
  { value: "left", label: "左側" },
];
// Mirrors video.py's AVATAR_SIZES.
const AVATAR_SIZES = [
  { value: "small", label: "小" },
  { value: "medium", label: "中" },
  { value: "large", label: "大" },
];

const GEMINI_API_KEY_URL = "https://aistudio.google.com/app/apikey";
const PEXELS_API_KEY_URL = "https://www.pexels.com/api/new/";
const POLL_INTERVAL_MS = 2000;

const DOWNLOAD_LABELS = { mp4: "課程影片", srt: "字幕檔", docx: "講稿" };

function formatPercent(n) {
  return `${n >= 0 ? "+" : ""}${n}%`;
}

// Results-screen "產出細節" formatting — all three take null gracefully
// (an older/mocked job result may not carry these fields) and show an
// em-dash rather than "undefined" or "NaN".
function formatFileSizeMb(bytes) {
  if (bytes == null) return "—";
  const mb = bytes / (1024 * 1024);
  return mb < 1 ? `${Math.round(bytes / 1024)} KB` : `${mb.toFixed(1)} MB`;
}

function formatDurationMs(ms) {
  if (ms == null) return "—";
  const totalSeconds = Math.round(ms / 1000);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatGenerationSeconds(seconds) {
  if (seconds == null) return "—";
  const total = Math.round(seconds);
  if (total < 60) return `${total} 秒`;
  return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
}

function toAbsoluteUrl(path) {
  return resolveDownloadUrl(path, window.location.hostname, window.location.origin);
}

const STEPS = [
  { n: 1, key: "upload", label: "上傳投影片" },
  { n: 2, key: "script", label: "講稿來源" },
  { n: 3, key: "voice", label: "語音與轉場" },
  { n: 4, key: "extras", label: "進階選項" },
  { n: 5, key: "visuals", label: "AI 視覺素材" },
  { n: 6, key: "review", label: "開始製作" },
];

// Note: no client-side default start/duration constant here anymore — each
// recommendation's suggested_start_ms/suggested_end_ms (from
// /api/analyze-visuals, computed against real per-slide TTS timing) is the
// starting point the user sees, still fully overridable, and still clamped
// server-side against the slide's real audio duration regardless.

function useObjectUrls(files) {
  const [urls, setUrls] = useState([]);
  useEffect(() => {
    const next = files.map((f) => URL.createObjectURL(f));
    setUrls(next);
    return () => next.forEach((u) => URL.revokeObjectURL(u));
  }, [files]);
  return urls;
}

function App() {
  const [currentStep, setCurrentStep] = useState(1);

  const [pptxFile, setPptxFile] = useState(null);
  const [pptxPreviewStatus, setPptxPreviewStatus] = useState("idle"); // idle | loading | done | error
  const [pptxPreviewThumbnails, setPptxPreviewThumbnails] = useState([]);
  const [pptxPreviewError, setPptxPreviewError] = useState(null);
  const [pptxNotesStatus, setPptxNotesStatus] = useState("idle"); // idle | loading | done | error
  const [pptxNotes, setPptxNotes] = useState([]);
  const [pptxNotesError, setPptxNotesError] = useState(null);
  const [imageFiles, setImageFiles] = useState([]);
  const [baseName, setBaseName] = useState("課程");
  const [scriptMode, setScriptMode] = useState("NOTES");
  const [textInputMode, setTextInputMode] = useState("perSlide");
  const [perSlideTexts, setPerSlideTexts] = useState([]);
  const [pasteText, setPasteText] = useState("");
  const [pasteError, setPasteError] = useState(null);
  const [scriptPreviewStatus, setScriptPreviewStatus] = useState("idle"); // idle | loading | done | error
  const [scriptPreviewError, setScriptPreviewError] = useState(null);
  const [geminiApiKey, setGeminiApiKey] = useState("");
  const [voice, setVoice] = useState(VOICES[0].value);
  const [voiceRate, setVoiceRate] = useState(0);
  const [voiceVolume, setVoiceVolume] = useState(0);
  const [transition, setTransition] = useState("fade");
  const [transitionDurationMs, setTransitionDurationMs] = useState(500);
  const [resolution, setResolution] = useState(RESOLUTIONS[0].value);
  const [fontSize, setFontSize] = useState(FONT_SIZES[1].value);
  const [subtitlePositionPercent, setSubtitlePositionPercent] = useState(SUBTITLE_POSITION_DEFAULT);
  const [subtitleFontColor, setSubtitleFontColor] = useState(SUBTITLE_FONT_COLOR_DEFAULT);
  const [subtitleOutlineColor, setSubtitleOutlineColor] = useState(SUBTITLE_OUTLINE_COLOR_DEFAULT);
  const [subtitleBold, setSubtitleBold] = useState(SUBTITLE_BOLD_DEFAULT);
  const [logoFile, setLogoFile] = useState(null);
  const [logoOpacity, setLogoOpacity] = useState(LOGO_OPACITY_DEFAULT);
  const [logoPosition, setLogoPosition] = useState(LOGO_POSITION_DEFAULT);
  const [logoSize, setLogoSize] = useState(LOGO_SIZE_DEFAULT);
  const [bgmFile, setBgmFile] = useState(null);
  const [introFile, setIntroFile] = useState(null);
  const [outroFile, setOutroFile] = useState(null);
  const [customDict, setCustomDict] = useState("");

  const [avatarMode, setAvatarMode] = useState("none");
  const [avatarPosition, setAvatarPosition] = useState("bottom_right");
  const [avatarSize, setAvatarSize] = useState("small");
  // Set of 1-based slide numbers, only used when avatarMode === "custom".
  const [avatarCustomSlides, setAvatarCustomSlides] = useState([]);

  const [readingPauseSec, setReadingPauseSec] = useState(0);
  const [closingPauseSec, setClosingPauseSec] = useState(0);
  const [targetDurationEnabled, setTargetDurationEnabled] = useState(false);
  const [targetDurationSec, setTargetDurationSec] = useState(120);
  const [avoidVoiceOverlap, setAvoidVoiceOverlap] = useState(false);

  const [visualAnalysisStatus, setVisualAnalysisStatus] = useState("idle"); // idle | loading | done | error
  const [visualAnalysisError, setVisualAnalysisError] = useState(null);
  const [visualRecommendations, setVisualRecommendations] = useState([]);
  const [visualAnalysisWarnings, setVisualAnalysisWarnings] = useState([]);
  const [pexelsApiKey, setPexelsApiKey] = useState("");
  // slide_number -> { asset, startSec, durationSec } | undefined ("不使用" or not yet chosen)
  const [brollChoices, setBrollChoices] = useState({});
  // slide_number -> { status: idle|loading|done|error, assets, warning }
  const [mediaSearchBySlide, setMediaSearchBySlide] = useState({});

  const [submitting, setSubmitting] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [formError, setFormError] = useState(null);

  const pollTimerRef = useRef(null);
  const pptxPreviewRequestRef = useRef(0);
  const pptxNotesRequestRef = useRef(0);
  const thumbUrls = useObjectUrls(imageFiles);

  const modeConfig = SCRIPT_MODES.find((m) => m.value === scriptMode);
  const voiceLabel = VOICES.find((v) => v.value === voice)?.label;
  const transitionLabel = TRANSITIONS.find((t) => t.value === transition)?.label;
  const resolutionLabel = RESOLUTIONS.find((r) => r.value === resolution)?.label;
  const fontSizeOption = FONT_SIZES.find((f) => f.value === fontSize);
  const fontSizeLabel = fontSizeOption
    ? `${fontSizeOption.label}（約 ${scaledFontSize(fontSizeOption.value, resolution)}px）`
    : undefined;
  const usesAiGeneration = scriptMode === "AUTO" || scriptMode === "POLISH";

  // A previously-generated preview only reflects the deck/slide-count it was
  // generated from — re-uploading the deck or switching modes invalidates
  // it. Editing the generated text itself must NOT reset this: that's the
  // "review and tweak before continuing" flow this whole feature is for.
  useEffect(() => {
    setScriptPreviewStatus("idle");
    setScriptPreviewError(null);
  }, [scriptMode, pptxFile, imageFiles.length]);

  // A visual-need analysis (and any B-roll already picked from it) only
  // makes sense for the exact deck/script it was run against — swapping the
  // deck or re-doing the script invalidates it the same way the script
  // preview above gets invalidated.
  useEffect(() => {
    setVisualAnalysisStatus("idle");
    setVisualAnalysisError(null);
    setVisualRecommendations([]);
    setVisualAnalysisWarnings([]);
    setBrollChoices({});
    setMediaSearchBySlide({});
  }, [scriptMode, pptxFile, imageFiles.length]);

  async function handleGenerateScriptPreview() {
    setScriptPreviewStatus("loading");
    setScriptPreviewError(null);
    try {
      const { texts } = await generateScriptPreview({
        pptxFile,
        scriptMode,
        geminiApiKey,
        geminiModel: undefined,
        texts: scriptMode === "POLISH" ? perSlideTexts : undefined,
      });
      setPerSlideTexts(texts);
      setTextInputMode("perSlide");
      setScriptPreviewStatus("done");
      setFormError(null); // clears the stale "請先產生預覽" banner from an earlier blocked "下一步" click
    } catch (err) {
      setScriptPreviewError(err.message);
      setScriptPreviewStatus("error");
    }
  }

  // The one script text per slide currently "in effect" for whichever
  // script_mode/input path is active — the same text handleSubmit would
  // send as the job's narration. Returns null while that text isn't fully
  // resolved yet (e.g. paste-mode markers don't parse, or an AI preview
  // hasn't been generated), which the visuals step uses to gate analysis
  // on a script that actually matches what will be narrated.
  function resolveCurrentTexts() {
    if (scriptMode === "NOTES") {
      return pptxNotes.length === imageFiles.length ? pptxNotes : null;
    }
    if (textInputMode === "paste") {
      const result = parseNumberedScript(pasteText, imageFiles.length);
      return result.texts || null;
    }
    if (perSlideTexts.length !== imageFiles.length || perSlideTexts.some((t) => !t.trim())) {
      return null;
    }
    return perSlideTexts;
  }

  async function handleAnalyzeVisuals() {
    const texts = resolveCurrentTexts();
    if (!pptxFile || !texts) return;
    setVisualAnalysisStatus("loading");
    setVisualAnalysisError(null);
    try {
      const { recommendations, warnings } = await analyzeVisuals({
        pptxFile,
        texts,
        voice,
        voiceRate: formatPercent(voiceRate),
        voiceVolume: formatPercent(voiceVolume),
        geminiApiKey,
      });
      setVisualRecommendations(recommendations);
      setVisualAnalysisWarnings(warnings || []);
      setVisualAnalysisStatus("done");
    } catch (err) {
      setVisualAnalysisError(err.message);
      setVisualAnalysisStatus("error");
    }
  }

  async function handleSearchMedia(slideNumber, keyword) {
    setMediaSearchBySlide((prev) => ({
      ...prev,
      [slideNumber]: { status: "loading", assets: [], warning: null },
    }));
    try {
      const { assets, warning } = await searchMedia({ keyword, slideNumber, pexelsApiKey });
      setMediaSearchBySlide((prev) => ({ ...prev, [slideNumber]: { status: "done", assets, warning } }));
    } catch (err) {
      setMediaSearchBySlide((prev) => ({
        ...prev,
        [slideNumber]: { status: "error", assets: [], warning: err.message },
      }));
    }
  }

  // Real slide thumbnails (rendered server-side via LibreOffice), not just a
  // filename — mirrors the instant visual confirmation the per-slide image
  // upload field already gives, so the user can tell at a glance they picked
  // the right deck. Runs automatically on file selection; the request id
  // guards against an in-flight request from a since-replaced file
  // overwriting the current one's result.
  async function loadPptxPreview(file) {
    const requestId = ++pptxPreviewRequestRef.current;
    setPptxPreviewStatus("loading");
    setPptxPreviewError(null);
    try {
      const { thumbnails } = await fetchPptxPreview(file);
      if (pptxPreviewRequestRef.current !== requestId) return;
      setPptxPreviewThumbnails(thumbnails);
      setPptxPreviewStatus("done");
    } catch (err) {
      if (pptxPreviewRequestRef.current !== requestId) return;
      setPptxPreviewError(err.message);
      setPptxPreviewStatus("error");
    }
  }

  useEffect(() => {
    if (!pptxFile) {
      pptxPreviewRequestRef.current += 1; // invalidate any in-flight request
      setPptxPreviewStatus("idle");
      setPptxPreviewThumbnails([]);
      setPptxPreviewError(null);
      return;
    }
    loadPptxPreview(pptxFile);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pptxFile]);

  // Speaker notes are a property of the uploaded deck itself, not of the
  // chosen script mode — fetched right alongside the thumbnails so the
  // preview is already sitting there the moment the user switches to
  // "使用投影片備忘稿". Same in-flight-request-guard pattern as above.
  async function loadPptxNotes(file) {
    const requestId = ++pptxNotesRequestRef.current;
    setPptxNotesStatus("loading");
    setPptxNotesError(null);
    try {
      const { notes } = await fetchPptxNotes(file);
      if (pptxNotesRequestRef.current !== requestId) return;
      setPptxNotes(notes);
      setPptxNotesStatus("done");
    } catch (err) {
      if (pptxNotesRequestRef.current !== requestId) return;
      setPptxNotesError(err.message);
      setPptxNotesStatus("error");
    }
  }

  useEffect(() => {
    if (!pptxFile) {
      pptxNotesRequestRef.current += 1; // invalidate any in-flight request
      setPptxNotesStatus("idle");
      setPptxNotes([]);
      setPptxNotesError(null);
      return;
    }
    loadPptxNotes(pptxFile);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pptxFile]);

  useEffect(() => {
    if (!modeConfig.needsTexts || textInputMode !== "perSlide") return;
    setPerSlideTexts((prev) => imageFiles.map((_, i) => prev[i] || ""));
  }, [imageFiles, modeConfig.needsTexts, textInputMode]);

  useEffect(() => {
    if (!modeConfig.needsTexts || textInputMode !== "paste") return;
    if (!pasteText.trim()) {
      setPasteError(null);
      return;
    }
    const result = parseNumberedScript(pasteText, imageFiles.length);
    if (result.error) {
      setPasteError(result.error);
    } else {
      setPasteError(null);
      setPerSlideTexts(result.texts);
    }
  }, [pasteText, textInputMode, imageFiles.length, modeConfig.needsTexts]);

  useEffect(() => {
    if (!jobId) return undefined;

    const poll = async () => {
      try {
        const status = await getJobStatus(jobId);
        setJobStatus(status);
        if (status.status === "done" || status.status === "error") {
          clearInterval(pollTimerRef.current);
        }
      } catch (err) {
        setJobStatus({ status: "error", error: err.message });
        clearInterval(pollTimerRef.current);
      }
    };

    poll();
    pollTimerRef.current = setInterval(poll, POLL_INTERVAL_MS);
    return () => clearInterval(pollTimerRef.current);
  }, [jobId]);

  function resetJob() {
    // Full reset, not just clearing the finished job — "建立新任務" should
    // mean an actually blank form, not the previous task's files/settings
    // still sitting there waiting to be manually swapped out.
    setJobId(null);
    setJobStatus(null);
    setFormError(null);
    setCurrentStep(1);

    setPptxFile(null);
    pptxPreviewRequestRef.current += 1; // invalidate any in-flight preview request
    setPptxPreviewStatus("idle");
    setPptxPreviewThumbnails([]);
    setPptxPreviewError(null);
    pptxNotesRequestRef.current += 1; // invalidate any in-flight notes request
    setPptxNotesStatus("idle");
    setPptxNotes([]);
    setPptxNotesError(null);
    setImageFiles([]);
    setBaseName("課程");
    setScriptMode("NOTES");
    setTextInputMode("perSlide");
    setPerSlideTexts([]);
    setPasteText("");
    setPasteError(null);
    setGeminiApiKey("");
    setVoice(VOICES[0].value);
    setVoiceRate(0);
    setVoiceVolume(0);
    setTransition("fade");
    setTransitionDurationMs(500);
    setResolution(RESOLUTIONS[0].value);
    setFontSize(FONT_SIZES[1].value);
    setSubtitlePositionPercent(SUBTITLE_POSITION_DEFAULT);
    setSubtitleFontColor(SUBTITLE_FONT_COLOR_DEFAULT);
    setSubtitleOutlineColor(SUBTITLE_OUTLINE_COLOR_DEFAULT);
    setSubtitleBold(SUBTITLE_BOLD_DEFAULT);
    setLogoFile(null);
    setLogoOpacity(LOGO_OPACITY_DEFAULT);
    setLogoPosition(LOGO_POSITION_DEFAULT);
    setLogoSize(LOGO_SIZE_DEFAULT);
    setBgmFile(null);
    setIntroFile(null);
    setOutroFile(null);
    setCustomDict("");
    setVisualAnalysisStatus("idle");
    setVisualAnalysisError(null);
    setVisualRecommendations([]);
    setVisualAnalysisWarnings([]);
    setPexelsApiKey("");
    setBrollChoices({});
    setMediaSearchBySlide({});
    setSubmitting(false);
    setScriptPreviewStatus("idle");
    setScriptPreviewError(null);
  }

  function validateStep(step) {
    if (step === 1) {
      if (!pptxFile) return "請上傳 .pptx 檔案";
      if (imageFiles.length === 0) return "請上傳每一頁投影片的圖片";
      return null;
    }
    if (step === 2) {
      if (modeConfig.needsApiKey && !geminiApiKey.trim()) return "此模式需要 Gemini API Key";

      if (scriptMode === "POLISH") {
        if (textInputMode === "paste") {
          if (!pasteText.trim()) return "請貼上包含頁碼標記的講稿草稿";
          if (pasteError) return pasteError;
        } else if (perSlideTexts.some((t) => !t.trim())) {
          return "請為每一頁投影片輸入草稿";
        }
      }

      if (usesAiGeneration && scriptPreviewStatus !== "done") {
        return "請先產生 AI 講稿預覽，確認內容後再繼續下一步";
      }

      if (scriptMode === "OWN") {
        if (textInputMode === "paste") {
          if (!pasteText.trim()) return "請貼上包含頁碼標記的講稿";
          if (pasteError) return pasteError;
        } else if (perSlideTexts.some((t) => !t.trim())) {
          return "請為每一頁投影片輸入講稿";
        }
      }
      return null;
    }
    return null;
  }

  function goNext() {
    const err = validateStep(currentStep);
    if (err) {
      setFormError(err);
      return;
    }
    setFormError(null);
    setCurrentStep((s) => Math.min(s + 1, STEPS.length));
  }

  function goBack() {
    setFormError(null);
    setCurrentStep((s) => Math.max(s - 1, 1));
  }

  async function handleSubmit() {
    const stepOneError = validateStep(1);
    const stepTwoError = validateStep(2);
    if (stepOneError || stepTwoError) {
      setFormError(stepOneError || stepTwoError);
      setCurrentStep(stepOneError ? 1 : 2);
      return;
    }

    const [width, height] = resolution.split("x");

    // Once an AI-generated script has been previewed (and possibly edited),
    // submit exactly what was reviewed — as OWN — instead of asking the
    // backend to call Gemini a second time during the pipeline run. That
    // would be slower, cost another API call, and since generation isn't
    // deterministic, could silently produce different text than what the
    // user actually approved on screen.
    const usedAiPreview = usesAiGeneration && scriptPreviewStatus === "done";
    const effectiveScriptMode = usedAiPreview ? "OWN" : scriptMode;

    const form = new FormData();
    form.append("pptx", pptxFile);
    imageFiles.forEach((file) => form.append("images", file));
    form.append("script_mode", effectiveScriptMode);
    form.append("voice", voice);
    form.append("rate", formatPercent(voiceRate));
    form.append("volume", formatPercent(voiceVolume));
    form.append("base_name", baseName || "課程");
    if (usedAiPreview || modeConfig.needsTexts) {
      form.append("texts", JSON.stringify(perSlideTexts));
    }
    if (!usedAiPreview && modeConfig.needsApiKey) {
      form.append("gemini_api_key", geminiApiKey);
    }
    form.append("transition", transition);
    form.append("transition_duration_ms", String(transitionDurationMs));
    form.append("resolution_width", width);
    form.append("resolution_height", height);
    form.append("font_size", String(scaledFontSize(fontSize, resolution)));
    form.append(
      "subtitle_margin_v",
      String(Math.round((Number(height) * subtitlePositionPercent) / 100))
    );
    form.append("subtitle_font_color", subtitleFontColor);
    form.append("subtitle_outline_color", subtitleOutlineColor);
    if (subtitleBold) form.append("subtitle_bold", "true");
    if (logoFile) {
      form.append("logo", logoFile);
      form.append("logo_opacity", String(logoOpacity / 100));
      form.append("logo_position", logoPosition);
      form.append("logo_width", String(scaledLogoWidth(logoSize, resolution)));
      form.append("logo_margin", String(scaledLogoMargin(resolution)));
    }
    if (bgmFile) form.append("bgm", bgmFile);
    if (introFile) form.append("intro", introFile);
    if (outroFile) form.append("outro", outroFile);
    if (customDict.trim()) form.append("custom_dict", customDict);

    if (avatarMode !== "none") {
      form.append("avatar_mode", avatarMode);
      form.append("avatar_position", avatarPosition);
      form.append("avatar_size", avatarSize);
      if (avatarMode === "custom") {
        form.append("avatar_custom_slides", JSON.stringify(avatarCustomSlides));
      }
    }

    if (targetDurationEnabled) {
      // Target mode auto-computes reading pauses server-side and ignores
      // any flat reading_pause_ms/closing_pause_ms, so those are simply
      // not sent here.
      form.append("target_duration_ms", String(Math.round(targetDurationSec * 1000)));
    } else {
      if (readingPauseSec > 0) form.append("reading_pause_ms", String(Math.round(readingPauseSec * 1000)));
      if (closingPauseSec > 0) form.append("closing_pause_ms", String(Math.round(closingPauseSec * 1000)));
    }
    if (avoidVoiceOverlap) form.append("avoid_voice_overlap", "true");

    const confirmedBrollSelections = Object.entries(brollChoices)
      .filter(([, choice]) => choice && choice.asset)
      .map(([slideNumber, choice]) => ({
        slide_number: Number(slideNumber),
        download_url: choice.asset.download_url,
        start_ms: Math.round(choice.startSec * 1000),
        end_ms: Math.round((choice.startSec + choice.durationSec) * 1000),
      }));
    if (confirmedBrollSelections.length > 0) {
      form.append("broll_selections", JSON.stringify(confirmedBrollSelections));
    }

    setSubmitting(true);
    setFormError(null);
    try {
      const { job_id: newJobId } = await createJob(form);
      setJobId(newJobId);
      setJobStatus({ status: "queued" });
    } catch (err) {
      setFormError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  const isRunning = jobStatus && (jobStatus.status === "queued" || jobStatus.status === "running");
  const isDone = jobStatus && jobStatus.status === "done";
  const isError = jobStatus && jobStatus.status === "error";

  return (
    <div className="shell">
      <header className="masthead">
        <p className="wordmark">
          <span className="sprocket" aria-hidden="true" />
          PPT2Course
        </p>
        <p className="tagline">把PPT轉成有旁白、有字幕的課程影片</p>
      </header>

      {jobId ? (
        <StatusView
          jobId={jobId}
          jobStatus={jobStatus}
          isRunning={isRunning}
          isDone={isDone}
          isError={isError}
          onReset={resetJob}
        />
      ) : (
        <div className="studio">
          <nav className="rail" aria-label="製作流程">
            {STEPS.map((step) => (
              <button
                key={step.key}
                type="button"
                className={
                  "step" +
                  (step.n === currentStep ? " active" : "") +
                  (step.n < currentStep ? " done clickable" : "")
                }
                onClick={() => step.n < currentStep && setCurrentStep(step.n)}
                disabled={step.n >= currentStep}
              >
                <span className="num">{step.n < currentStep ? "✓" : String(step.n).padStart(2, "0")}</span>
                <span className="label">{step.label}</span>
              </button>
            ))}
          </nav>

          <div className="card">
            {formError && <p className="form-error">{formError}</p>}

            {currentStep === 1 && (
              <UploadStep
                pptxFile={pptxFile}
                setPptxFile={setPptxFile}
                pptxPreviewStatus={pptxPreviewStatus}
                pptxPreviewThumbnails={pptxPreviewThumbnails}
                pptxPreviewError={pptxPreviewError}
                onRetryPptxPreview={() => loadPptxPreview(pptxFile)}
                imageFiles={imageFiles}
                setImageFiles={setImageFiles}
                thumbUrls={thumbUrls}
                baseName={baseName}
                setBaseName={setBaseName}
              />
            )}

            {currentStep === 2 && (
              <ScriptStep
                slideCount={imageFiles.length}
                scriptMode={scriptMode}
                setScriptMode={setScriptMode}
                modeConfig={modeConfig}
                geminiApiKey={geminiApiKey}
                setGeminiApiKey={setGeminiApiKey}
                textInputMode={textInputMode}
                setTextInputMode={setTextInputMode}
                perSlideTexts={perSlideTexts}
                setPerSlideTexts={setPerSlideTexts}
                pasteText={pasteText}
                setPasteText={setPasteText}
                pasteError={pasteError}
                usesAiGeneration={usesAiGeneration}
                onGeneratePreview={handleGenerateScriptPreview}
                scriptPreviewStatus={scriptPreviewStatus}
                scriptPreviewError={scriptPreviewError}
                pptxNotesStatus={pptxNotesStatus}
                pptxNotes={pptxNotes}
                pptxNotesError={pptxNotesError}
              />
            )}

            {currentStep === 3 && (
              <VoiceStep
                voice={voice}
                setVoice={setVoice}
                voiceRate={voiceRate}
                setVoiceRate={setVoiceRate}
                voiceVolume={voiceVolume}
                setVoiceVolume={setVoiceVolume}
                transition={transition}
                setTransition={setTransition}
                transitionDurationMs={transitionDurationMs}
                setTransitionDurationMs={setTransitionDurationMs}
              />
            )}

            {currentStep === 4 && (
              <ExtrasStep
                resolution={resolution}
                setResolution={setResolution}
                fontSize={fontSize}
                setFontSize={setFontSize}
                subtitlePositionPercent={subtitlePositionPercent}
                setSubtitlePositionPercent={setSubtitlePositionPercent}
                subtitleFontColor={subtitleFontColor}
                setSubtitleFontColor={setSubtitleFontColor}
                subtitleOutlineColor={subtitleOutlineColor}
                setSubtitleOutlineColor={setSubtitleOutlineColor}
                subtitleBold={subtitleBold}
                setSubtitleBold={setSubtitleBold}
                previewImageUrl={thumbUrls[0]}
                logoFile={logoFile}
                setLogoFile={setLogoFile}
                logoOpacity={logoOpacity}
                setLogoOpacity={setLogoOpacity}
                logoPosition={logoPosition}
                setLogoPosition={setLogoPosition}
                logoSize={logoSize}
                setLogoSize={setLogoSize}
                bgmFile={bgmFile}
                setBgmFile={setBgmFile}
                introFile={introFile}
                setIntroFile={setIntroFile}
                outroFile={outroFile}
                setOutroFile={setOutroFile}
                customDict={customDict}
                setCustomDict={setCustomDict}
                avatarMode={avatarMode}
                setAvatarMode={setAvatarMode}
                avatarPosition={avatarPosition}
                setAvatarPosition={setAvatarPosition}
                avatarSize={avatarSize}
                setAvatarSize={setAvatarSize}
                avatarCustomSlides={avatarCustomSlides}
                setAvatarCustomSlides={setAvatarCustomSlides}
                slideCount={imageFiles.length}
                readingPauseSec={readingPauseSec}
                setReadingPauseSec={setReadingPauseSec}
                closingPauseSec={closingPauseSec}
                setClosingPauseSec={setClosingPauseSec}
                targetDurationEnabled={targetDurationEnabled}
                setTargetDurationEnabled={setTargetDurationEnabled}
                targetDurationSec={targetDurationSec}
                setTargetDurationSec={setTargetDurationSec}
                avoidVoiceOverlap={avoidVoiceOverlap}
                setAvoidVoiceOverlap={setAvoidVoiceOverlap}
              />
            )}

            {currentStep === 5 && (
              <VisualsStep
                slideCount={imageFiles.length}
                thumbUrls={thumbUrls}
                onAnalyze={handleAnalyzeVisuals}
                canAnalyze={Boolean(pptxFile) && resolveCurrentTexts() !== null}
                analysisStatus={visualAnalysisStatus}
                analysisError={visualAnalysisError}
                recommendations={visualRecommendations}
                analysisWarnings={visualAnalysisWarnings}
                pexelsApiKey={pexelsApiKey}
                setPexelsApiKey={setPexelsApiKey}
                mediaSearchBySlide={mediaSearchBySlide}
                onSearchMedia={handleSearchMedia}
                brollChoices={brollChoices}
                setBrollChoices={setBrollChoices}
              />
            )}

            {currentStep === 6 && (
              <ReviewStep
                baseName={baseName}
                slideCount={imageFiles.length}
                modeLabel={modeConfig.label}
                voiceLabel={voiceLabel}
                voiceRate={voiceRate}
                voiceVolume={voiceVolume}
                transitionLabel={transitionLabel}
                transitionDurationMs={transitionDurationMs}
                resolutionLabel={resolutionLabel}
                fontSizeLabel={fontSizeLabel}
                subtitlePositionPercent={subtitlePositionPercent}
                extras={[
                  (subtitleFontColor !== SUBTITLE_FONT_COLOR_DEFAULT ||
                    subtitleOutlineColor !== SUBTITLE_OUTLINE_COLOR_DEFAULT) &&
                    `字幕顏色（文字 ${subtitleFontColor.toUpperCase()}・外框 ${subtitleOutlineColor.toUpperCase()}）`,
                  subtitleBold && "字幕粗體",
                  logoFile &&
                    `Logo（${LOGO_POSITIONS.find((p) => p.value === logoPosition)?.label}角・${
                      LOGO_SIZES.find((s) => s.value === logoSize)?.label
                    }・透明度 ${logoOpacity}%）`,
                  bgmFile && "背景音樂",
                  introFile && "片頭",
                  outroFile && "片尾",
                  customDict.trim() &&
                    `自訂詞庫（${customDict.split("\n").filter((l) => l.trim()).length} 個詞）`,
                  Object.values(brollChoices).filter((c) => c && c.asset).length > 0 &&
                    `AI 視覺素材（${Object.values(brollChoices).filter((c) => c && c.asset).length} 頁）`,
                  avatarMode !== "none" &&
                    `2D 講師 Avatar（${AVATAR_MODES.find((m) => m.value === avatarMode)?.label}・${
                      AVATAR_POSITIONS.find((p) => p.value === avatarPosition)?.label
                    }・${AVATAR_SIZES.find((s) => s.value === avatarSize)?.label}）`,
                  targetDurationEnabled
                    ? `目標長度（${formatSecondsAsMinSec(targetDurationSec)}，自動分配停頓）`
                    : (readingPauseSec > 0 || closingPauseSec > 0) &&
                      `節奏停頓（每頁 +${readingPauseSec.toFixed(1)}s${
                        closingPauseSec > 0 ? `・結尾 +${closingPauseSec.toFixed(1)}s` : ""
                      }）`,
                  avoidVoiceOverlap && "轉場後才開口（聲音不重疊）",
                ].filter(Boolean)}
              />
            )}

            <div className="actions">
              <span className="step-count">
                STEP {String(currentStep).padStart(2, "0")} / {String(STEPS.length).padStart(2, "0")}
              </span>
              <div style={{ display: "flex", gap: 10 }}>
                {currentStep > 1 && (
                  <button type="button" className="btn btn-ghost" onClick={goBack}>
                    上一步
                  </button>
                )}
                {currentStep < STEPS.length ? (
                  <button type="button" className="btn btn-primary" onClick={goNext}>
                    下一步 →
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={submitting}
                    onClick={handleSubmit}
                  >
                    {submitting ? "送出中..." : "開始製作課程影片"}
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      <HelpWidget />
    </div>
  );
}

function UploadStep({
  pptxFile,
  setPptxFile,
  pptxPreviewStatus,
  pptxPreviewThumbnails,
  pptxPreviewError,
  onRetryPptxPreview,
  imageFiles,
  setImageFiles,
  thumbUrls,
  baseName,
  setBaseName,
}) {
  const [lightboxIndex, setLightboxIndex] = useState(null);
  const slideCountTrailing = imageFiles.length
    ? `${imageFiles.length} 頁投影片`
    : pptxPreviewThumbnails.length
      ? `${pptxPreviewThumbnails.length} 頁投影片`
      : null;

  return (
    <>
      <CardHead eyebrow="Step 01" title="上傳投影片" trailing={slideCountTrailing} />
      <div className="field-grid">
        <div className="field full">
          <label htmlFor="pptx-input">
            PPTX 檔案 <FieldTag required />
          </label>
          <div className="uploader">
            <input
              id="pptx-input"
              type="file"
              accept=".pptx"
              onChange={(e) => setPptxFile(e.target.files[0] || null)}
            />
            <span className="filename">{pptxFile ? pptxFile.name : "尚未選擇檔案"}</span>
            <span className="btn-mini">選擇</span>
          </div>

          {pptxPreviewStatus === "loading" && (
            <p className="pptx-preview-status">
              <span className="pptx-preview-spinner" aria-hidden="true" />
              正在產生投影片預覽縮圖…
            </p>
          )}

          {pptxPreviewStatus === "error" && (
            <p className="pptx-preview-status pptx-preview-status-error">
              預覽產生失敗：{pptxPreviewError}
              <button type="button" className="btn-mini" onClick={onRetryPptxPreview}>
                重試
              </button>
            </p>
          )}

          {pptxPreviewStatus === "done" && pptxPreviewThumbnails.length > 0 && (
            <div className="pptx-preview-strip">
              {pptxPreviewThumbnails.map((url, i) => (
                <button
                  key={i}
                  type="button"
                  className="pptx-preview-thumb"
                  onClick={() => setLightboxIndex(i)}
                  aria-label={`放大檢視第 ${i + 1} 頁投影片`}
                >
                  <img src={url} alt={`第 ${i + 1} 頁投影片預覽`} />
                  <span className="pptx-preview-thumb-badge">{i + 1}</span>
                </button>
              ))}
            </div>
          )}

          {lightboxIndex !== null && (
            <PptxSlideLightbox
              thumbnails={pptxPreviewThumbnails}
              index={lightboxIndex}
              onClose={() => setLightboxIndex(null)}
              onNavigate={setLightboxIndex}
            />
          )}
        </div>

        <div className="field full">
          <label htmlFor="images-input">
            每頁投影片的圖片 <FieldTag required />{" "}
            <span className="hint">— 依順序全選，張數需與 PPTX 頁數相同</span>
          </label>
          <div className="uploader">
            <input
              id="images-input"
              type="file"
              accept="image/*"
              multiple
              onChange={(e) => setImageFiles(Array.from(e.target.files))}
            />
            <span className="filename">
              {imageFiles.length ? `已選 ${imageFiles.length} 張圖片` : "尚未選擇檔案"}
            </span>
            <span className="btn-mini">選擇</span>
          </div>
          {thumbUrls.length > 0 && (
            <div className="thumbstrip">
              {thumbUrls.map((url, i) => (
                <div key={i} className="thumb" style={{ backgroundImage: `url(${url})` }} />
              ))}
            </div>
          )}
        </div>

        <div className="field full">
          <label htmlFor="base-name-input">
            課程名稱（輸出檔名） <FieldTag required={false} />{" "}
            <span className="hint">— 留空預設為「課程」</span>
          </label>
          <input
            id="base-name-input"
            type="text"
            value={baseName}
            onChange={(e) => setBaseName(e.target.value)}
          />
        </div>
      </div>
    </>
  );
}

function PptxSlideLightbox({ thumbnails, index, onClose, onNavigate }) {
  useEffect(() => {
    function handleKeyDown(e) {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft") onNavigate((i) => Math.max(i - 1, 0));
      else if (e.key === "ArrowRight") onNavigate((i) => Math.min(i + 1, thumbnails.length - 1));
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose, onNavigate, thumbnails.length]);

  return (
    <div className="pptx-lightbox-backdrop" onClick={onClose}>
      <div
        className="pptx-lightbox"
        role="dialog"
        aria-modal="true"
        aria-label={`第 ${index + 1} 頁投影片放大檢視`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="pptx-lightbox-stage">
          <button type="button" className="pptx-lightbox-close" onClick={onClose} aria-label="關閉">
            ✕
          </button>
          <button
            type="button"
            className="pptx-lightbox-nav pptx-lightbox-nav-prev"
            onClick={() => onNavigate((i) => Math.max(i - 1, 0))}
            disabled={index === 0}
            aria-label="上一頁"
          >
            ‹
          </button>
          <img src={thumbnails[index]} alt={`第 ${index + 1} 頁投影片`} />
          <button
            type="button"
            className="pptx-lightbox-nav pptx-lightbox-nav-next"
            onClick={() => onNavigate((i) => Math.min(i + 1, thumbnails.length - 1))}
            disabled={index === thumbnails.length - 1}
            aria-label="下一頁"
          >
            ›
          </button>
        </div>
        <span className="pptx-lightbox-caption">
          第 {index + 1} / {thumbnails.length} 頁
        </span>
      </div>
    </div>
  );
}

function PerSlideTextEditor({ slideCount, perSlideTexts, setPerSlideTexts }) {
  return (
    <div className="per-slide-texts">
      {Array.from({ length: slideCount }).map((_, i) => (
        <div key={i} className="field">
          <label>第 {i + 1} 頁</label>
          <textarea
            rows={2}
            value={perSlideTexts[i] || ""}
            onChange={(e) => {
              const next = [...perSlideTexts];
              next[i] = e.target.value;
              setPerSlideTexts(next);
            }}
          />
        </div>
      ))}
    </div>
  );
}

function GenerateScriptButton({ label, loadingLabel, disabled, status, error, onClick }) {
  return (
    <div className="field full">
      <button type="button" className="btn btn-ghost" disabled={disabled} onClick={onClick}>
        {status === "loading" ? loadingLabel : label}
      </button>
      {status === "error" && <p className="form-error" style={{ margin: "8px 0 0" }}>{error}</p>}
    </div>
  );
}

function ScriptStep({
  slideCount,
  scriptMode,
  setScriptMode,
  modeConfig,
  geminiApiKey,
  setGeminiApiKey,
  textInputMode,
  setTextInputMode,
  perSlideTexts,
  setPerSlideTexts,
  pasteText,
  setPasteText,
  pasteError,
  usesAiGeneration,
  onGeneratePreview,
  scriptPreviewStatus,
  scriptPreviewError,
  pptxNotesStatus,
  pptxNotes,
  pptxNotesError,
}) {
  const detectedCount = perSlideTexts.filter((t) => t.trim()).length;
  const emptyNotesCount = pptxNotes.filter((n) => !n.trim()).length;
  const hasDraftText =
    textInputMode === "paste" ? pasteText.trim().length > 0 : perSlideTexts.some((t) => t.trim());

  return (
    <>
      <CardHead eyebrow="Step 02" title="講稿來源" trailing={slideCount ? `${slideCount} 頁投影片` : null} />
      <div className="field-grid">
        <div className="field full">
          <label>
            模式 <FieldTag required />
          </label>
          <div className="mode-picker">
            {SCRIPT_MODES.map((m) => (
              <button
                key={m.value}
                type="button"
                className={"mode-option" + (scriptMode === m.value ? " selected" : "")}
                onClick={() => setScriptMode(m.value)}
              >
                <span className="dot" />
                <span>{m.label}</span>
                {m.needsApiKey && <span className="badge">Gemini</span>}
              </button>
            ))}
          </div>
        </div>

        {modeConfig.needsApiKey && (
          <div className="field full">
            <label htmlFor="gemini-key-input">
              Gemini API Key <FieldTag required />{" "}
              <span className="hint">— 只用於這次任務，不會被儲存</span>
            </label>
            <input
              id="gemini-key-input"
              type="password"
              value={geminiApiKey}
              onChange={(e) => setGeminiApiKey(e.target.value)}
              placeholder="貼上你的 Gemini API Key"
            />
            <a href={GEMINI_API_KEY_URL} target="_blank" rel="noreferrer" className="hint-link">
              還沒有 Gemini API Key？點此免費申請 →
            </a>
          </div>
        )}

        {scriptMode === "NOTES" && (
          <div className="field full">
            <label>
              備忘稿預覽 <span className="hint">— 這就是每一頁實際會唸出來的內容，唸稿前先確認有沒有寫錯</span>
            </label>

            {pptxNotesStatus === "loading" && (
              <p className="status-caption">讀取備忘稿中...</p>
            )}

            {pptxNotesStatus === "error" && (
              <p className="form-error" style={{ margin: "8px 0 0" }}>{pptxNotesError}</p>
            )}

            {pptxNotesStatus === "done" && pptxNotes.length > 0 && (
              <>
                <div className="per-slide-texts">
                  {pptxNotes.map((notes, i) => (
                    <div key={i} className="field">
                      <label>第 {i + 1} 頁{!notes.trim() && <span className="hint">（沒有備忘稿，這頁會是靜音）</span>}</label>
                      <textarea rows={2} value={notes} readOnly placeholder="（空白）" />
                    </div>
                  ))}
                </div>
                {emptyNotesCount > 0 && (
                  <p className="form-error" style={{ margin: "8px 0 0" }}>
                    有 {emptyNotesCount} 頁沒有寫備忘稿，這幾頁在影片裡會是靜音畫面。若不是故意的，請回去 PowerPoint 補上備忘稿後重新上傳。
                  </p>
                )}
              </>
            )}

            {pptxNotesStatus === "done" && pptxNotes.length === 0 && (
              <p className="status-caption">尚未偵測到投影片，請先在上一步上傳 PPTX 檔案</p>
            )}
          </div>
        )}

        {scriptMode === "AUTO" && slideCount > 0 && (
          <GenerateScriptButton
            label="產生 AI 講稿預覽"
            loadingLabel="AI 生成中，請稍候..."
            disabled={!geminiApiKey.trim() || scriptPreviewStatus === "loading"}
            status={scriptPreviewStatus}
            error={scriptPreviewError}
            onClick={onGeneratePreview}
          />
        )}

        {scriptMode === "AUTO" && scriptPreviewStatus === "done" && slideCount > 0 && (
          <div className="field full">
            <label>
              AI 產生的講稿 <span className="hint">— 可直接修改，滿意後再繼續下一步</span>
            </label>
            <PerSlideTextEditor
              slideCount={slideCount}
              perSlideTexts={perSlideTexts}
              setPerSlideTexts={setPerSlideTexts}
            />
          </div>
        )}

        {modeConfig.needsTexts && slideCount > 0 && (
          <div className="field full">
            <label>
              {scriptMode === "POLISH" ? "潤飾前的草稿" : "逐頁講稿"} <FieldTag required />
            </label>
            <div className="tab-group" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={textInputMode === "perSlide"}
                className={"tab" + (textInputMode === "perSlide" ? " selected" : "")}
                onClick={() => setTextInputMode("perSlide")}
              >
                逐頁輸入
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={textInputMode === "paste"}
                className={"tab" + (textInputMode === "paste" ? " selected" : "")}
                onClick={() => setTextInputMode("paste")}
              >
                整段貼上自動分頁
              </button>
            </div>

            {textInputMode === "perSlide" ? (
              <PerSlideTextEditor
                slideCount={slideCount}
                perSlideTexts={perSlideTexts}
                setPerSlideTexts={setPerSlideTexts}
              />
            ) : (
              <div className="field">
                <label htmlFor="paste-script-input">
                  <span className="hint">
                    用「第1頁」「第一頁」這樣的標記分開每一頁講稿，系統會自動依標記分頁
                  </span>
                </label>

                <ScriptFileUpload onExtracted={setPasteText} />

                <textarea
                  id="paste-script-input"
                  rows={10}
                  placeholder={"第1頁\n大家好，歡迎收看本次課程。\n第2頁\n這是第二頁的內容。"}
                  value={pasteText}
                  onChange={(e) => setPasteText(e.target.value)}
                />
                {pasteError ? (
                  <p className="form-error" style={{ margin: "8px 0 0" }}>
                    {pasteError}
                  </p>
                ) : (
                  pasteText.trim() && (
                    <p className="status-caption" style={{ marginTop: 8 }}>
                      已辨識 {detectedCount} / {slideCount} 頁講稿
                    </p>
                  )
                )}
              </div>
            )}
          </div>
        )}

        {scriptMode === "POLISH" && slideCount > 0 && (
          <GenerateScriptButton
            label={scriptPreviewStatus === "done" ? "重新產生 AI 潤飾預覽" : "產生 AI 潤飾預覽"}
            loadingLabel="AI 潤飾中，請稍候..."
            disabled={!geminiApiKey.trim() || !hasDraftText || scriptPreviewStatus === "loading"}
            status={scriptPreviewStatus}
            error={scriptPreviewError}
            onClick={onGeneratePreview}
          />
        )}

        {usesAiGeneration && scriptPreviewStatus === "done" && (
          <div className="field full">
            <p className="status-caption">
              ✓ 已產生預覽，可直接修改上方文字，確認沒問題後按「下一步」繼續
            </p>
          </div>
        )}
      </div>
    </>
  );
}

function VoiceStep({
  voice,
  setVoice,
  voiceRate,
  setVoiceRate,
  voiceVolume,
  setVoiceVolume,
  transition,
  setTransition,
  transitionDurationMs,
  setTransitionDurationMs,
}) {
  const [previewState, setPreviewState] = useState("idle"); // idle | loading | error
  const audioRef = useRef(null);

  async function handlePreview() {
    setPreviewState("loading");
    try {
      const blob = await fetchVoicePreview(voice, formatPercent(voiceRate), formatPercent(voiceVolume));
      const url = URL.createObjectURL(blob);
      if (audioRef.current) audioRef.current.pause();
      const audio = new Audio(url);
      audioRef.current = audio;
      audio.addEventListener("ended", () => URL.revokeObjectURL(url));
      await audio.play();
      setPreviewState("idle");
    } catch {
      setPreviewState("error");
    }
  }

  return (
    <>
      <CardHead eyebrow="Step 03" title="語音與轉場" trailing="全部已有預設值" />
      <div className="field-grid">
        <div className="field">
          <label htmlFor="voice-select">
            配音語者 <FieldTag required={false} />
          </label>
          <div className="field-with-action">
            <select id="voice-select" value={voice} onChange={(e) => setVoice(e.target.value)}>
              {VOICES.map((v) => (
                <option key={v.value} value={v.value}>
                  {v.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn-mini"
              onClick={handlePreview}
              disabled={previewState === "loading"}
            >
              {previewState === "loading" ? "載入中" : "▶ 預覽"}
            </button>
          </div>
          {previewState === "error" && (
            <p className="form-error" style={{ margin: "6px 0 0" }}>
              語音預覽失敗，請稍後再試
            </p>
          )}
        </div>

        <div className="field">
          <label htmlFor="transition-select">
            轉場效果 <FieldTag required={false} />
          </label>
          <select
            id="transition-select"
            value={transition}
            onChange={(e) => setTransition(e.target.value)}
          >
            {TRANSITIONS.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="rate-range">
            語速 <FieldTag required={false} /> <span className="hint">{formatPercent(voiceRate)}</span>
          </label>
          <input
            id="rate-range"
            type="range"
            min={RATE_MIN}
            max={RATE_MAX}
            step={5}
            value={voiceRate}
            onChange={(e) => setVoiceRate(Number(e.target.value))}
          />
        </div>

        <div className="field">
          <label htmlFor="volume-range">
            音量 <FieldTag required={false} /> <span className="hint">{formatPercent(voiceVolume)}</span>
          </label>
          <input
            id="volume-range"
            type="range"
            min={VOLUME_MIN}
            max={VOLUME_MAX}
            step={5}
            value={voiceVolume}
            onChange={(e) => setVoiceVolume(Number(e.target.value))}
          />
        </div>

        <div className="field">
          <label htmlFor="transition-ms-select">
            轉場時間 <FieldTag required={false} />
          </label>
          <select
            id="transition-ms-select"
            value={transitionDurationMs}
            onChange={(e) => setTransitionDurationMs(Number(e.target.value))}
          >
            {TRANSITION_DURATIONS.map((ms) => (
              <option key={ms} value={ms}>
                {(ms / 1000).toFixed(1).replace(/\.0$/, "")} 秒
              </option>
            ))}
          </select>
        </div>
      </div>
    </>
  );
}

function ExtrasStep({
  resolution,
  setResolution,
  fontSize,
  setFontSize,
  subtitlePositionPercent,
  setSubtitlePositionPercent,
  subtitleFontColor,
  setSubtitleFontColor,
  subtitleOutlineColor,
  setSubtitleOutlineColor,
  subtitleBold,
  setSubtitleBold,
  previewImageUrl,
  logoFile,
  setLogoFile,
  logoOpacity,
  setLogoOpacity,
  logoPosition,
  setLogoPosition,
  logoSize,
  setLogoSize,
  bgmFile,
  setBgmFile,
  introFile,
  setIntroFile,
  outroFile,
  setOutroFile,
  customDict,
  setCustomDict,
  avatarMode,
  setAvatarMode,
  avatarPosition,
  setAvatarPosition,
  avatarSize,
  setAvatarSize,
  avatarCustomSlides,
  setAvatarCustomSlides,
  slideCount,
  readingPauseSec,
  setReadingPauseSec,
  closingPauseSec,
  setClosingPauseSec,
  targetDurationEnabled,
  setTargetDurationEnabled,
  targetDurationSec,
  setTargetDurationSec,
  avoidVoiceOverlap,
  setAvoidVoiceOverlap,
}) {
  return (
    <>
      <CardHead eyebrow="Step 04" title="進階選項" trailing="全部選填" />
      <div className="field-grid">
        <div className="field">
          <label htmlFor="resolution-select">
            解析度 <FieldTag required={false} />
          </label>
          <select
            id="resolution-select"
            value={resolution}
            onChange={(e) => setResolution(e.target.value)}
          >
            {RESOLUTIONS.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="font-size-select">
            字幕大小 <FieldTag required={false} />
          </label>
          <select
            id="font-size-select"
            value={fontSize}
            onChange={(e) => setFontSize(Number(e.target.value))}
          >
            {FONT_SIZES.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}（約 {scaledFontSize(f.value, resolution)}px）
              </option>
            ))}
          </select>
        </div>

        <SubtitlePositionField
          percent={subtitlePositionPercent}
          setPercent={setSubtitlePositionPercent}
          previewImageUrl={previewImageUrl}
          fontSize={fontSize}
          fontColor={subtitleFontColor}
          outlineColor={subtitleOutlineColor}
          bold={subtitleBold}
        />

        <SubtitleColorField
          fontColor={subtitleFontColor}
          setFontColor={setSubtitleFontColor}
          outlineColor={subtitleOutlineColor}
          setOutlineColor={setSubtitleOutlineColor}
          bold={subtitleBold}
          setBold={setSubtitleBold}
        />

        <CollapsibleField
          title="自訂詞庫"
          defaultOpen={customDict.trim().length > 0}
          summary={
            customDict.trim()
              ? `${customDict.split("\n").filter((l) => l.trim()).length} 個詞`
              : "尚未設定"
          }
        >
          <div className="field full">
            <label htmlFor="custom-dict-textarea">字幕斷詞用詞庫</label>
            <textarea
              id="custom-dict-textarea"
              rows={3}
              value={customDict}
              onChange={(e) => setCustomDict(e.target.value)}
              placeholder={"每行一個詞，例如公司名稱或專業術語：\n普拉斯提亞雲端\n職場健康促進計畫"}
            />
            <p className="hint" style={{ margin: "6px 0 0" }}>
              讓字幕換行辨識這些詞，不會把它們從中間拆成兩行；不影響配音發音。
            </p>
          </div>
        </CollapsibleField>

        <CollapsibleField
          title="Logo 圖片（浮水印）"
          defaultOpen={Boolean(logoFile)}
          summary={logoFile ? logoFile.name : "尚未設定"}
        >
          <LogoField
            file={logoFile}
            onChange={setLogoFile}
            opacity={logoOpacity}
            setOpacity={setLogoOpacity}
            position={logoPosition}
            setPosition={setLogoPosition}
            size={logoSize}
            setSize={setLogoSize}
            resolution={resolution}
            previewImageUrl={previewImageUrl}
          />
        </CollapsibleField>

        <CollapsibleField
          title="背景音樂／片頭／片尾影片"
          defaultOpen={Boolean(bgmFile || introFile || outroFile)}
          summary={
            [bgmFile && "背景音樂", introFile && "片頭", outroFile && "片尾"].filter(Boolean).join("、") ||
            "尚未設定"
          }
        >
          <div className="field-grid">
            <FileField label="背景音樂" file={bgmFile} onChange={setBgmFile} accept="audio/*" />
            <FileField label="片頭影片" file={introFile} onChange={setIntroFile} accept="video/*" />
            <FileField label="片尾影片" file={outroFile} onChange={setOutroFile} accept="video/*" />
          </div>
        </CollapsibleField>

        <CollapsibleField
          title="2D 講師 Avatar"
          defaultOpen
          summary={AVATAR_MODES.find((m) => m.value === avatarMode)?.label}
        >
          <AvatarField
            mode={avatarMode}
            setMode={setAvatarMode}
            position={avatarPosition}
            setPosition={setAvatarPosition}
            size={avatarSize}
            setSize={setAvatarSize}
            customSlides={avatarCustomSlides}
            setCustomSlides={setAvatarCustomSlides}
            slideCount={slideCount}
          />
        </CollapsibleField>

        <CollapsibleField
          title="節奏控制（停頓 ／ 聲音重疊）"
          defaultOpen
          summary="使用預設節奏"
        >
          <PacingField
            readingPauseSec={readingPauseSec}
            setReadingPauseSec={setReadingPauseSec}
            closingPauseSec={closingPauseSec}
            setClosingPauseSec={setClosingPauseSec}
            targetDurationEnabled={targetDurationEnabled}
            setTargetDurationEnabled={setTargetDurationEnabled}
            targetDurationSec={targetDurationSec}
            setTargetDurationSec={setTargetDurationSec}
            avoidVoiceOverlap={avoidVoiceOverlap}
            setAvoidVoiceOverlap={setAvoidVoiceOverlap}
          />
        </CollapsibleField>
      </div>
    </>
  );
}

const READING_PAUSE_MAX_SEC = 5;
const CLOSING_PAUSE_MAX_SEC = 10;
const TARGET_DURATION_MIN_SEC = 10;
const TARGET_DURATION_MAX_SEC = 3600;

function formatSecondsAsMinSec(totalSec) {
  const s = Math.max(0, Math.round(totalSec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

// Reading Pause / 目標影片長度 only ever extend a slide's own *visual* hold
// time (and, via silent padding baked in server-side, its audio track) —
// they never touch the real narration audio, its chunks, or the subtitle
// cues generated from it. See video.py's narration_durations_ms vs.
// visual_durations_ms split.
function PacingField({
  readingPauseSec,
  setReadingPauseSec,
  closingPauseSec,
  setClosingPauseSec,
  targetDurationEnabled,
  setTargetDurationEnabled,
  targetDurationSec,
  setTargetDurationSec,
  avoidVoiceOverlap,
  setAvoidVoiceOverlap,
}) {
  return (
    <>
      <p className="hint" style={{ margin: "0 0 8px" }}>
        只會延長畫面停留（必要時加上靜音），不會改變任何一句旁白配音本身的長度或語速。
      </p>

      <label className="pacing-toggle">
        <input
          type="checkbox"
          checked={targetDurationEnabled}
          onChange={(e) => setTargetDurationEnabled(e.target.checked)}
        />
        設定目標影片長度，自動分配停頓時間
      </label>

      {targetDurationEnabled ? (
        <div style={{ marginTop: 8 }}>
          <label htmlFor="target-duration-range">
            目標長度 <span className="hint">{formatSecondsAsMinSec(targetDurationSec)}</span>
          </label>
          <input
            id="target-duration-range"
            type="range"
            min={TARGET_DURATION_MIN_SEC}
            max={TARGET_DURATION_MAX_SEC}
            step={5}
            value={targetDurationSec}
            onChange={(e) => setTargetDurationSec(Number(e.target.value))}
          />
          <p className="hint" style={{ margin: "6px 0 0" }}>
            系統會把每一頁的閱讀停頓平均分配以貼近這個長度；如果旁白本身總長已經超過目標，會停在旁白最短能到的長度，絕不會加快語速或剪短配音。
          </p>
        </div>
      ) : (
        <div className="field-grid" style={{ marginTop: 8 }}>
          <div className="field">
            <label htmlFor="reading-pause-range">
              每頁閱讀停頓 <span className="hint">{readingPauseSec.toFixed(1)} 秒</span>
            </label>
            <input
              id="reading-pause-range"
              type="range"
              min={0}
              max={READING_PAUSE_MAX_SEC}
              step={0.1}
              value={readingPauseSec}
              onChange={(e) => setReadingPauseSec(Number(e.target.value))}
            />
          </div>
          <div className="field">
            <label htmlFor="closing-pause-range">
              結尾額外停留 <span className="hint">{closingPauseSec.toFixed(1)} 秒</span>
            </label>
            <input
              id="closing-pause-range"
              type="range"
              min={0}
              max={CLOSING_PAUSE_MAX_SEC}
              step={0.5}
              value={closingPauseSec}
              onChange={(e) => setClosingPauseSec(Number(e.target.value))}
            />
          </div>
        </div>
      )}

      <label className="pacing-toggle" style={{ marginTop: 10 }}>
        <input
          type="checkbox"
          checked={avoidVoiceOverlap}
          onChange={(e) => setAvoidVoiceOverlap(e.target.checked)}
        />
        轉場後才開口（避免上一頁的聲音跟下一頁疊在一起）
      </label>
      {avoidVoiceOverlap && (
        <p className="hint" style={{ margin: "6px 0 0" }}>
          畫面轉場動畫照舊播放，但下一頁的旁白會等上一頁完全講完（含閱讀停頓）才開始出聲；整支影片的總長度會因此變長，不會再因為轉場而縮短。
        </p>
      )}
    </>
  );
}

// 目前使用系統內建的簡約風格佔位角色（idle/talk_open/talk_close 嘴型會依真實
// 語音時間點自動切換），尚未支援上傳自訂角色圖檔。
function AvatarField({
  mode,
  setMode,
  position,
  setPosition,
  size,
  setSize,
  customSlides,
  setCustomSlides,
  slideCount,
}) {
  function toggleSlide(n) {
    setCustomSlides((prev) =>
      prev.includes(n) ? prev.filter((x) => x !== n) : [...prev, n].sort((a, b) => a - b)
    );
  }

  return (
    <>
      <label htmlFor="avatar-mode-select">顯示模式</label>
      <select
        id="avatar-mode-select"
        value={mode}
        onChange={(e) => setMode(e.target.value)}
      >
        {AVATAR_MODES.map((m) => (
          <option key={m.value} value={m.value}>
            {m.label}
          </option>
        ))}
      </select>
      <p className="hint" style={{ margin: "6px 0 0" }}>
        使用內建的簡約風格佔位角色，嘴型會依這一頁旁白的真實語音時間點自動開合，不會改變任何配音或字幕的時間軸。
      </p>

      {mode !== "none" && (
        <div className="field-grid" style={{ marginTop: 10 }}>
          <div className="field">
            <label htmlFor="avatar-position-select">位置</label>
            <select
              id="avatar-position-select"
              value={position}
              onChange={(e) => setPosition(e.target.value)}
            >
              {AVATAR_POSITIONS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="avatar-size-select">大小</label>
            <select
              id="avatar-size-select"
              value={size}
              onChange={(e) => setSize(e.target.value)}
            >
              {AVATAR_SIZES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>

          {mode === "custom" && slideCount > 0 && (
            <div className="field full">
              <label>選擇要顯示 Avatar 的頁面</label>
              <div className="avatar-slide-picker">
                {Array.from({ length: slideCount }, (_, i) => i + 1).map((n) => (
                  <label key={n} className="avatar-slide-picker-item">
                    <input
                      type="checkbox"
                      checked={customSlides.includes(n)}
                      onChange={() => toggleSlide(n)}
                    />
                    第 {n} 頁
                  </label>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </>
  );
}

function SubtitlePositionField({
  percent,
  setPercent,
  previewImageUrl,
  fontSize,
  fontColor,
  outlineColor,
  bold,
}) {
  // True proportional preview: ffmpeg/libass burns FontSize as a literal
  // pixel count against the real output's actual height (see
  // scaledFontSize / DEFAULT_FONT_SIZE), so on the real video the caption's
  // ink height is (referencePx / 1080) of the frame — the same fraction
  // regardless of which output resolution is chosen, by design. This
  // preview box keeps the same 16:9 aspect ratio as every selectable
  // output resolution, so converting that same fraction into cqw ("1% of
  // this box's own rendered width") reproduces the real relative size
  // exactly, with no separate fudge factor: what's shown here is what
  // actually renders. (An earlier version scaled against the preview
  // box's width with an arbitrary legibility factor instead of the real
  // output height — it looked fine in isolation, but didn't match the
  // real video, which is exactly what caused subtitles to come out much
  // smaller on screen than the preview suggested.)
  const PREVIEW_ASPECT_HEIGHT_OVER_WIDTH = 9 / 16;
  const previewFontSizeCqw = (fontSize / 1080) * PREVIEW_ASPECT_HEIGHT_OVER_WIDTH * 100;

  return (
    <div className="field full">
      <label htmlFor="subtitle-position-range">
        字幕高度 <FieldTag required={false} /> <span className="hint">{percent}%（距離畫面底部）</span>
      </label>
      <input
        id="subtitle-position-range"
        type="range"
        min={SUBTITLE_POSITION_MIN}
        max={SUBTITLE_POSITION_MAX}
        step={1}
        value={percent}
        onChange={(e) => setPercent(Number(e.target.value))}
      />

      <div
        className="subtitle-position-preview"
        style={previewImageUrl ? { backgroundImage: `url(${previewImageUrl})` } : undefined}
      >
        <span
          className="subtitle-position-preview-caption"
          style={{
            bottom: `${percent}%`,
            fontSize: `clamp(8px, ${previewFontSizeCqw}cqw, 72px)`,
            color: fontColor,
            fontWeight: bold ? 700 : 400,
            textShadow: [
              `-1px -1px 0 ${outlineColor}`,
              `1px -1px 0 ${outlineColor}`,
              `-1px 1px 0 ${outlineColor}`,
              `1px 1px 0 ${outlineColor}`,
              `0 0 6px ${outlineColor}`,
            ].join(", "),
          }}
        >
          字幕預覽文字
        </span>
      </div>
      <p className="hint" style={{ margin: "6px 0 0" }}>
        {previewImageUrl
          ? "字級、位置、顏色皆按實際畫面比例預覽"
          : "先在 Step 01 上傳投影片圖片，這裡就會用實際畫面預覽字幕高度與字級"}
      </p>
    </div>
  );
}

// Native <input type="color"> pickers -- always send a real 6-digit hex
// (browsers never leave a color input empty), so unlike the file/logo
// fields there's no "not set" state to represent; leaving both untouched
// simply resends the same white-text/black-outline defaults the backend
// already renders today, so this is fully backward compatible either way.
function SubtitleColorField({
  fontColor,
  setFontColor,
  outlineColor,
  setOutlineColor,
  bold,
  setBold,
}) {
  const isDefault =
    fontColor === SUBTITLE_FONT_COLOR_DEFAULT &&
    outlineColor === SUBTITLE_OUTLINE_COLOR_DEFAULT &&
    bold === SUBTITLE_BOLD_DEFAULT;

  return (
    <div className="field full">
      <label>
        字幕顏色與粗細 <FieldTag required={false} />
      </label>
      <div className="subtitle-color-field">
        <div className="subtitle-color-swatch">
          <label htmlFor="subtitle-font-color-input">文字顏色</label>
          <input
            id="subtitle-font-color-input"
            type="color"
            value={fontColor}
            onChange={(e) => setFontColor(e.target.value)}
          />
          <span className="hint">{fontColor.toUpperCase()}</span>
        </div>
        <div className="subtitle-color-swatch">
          <label htmlFor="subtitle-outline-color-input">外框顏色</label>
          <input
            id="subtitle-outline-color-input"
            type="color"
            value={outlineColor}
            onChange={(e) => setOutlineColor(e.target.value)}
          />
          <span className="hint">{outlineColor.toUpperCase()}</span>
        </div>
        <label className="pacing-toggle">
          <input
            type="checkbox"
            checked={bold}
            onChange={(e) => setBold(e.target.checked)}
          />
          粗體
        </label>
        {!isDefault && (
          <button
            type="button"
            className="btn-mini"
            onClick={() => {
              setFontColor(SUBTITLE_FONT_COLOR_DEFAULT);
              setOutlineColor(SUBTITLE_OUTLINE_COLOR_DEFAULT);
              setBold(SUBTITLE_BOLD_DEFAULT);
            }}
          >
            還原預設（白字黑框・非粗體）
          </button>
        )}
      </div>
      <p className="hint" style={{ margin: "6px 0 0" }}>
        上方的字幕預覽會同步套用這裡選的顏色與粗細。粗體只有開/關兩種，不是連續的粗細刻度——這是字幕格式（ASS/libass）本身的限制。
      </p>
    </div>
  );
}

function FileField({ label, file, onChange, accept }) {
  // Bumping this key remounts the native <input>, which is the only way to
  // clear its internal file selection — without it, removing a file then
  // picking that exact same file again silently does nothing (the browser
  // only fires onChange when the selection actually changes).
  const [resetKey, setResetKey] = useState(0);

  function handleRemove() {
    onChange(null);
    setResetKey((k) => k + 1);
  }

  return (
    <div className="field">
      <label>{label}</label>
      <div className="uploader">
        <input
          key={resetKey}
          type="file"
          accept={accept}
          onChange={(e) => onChange(e.target.files[0] || null)}
        />
        <span className="filename">{file ? file.name : "未選擇"}</span>
        {file ? (
          <button type="button" className="btn-mini btn-mini-remove" onClick={handleRemove}>
            移除
          </button>
        ) : (
          <span className="btn-mini">選擇</span>
        )}
      </div>
    </div>
  );
}

function LogoField({
  file,
  onChange,
  opacity,
  setOpacity,
  position,
  setPosition,
  size,
  setSize,
  resolution,
  previewImageUrl,
}) {
  const [resetKey, setResetKey] = useState(0);
  // `file ? [file] : []` is a fresh array on every render regardless of
  // whether `file` itself changed, so useObjectUrls' effect (keyed on that
  // array's identity) would re-run every render and set new state every
  // time — an infinite render loop. Memoizing on `file` keeps the array
  // reference stable across renders where the file hasn't actually changed.
  const previewFiles = useMemo(() => (file ? [file] : []), [file]);
  const previewUrls = useObjectUrls(previewFiles);
  const previewUrl = previewUrls[0];

  function handleRemove() {
    onChange(null);
    setOpacity(LOGO_OPACITY_DEFAULT);
    setResetKey((k) => k + 1);
  }

  return (
    <>
      <div className="uploader">
        <input
          key={resetKey}
          type="file"
          accept="image/*"
          onChange={(e) => onChange(e.target.files[0] || null)}
        />
        <span className="filename">{file ? file.name : "未選擇"}</span>
        {file ? (
          <button type="button" className="btn-mini btn-mini-remove" onClick={handleRemove}>
            移除
          </button>
        ) : (
          <span className="btn-mini">選擇</span>
        )}
      </div>

      {file && (
        <div className="logo-preview">
          <div className="logo-preview-frame">
            <img src={previewUrl} alt="Logo 預覽" style={{ opacity: opacity / 100 }} />
          </div>
          <div className="logo-opacity-control">
            <label htmlFor="logo-opacity-range">
              透明度 <span className="hint">{opacity}%</span>
            </label>
            <input
              id="logo-opacity-range"
              type="range"
              min={LOGO_OPACITY_MIN}
              max={LOGO_OPACITY_MAX}
              step={5}
              value={opacity}
              onChange={(e) => setOpacity(Number(e.target.value))}
            />
          </div>
        </div>
      )}

      {file && (
        <div className="logo-position-field">
          <div className="field-grid" style={{ marginBottom: 12 }}>
            <div className="field">
              <label htmlFor="logo-size-select">Logo 大小</label>
              <select
                id="logo-size-select"
                value={size}
                onChange={(e) => setSize(Number(e.target.value))}
              >
                {LOGO_SIZES.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}（約 {scaledLogoWidth(s.value, resolution)}px 寬）
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label htmlFor="logo-position-group">Logo 位置</label>
              <div
                id="logo-position-group"
                className="logo-position-grid"
                role="group"
                aria-label="Logo 位置"
              >
                {LOGO_POSITIONS.map((p) => (
                  <button
                    key={p.value}
                    type="button"
                    className={`logo-position-btn${position === p.value ? " active" : ""}`}
                    aria-pressed={position === p.value}
                    onClick={() => setPosition(p.value)}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div
            className="logo-position-preview"
            style={previewImageUrl ? { backgroundImage: `url(${previewImageUrl})` } : undefined}
          >
            <img
              className="logo-position-preview-logo"
              src={previewUrl}
              alt=""
              style={{
                ...logoPositionStyle(position, `${(LOGO_MARGIN_REFERENCE / 1920) * 100}cqw`),
                width: `${(size / 1920) * 100}cqw`,
                opacity: opacity / 100,
              }}
            />
          </div>
          <p className="hint" style={{ margin: "6px 0 0" }}>
            {previewImageUrl
              ? "大小、位置皆按實際畫面比例預覽"
              : "先在 Step 01 上傳投影片圖片，這裡就會用實際畫面預覽 Logo 大小與位置"}
          </p>
        </div>
      )}
    </>
  );
}

function ScriptFileUpload({ onExtracted }) {
  const [extracting, setExtracting] = useState(false);
  const [extractError, setExtractError] = useState(null);

  async function handleChange(e) {
    const file = e.target.files[0];
    if (!file) return;
    setExtracting(true);
    setExtractError(null);
    try {
      const { text } = await extractScriptText(file);
      onExtracted(text);
    } catch (err) {
      setExtractError(err.message);
    } finally {
      setExtracting(false);
      e.target.value = "";
    }
  }

  return (
    <div style={{ marginBottom: 10 }}>
      <div className="uploader">
        <input type="file" accept=".txt,.docx" onChange={handleChange} disabled={extracting} />
        <span className="filename">{extracting ? "解析中..." : "或上傳講稿檔案 (.txt / .docx)"}</span>
        <span className="btn-mini">選擇</span>
      </div>
      {extractError && (
        <p className="form-error" style={{ margin: "8px 0 0" }}>
          {extractError}
        </p>
      )}
    </div>
  );
}

function VisualsStep({
  slideCount,
  thumbUrls,
  onAnalyze,
  canAnalyze,
  analysisStatus,
  analysisError,
  recommendations,
  analysisWarnings,
  pexelsApiKey,
  setPexelsApiKey,
  mediaSearchBySlide,
  onSearchMedia,
  brollChoices,
  setBrollChoices,
}) {
  const recommended = recommendations.filter((r) => r.recommended);
  const skipped = recommendations.filter((r) => !r.recommended);
  const confirmedCount = Object.values(brollChoices).filter((c) => c && c.asset).length;

  return (
    <>
      <CardHead
        eyebrow="Step 05"
        title="AI 視覺素材"
        trailing={slideCount ? `${slideCount} 頁投影片・全部選填` : "全部選填"}
      />
      <div className="field-grid">
        <div className="field full">
          <p className="hint">
            AI 會分析每一頁的內容，建議哪些頁面加入額外圖片能提升理解——只有你確認選用的素材才會真的出現在影片裡。
            這個功能完全選填，時間也不會超出該頁旁白本身的長度，不會影響配音與字幕的時間軸。
          </p>
        </div>

        <div className="field full">
          <label htmlFor="pexels-key-input">
            Pexels API Key <FieldTag required={false} />
          </label>
          <p className="hint" style={{ margin: "0 0 8px" }}>
            AI 建議「這頁適合加圖片/影片」之後，就是用這組 Key 去 Pexels（免費圖庫網站）搜尋素材縮圖給你挑選；
            不填的話，這個搜尋功能不能用（只會顯示提示訊息），但完全不影響其他功能，也不會讓製作失敗。
          </p>
          <input
            id="pexels-key-input"
            type="password"
            value={pexelsApiKey}
            onChange={(e) => setPexelsApiKey(e.target.value)}
            placeholder="貼上你的 Pexels API Key（可留空）"
          />
          <a href={PEXELS_API_KEY_URL} target="_blank" rel="noreferrer" className="hint-link">
            還沒有 Pexels API Key？點此免費申請 →
          </a>
        </div>

        <GenerateScriptButton
          label={analysisStatus === "done" ? "重新分析視覺素材建議" : "分析 PPT 並產生視覺建議"}
          loadingLabel="AI 分析中，請稍候（會為建議頁面合成語音以抓取最適合的出現時間，可能需要一些時間）..."
          disabled={!canAnalyze || analysisStatus === "loading"}
          status={analysisStatus}
          error={analysisError}
          onClick={onAnalyze}
        />

        {!canAnalyze && (
          <div className="field full">
            <p className="status-caption">請先完成上一步「講稿來源」的設定，才能分析視覺素材建議</p>
          </div>
        )}

        {analysisStatus === "done" && analysisWarnings.length > 0 && (
          <div className="field full">
            {analysisWarnings.map((w, i) => (
              <p key={i} className="status-caption">
                {w}
              </p>
            ))}
          </div>
        )}

        {analysisStatus === "done" && recommended.length === 0 && (
          <div className="field full">
            <p className="status-caption">AI 判斷目前每一頁的內容都不需要額外視覺素材，可以直接進入下一步。</p>
          </div>
        )}

        {analysisStatus === "done" && recommended.length > 0 && (
          <div className="field full">
            <label>
              建議加入視覺素材的頁面
              {confirmedCount > 0 && <span className="hint"> — 已選用 {confirmedCount} 頁</span>}
            </label>
            <div className="visual-recommendations">
              {recommended.map((rec) => (
                <VisualRecommendationCard
                  key={rec.slide_number}
                  recommendation={rec}
                  suggestedStartSec={rec.suggested_start_ms / 1000}
                  suggestedDurationSec={Math.max(0.5, (rec.suggested_end_ms - rec.suggested_start_ms) / 1000)}
                  thumbUrl={thumbUrls[rec.slide_number - 1]}
                  searchState={mediaSearchBySlide[rec.slide_number]}
                  onSearch={() => onSearchMedia(rec.slide_number, rec.keywords[0] || rec.title)}
                  choice={brollChoices[rec.slide_number]}
                  setChoice={(choice) =>
                    setBrollChoices((prev) => ({ ...prev, [rec.slide_number]: choice }))
                  }
                />
              ))}
            </div>
          </div>
        )}

        {analysisStatus === "done" && skipped.length > 0 && (
          <div className="field full">
            <p className="hint">其餘 {skipped.length} 頁 AI 判斷不需要額外素材，已略過。</p>
          </div>
        )}
      </div>
    </>
  );
}

function VisualRecommendationCard({
  recommendation,
  suggestedStartSec,
  suggestedDurationSec,
  thumbUrl,
  searchState,
  onSearch,
  choice,
  setChoice,
}) {
  const assets = searchState?.assets || [];
  const startSec = choice?.startSec ?? suggestedStartSec;
  const durationSec = choice?.durationSec ?? suggestedDurationSec;

  function selectAsset(asset) {
    setChoice(asset ? { asset, startSec, durationSec } : null);
  }

  return (
    <div className="visual-rec-card">
      <div className="visual-rec-head">
        {thumbUrl && <img src={thumbUrl} alt="" className="visual-rec-thumb" />}
        <div>
          <p className="visual-rec-title">
            第 {recommendation.slide_number} 頁・{recommendation.title || "（無標題）"}
          </p>
          <p className="hint">
            建議分數 {recommendation.visual_need_score}／100 — {recommendation.reason}
          </p>
        </div>
      </div>

      {assets.length === 0 && (
        <button
          type="button"
          className="btn btn-ghost"
          onClick={onSearch}
          disabled={searchState?.status === "loading"}
        >
          {searchState?.status === "loading" ? "搜尋素材中..." : "搜尋候選素材"}
        </button>
      )}

      {searchState?.warning && <p className="status-caption">{searchState.warning}</p>}

      {assets.length > 0 && (
        <>
          <div className="visual-candidates">
            <label className="visual-candidate visual-candidate-none">
              <input type="radio" checked={!choice} onChange={() => selectAsset(null)} />
              不使用
            </label>
            {assets.map((asset, i) => (
              <label key={i} className="visual-candidate">
                <input
                  type="radio"
                  checked={choice?.asset?.preview_url === asset.preview_url}
                  onChange={() => selectAsset(asset)}
                />
                <img src={asset.preview_url} alt={asset.keyword} className="visual-candidate-thumb" />
              </label>
            ))}
          </div>

          {choice?.asset && (
            <div className="visual-timing">
              <label>
                出現時間（秒）
                <input
                  type="number"
                  min={0}
                  step={0.5}
                  value={startSec}
                  onChange={(e) => setChoice({ ...choice, startSec: Number(e.target.value) })}
                />
              </label>
              <label>
                持續秒數
                <input
                  type="number"
                  min={0.5}
                  step={0.5}
                  value={durationSec}
                  onChange={(e) => setChoice({ ...choice, durationSec: Number(e.target.value) })}
                />
              </label>
              <span className="hint">
                時間已依 AI 判斷旁白最相關的位置自動帶入，可自行調整；實際秒數仍會依該頁旁白長度自動裁切，不會超出旁白範圍
              </span>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function ReviewStep({
  baseName,
  slideCount,
  modeLabel,
  voiceLabel,
  voiceRate,
  voiceVolume,
  transitionLabel,
  transitionDurationMs,
  resolutionLabel,
  fontSizeLabel,
  subtitlePositionPercent,
  extras,
}) {
  const rows = useMemo(
    () => [
      ["課程名稱", baseName || "課程"],
      ["投影片頁數", `${slideCount} 頁`],
      ["講稿模式", modeLabel],
      ["配音語者", voiceLabel],
      ["語速 / 音量", `${formatPercent(voiceRate)} / ${formatPercent(voiceVolume)}`],
      [
        "轉場效果",
        `${transitionLabel}（${(transitionDurationMs / 1000).toFixed(1).replace(/\.0$/, "")} 秒）`,
      ],
      ["解析度", resolutionLabel],
      ["字幕大小", fontSizeLabel],
      ["字幕高度", `距離底部 ${subtitlePositionPercent}%`],
      ["額外項目", extras.length ? extras.join("、") : "無"],
    ],
    [
      baseName,
      slideCount,
      modeLabel,
      voiceLabel,
      voiceRate,
      voiceVolume,
      transitionLabel,
      transitionDurationMs,
      resolutionLabel,
      fontSizeLabel,
      subtitlePositionPercent,
      extras,
    ]
  );

  return (
    <>
      <CardHead eyebrow="Step 06" title="確認並開始製作" />
      <dl className="review-list">
        {rows.map(([label, value]) => (
          <div key={label} className="review-row">
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </>
  );
}

// Marks a field's label as either "必填" (blocks moving on if left empty) or
// "選填" (has a working default, or is an entirely optional add-on). Kept as
// a tiny standalone component so every field spells out the same rule in
// the same place, instead of each step inventing its own wording.
function FieldTag({ required }) {
  return (
    <span className={"field-req-badge" + (required ? " required" : " optional")}>
      {required ? "必填" : "選填"}
    </span>
  );
}

// Collapsed-by-default wrapper for an optional feature block (Logo, BGM,
// Avatar, pacing controls, ...). `defaultOpen` should reflect whether the
// field already carries a non-default value (e.g. a file was already
// chosen) so returning to this step doesn't hide something the user already
// set up. Each step remounts this component fresh when the wizard
// navigates away and back, so re-deriving `defaultOpen` from current state
// on mount is enough — no extra sync effect needed.
function CollapsibleField({ title, defaultOpen = false, summary, children }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={"field full collapsible-field" + (open ? " open" : "")}>
      <button
        type="button"
        className="collapsible-toggle"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="collapsible-toggle-title">
          {title}
          <FieldTag required={false} />
        </span>
        <span className="collapsible-toggle-right">
          {!open && summary && <span className="collapsible-toggle-summary">{summary}</span>}
          <span className={"collapsible-chevron" + (open ? " open" : "")} aria-hidden="true">
            ▾
          </span>
        </span>
      </button>
      {open && <div className="collapsible-body">{children}</div>}
    </div>
  );
}

function CardHead({ eyebrow, title, trailing }) {
  return (
    <div className="card-head">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h2>{title}</h2>
      </div>
      {trailing && <span className="reel-code">{trailing}</span>}
    </div>
  );
}

function HelpWidget() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    function handleKeyDown(e) {
      if (e.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open]);

  return (
    <>
      <button
        type="button"
        className="help-fab"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-label="使用說明"
      >
        ?
      </button>

      {open && (
        <div className="help-backdrop" onClick={() => setOpen(false)}>
          <div
            className="help-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="help-panel-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="help-panel-head">
              <div>
                <span className="eyebrow">Quick Start</span>
                <h2 id="help-panel-title">快速上手</h2>
              </div>
              <button
                type="button"
                className="help-close"
                onClick={() => setOpen(false)}
                aria-label="關閉說明"
              >
                ✕
              </button>
            </div>

            <ol className="help-steps">
              <li>
                <b>上傳投影片</b>
                <span>上傳 .pptx 檔案，加上每一頁的截圖，並設定課程名稱。</span>
              </li>
              <li>
                <b>講稿來源</b>
                <span>用投影片備忘稿、自己貼上／上傳講稿，或讓 AI 生成或潤飾。</span>
              </li>
              <li>
                <b>語音與轉場</b>
                <span>選配音角色、語速與音量，可先按「預覽配音」試聽。</span>
              </li>
              <li>
                <b>進階選項</b>
                <span>
                  Logo、背景音樂、片頭尾、字幕大小與高度、自訂詞庫，全部選填，不設定也沒關係。
                </span>
              </li>
              <li>
                <b>開始製作</b>
                <span>送出後排隊處理，完成後可下載影片、字幕檔、逐字稿。</span>
              </li>
            </ol>

            <div className="help-tips">
              <p className="help-tips-label">小提醒</p>
              <ul>
                <li>
                  PPTX 只用來讓系統讀取文字（投影片內容、備忘稿）；真正剪進影片畫面的是你上傳的圖片 —
                  用截圖而非系統重畫投影片，才不會字型跑掉、版面跑版
                </li>
                <li>自己貼講稿時，用「第1頁」「第一頁」這樣的格式標記每一頁</li>
                <li>
                  講稿裡有公司名稱、專有名詞怕字幕被拆成兩半？在「自訂詞庫」每行填一個詞，換行就會避開它們
                </li>
                <li>完成後除了下載，也能用 QR Code 讓手機直接掃碼下載</li>
                <li>產出的檔案會保留 24 小時，請盡快下載</li>
              </ul>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function MobileDownloadQr({ jobId }) {
  const canvasRef = useRef(null);
  // Points at the /share landing page, not the raw download link directly —
  // a QR scanner's in-app browser silently triggers-and-abandons a direct
  // attachment download (blank tab, no confirmation). /share gives the
  // phone something to land on and choose preview or download from.
  const absoluteUrl = toAbsoluteUrl(`/share/${jobId}`);

  useEffect(() => {
    if (!canvasRef.current) return;
    QRCode.toCanvas(canvasRef.current, absoluteUrl, { width: 148, margin: 1 }).catch(() => {});
  }, [absoluteUrl]);

  return (
    <div className="qr-block">
      <canvas ref={canvasRef} />
      <p className="status-caption">掃描 QR Code，在手機上下載課程影片</p>
    </div>
  );
}

function StatusView({ jobId, jobStatus, isRunning, isDone, isError, onReset }) {
  return (
    <div className="status-panel">
      <div className="top-row">
        <span className={"pill " + (jobStatus?.status || "queued")}>
          {jobStatus?.status === "queued" && "排隊中"}
          {jobStatus?.status === "running" && "製作中"}
          {jobStatus?.status === "done" && "完成"}
          {jobStatus?.status === "error" && "發生錯誤"}
        </span>
        <span className="take-id">TAKE #{jobId.slice(0, 8)}</span>
      </div>

      {isRunning && (
        <p className="status-caption">
          {jobStatus.status === "queued"
            ? "排隊中，前面的任務跑完就輪到你了..."
            : "處理中，請稍候..."}
        </p>
      )}

      {isDone && (
        <>
          <div className="deliverables">
            {Object.entries(jobStatus.downloads).map(([type, path]) => (
              <a key={type} className="chip" href={downloadUrl(path)} download>
                <span className="ext">{type.toUpperCase()}</span>
                {DOWNLOAD_LABELS[type] || type}
              </a>
            ))}
          </div>

          {jobStatus.details && (
            <dl className="job-details">
              <div className="stat">
                <dt>影片容量</dt>
                <dd>{formatFileSizeMb(jobStatus.details.video_size_bytes)}</dd>
              </div>
              <div className="stat">
                <dt>影片時長</dt>
                <dd>{formatDurationMs(jobStatus.details.video_duration_ms)}</dd>
              </div>
              <div className="stat">
                <dt>講稿字數</dt>
                <dd>
                  {jobStatus.details.script_char_count == null
                    ? "—"
                    : `${jobStatus.details.script_char_count} 字`}
                </dd>
              </div>
              <div className="stat">
                <dt>生成耗時</dt>
                <dd>{formatGenerationSeconds(jobStatus.details.generation_seconds)}</dd>
              </div>
            </dl>
          )}

          {jobStatus.downloads.mp4 && <MobileDownloadQr jobId={jobId} />}
        </>
      )}

      {isError && <p className="error-msg">{jobStatus.error}</p>}

      {(isDone || isError) && (
        <button type="button" onClick={onReset} className="secondary-button">
          建立新任務
        </button>
      )}
    </div>
  );
}

export default App;
