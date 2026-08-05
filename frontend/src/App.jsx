import { useEffect, useMemo, useRef, useState } from "react";
import QRCode from "qrcode";
import "./App.css";
import { createJob, downloadUrl, extractScriptText, fetchVoicePreview, getJobStatus } from "./api";
import { parseNumberedScript } from "./scriptParser";
import { resolveDownloadUrl } from "./urlUtils";

const SCRIPT_MODES = [
  { value: "NOTES", label: "使用投影片備忘稿", needsTexts: false, needsApiKey: false },
  { value: "OWN", label: "我自己輸入逐頁講稿", needsTexts: true, needsApiKey: false },
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

const FONT_SIZES = [
  { value: 16, label: "小 (16px)" },
  { value: 22, label: "中 (22px)" },
  { value: 30, label: "大 (30px)" },
  { value: 40, label: "特大 (40px)" },
];

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

const GEMINI_API_KEY_URL = "https://aistudio.google.com/app/apikey";
const POLL_INTERVAL_MS = 2000;

const DOWNLOAD_LABELS = { mp4: "課程影片", srt: "字幕檔", docx: "講稿" };

function formatPercent(n) {
  return `${n >= 0 ? "+" : ""}${n}%`;
}

function toAbsoluteUrl(path) {
  return resolveDownloadUrl(path, window.location.hostname, window.location.origin);
}

const STEPS = [
  { n: 1, key: "upload", label: "上傳投影片" },
  { n: 2, key: "script", label: "講稿來源" },
  { n: 3, key: "voice", label: "語音與轉場" },
  { n: 4, key: "extras", label: "進階選項" },
  { n: 5, key: "review", label: "開始製作" },
];

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
  const [imageFiles, setImageFiles] = useState([]);
  const [baseName, setBaseName] = useState("課程");
  const [scriptMode, setScriptMode] = useState("NOTES");
  const [textInputMode, setTextInputMode] = useState("perSlide");
  const [perSlideTexts, setPerSlideTexts] = useState([]);
  const [pasteText, setPasteText] = useState("");
  const [pasteError, setPasteError] = useState(null);
  const [geminiApiKey, setGeminiApiKey] = useState("");
  const [voice, setVoice] = useState(VOICES[0].value);
  const [voiceRate, setVoiceRate] = useState(0);
  const [voiceVolume, setVoiceVolume] = useState(0);
  const [transition, setTransition] = useState("fade");
  const [transitionDurationMs, setTransitionDurationMs] = useState(500);
  const [resolution, setResolution] = useState(RESOLUTIONS[0].value);
  const [fontSize, setFontSize] = useState(FONT_SIZES[1].value);
  const [logoFile, setLogoFile] = useState(null);
  const [logoOpacity, setLogoOpacity] = useState(LOGO_OPACITY_DEFAULT);
  const [bgmFile, setBgmFile] = useState(null);
  const [introFile, setIntroFile] = useState(null);
  const [outroFile, setOutroFile] = useState(null);

  const [submitting, setSubmitting] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [formError, setFormError] = useState(null);

  const pollTimerRef = useRef(null);
  const thumbUrls = useObjectUrls(imageFiles);

  const modeConfig = SCRIPT_MODES.find((m) => m.value === scriptMode);
  const voiceLabel = VOICES.find((v) => v.value === voice)?.label;
  const transitionLabel = TRANSITIONS.find((t) => t.value === transition)?.label;
  const resolutionLabel = RESOLUTIONS.find((r) => r.value === resolution)?.label;
  const fontSizeLabel = FONT_SIZES.find((f) => f.value === fontSize)?.label;

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
    setLogoFile(null);
    setLogoOpacity(LOGO_OPACITY_DEFAULT);
    setBgmFile(null);
    setIntroFile(null);
    setOutroFile(null);
    setSubmitting(false);
  }

  function validateStep(step) {
    if (step === 1) {
      if (!pptxFile) return "請上傳 .pptx 檔案";
      if (imageFiles.length === 0) return "請上傳每一頁投影片的圖片";
      return null;
    }
    if (step === 2) {
      if (modeConfig.needsApiKey && !geminiApiKey.trim()) return "此模式需要 Gemini API Key";
      if (modeConfig.needsTexts) {
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

    const form = new FormData();
    form.append("pptx", pptxFile);
    imageFiles.forEach((file) => form.append("images", file));
    form.append("script_mode", scriptMode);
    form.append("voice", voice);
    form.append("rate", formatPercent(voiceRate));
    form.append("volume", formatPercent(voiceVolume));
    form.append("base_name", baseName || "課程");
    if (modeConfig.needsTexts) {
      form.append("texts", JSON.stringify(perSlideTexts));
    }
    if (modeConfig.needsApiKey) {
      form.append("gemini_api_key", geminiApiKey);
    }
    form.append("transition", transition);
    form.append("transition_duration_ms", String(transitionDurationMs));
    form.append("resolution_width", width);
    form.append("resolution_height", height);
    form.append("font_size", String(fontSize));
    if (logoFile) {
      form.append("logo", logoFile);
      form.append("logo_opacity", String(logoOpacity / 100));
    }
    if (bgmFile) form.append("bgm", bgmFile);
    if (introFile) form.append("intro", introFile);
    if (outroFile) form.append("outro", outroFile);

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
          PPT2Course <span className="sub">AI</span>
        </p>
        <p className="tagline">把 PowerPoint 投影片轉成有旁白、有字幕的課程影片</p>
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
                logoFile={logoFile}
                setLogoFile={setLogoFile}
                logoOpacity={logoOpacity}
                setLogoOpacity={setLogoOpacity}
                bgmFile={bgmFile}
                setBgmFile={setBgmFile}
                introFile={introFile}
                setIntroFile={setIntroFile}
                outroFile={outroFile}
                setOutroFile={setOutroFile}
              />
            )}

            {currentStep === 5 && (
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
                extras={[
                  logoFile && `Logo（透明度 ${logoOpacity}%）`,
                  bgmFile && "背景音樂",
                  introFile && "片頭",
                  outroFile && "片尾",
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

function UploadStep({ pptxFile, setPptxFile, imageFiles, setImageFiles, thumbUrls, baseName, setBaseName }) {
  return (
    <>
      <CardHead eyebrow="Step 01" title="上傳投影片" trailing={imageFiles.length ? `${imageFiles.length} 頁投影片` : null} />
      <div className="field-grid">
        <div className="field full">
          <label htmlFor="pptx-input">PPTX 檔案</label>
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
        </div>

        <div className="field full">
          <label htmlFor="images-input">
            每頁投影片的圖片 <span className="hint">— 依順序全選，張數需與 PPTX 頁數相同</span>
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
          <label htmlFor="base-name-input">課程名稱（輸出檔名）</label>
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
}) {
  const detectedCount = perSlideTexts.filter((t) => t.trim()).length;

  return (
    <>
      <CardHead eyebrow="Step 02" title="講稿來源" trailing={slideCount ? `${slideCount} 頁投影片` : null} />
      <div className="field-grid">
        <div className="field full">
          <label>模式</label>
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
              Gemini API Key <span className="hint">— 只用於這次任務，不會被儲存</span>
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

        {modeConfig.needsTexts && slideCount > 0 && (
          <div className="field full">
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
      <CardHead eyebrow="Step 03" title="語音與轉場" />
      <div className="field-grid">
        <div className="field">
          <label htmlFor="voice-select">配音語者</label>
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
          <label htmlFor="transition-select">轉場效果</label>
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
            語速 <span className="hint">{formatPercent(voiceRate)}</span>
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
            音量 <span className="hint">{formatPercent(voiceVolume)}</span>
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
          <label htmlFor="transition-ms-select">轉場時間</label>
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
  logoFile,
  setLogoFile,
  logoOpacity,
  setLogoOpacity,
  bgmFile,
  setBgmFile,
  introFile,
  setIntroFile,
  outroFile,
  setOutroFile,
}) {
  return (
    <>
      <CardHead eyebrow="Step 04" title="進階選項" trailing="全部選填" />
      <div className="field-grid">
        <div className="field">
          <label htmlFor="resolution-select">解析度</label>
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
          <label htmlFor="font-size-select">字幕大小</label>
          <select
            id="font-size-select"
            value={fontSize}
            onChange={(e) => setFontSize(Number(e.target.value))}
          >
            {FONT_SIZES.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
        </div>

        <LogoField
          file={logoFile}
          onChange={setLogoFile}
          opacity={logoOpacity}
          setOpacity={setLogoOpacity}
        />
        <FileField label="背景音樂" file={bgmFile} onChange={setBgmFile} accept="audio/*" />
        <FileField label="片頭影片" file={introFile} onChange={setIntroFile} accept="video/*" />
        <FileField label="片尾影片" file={outroFile} onChange={setOutroFile} accept="video/*" />
      </div>
    </>
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

function LogoField({ file, onChange, opacity, setOpacity }) {
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
    <div className="field full">
      <label>Logo 圖片（右上角浮水印）</label>
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
    </div>
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
      extras,
    ]
  );

  return (
    <>
      <CardHead eyebrow="Step 05" title="確認並開始製作" />
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
                <span>Logo、背景音樂、片頭尾、字幕大小，全部選填，不設定也沒關係。</span>
              </li>
              <li>
                <b>開始製作</b>
                <span>送出後排隊處理，完成後可下載影片、字幕檔、逐字稿。</span>
              </li>
            </ol>

            <div className="help-tips">
              <p className="help-tips-label">小提醒</p>
              <ul>
                <li>自己貼講稿時，用「第1頁」「第一頁」這樣的格式標記每一頁</li>
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
