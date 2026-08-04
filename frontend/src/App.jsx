import { useEffect, useMemo, useRef, useState } from "react";
import "./App.css";
import { createJob, downloadUrl, getJobStatus } from "./api";

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

const GEMINI_API_KEY_URL = "https://aistudio.google.com/app/apikey";
const POLL_INTERVAL_MS = 2000;

const DOWNLOAD_LABELS = { mp4: "課程影片", srt: "字幕檔", docx: "講稿" };

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
  const [perSlideTexts, setPerSlideTexts] = useState([]);
  const [geminiApiKey, setGeminiApiKey] = useState("");
  const [voice, setVoice] = useState(VOICES[0].value);
  const [transition, setTransition] = useState("fade");
  const [transitionDurationMs, setTransitionDurationMs] = useState(500);
  const [resolution, setResolution] = useState(RESOLUTIONS[0].value);
  const [logoFile, setLogoFile] = useState(null);
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

  useEffect(() => {
    if (!modeConfig.needsTexts) return;
    setPerSlideTexts((prev) => imageFiles.map((_, i) => prev[i] || ""));
  }, [imageFiles, modeConfig.needsTexts]);

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
    setJobId(null);
    setJobStatus(null);
    setFormError(null);
    setCurrentStep(1);
  }

  function validateStep(step) {
    if (step === 1) {
      if (!pptxFile) return "請上傳 .pptx 檔案";
      if (imageFiles.length === 0) return "請上傳每一頁投影片的圖片";
      return null;
    }
    if (step === 2) {
      if (modeConfig.needsApiKey && !geminiApiKey.trim()) return "此模式需要 Gemini API Key";
      if (modeConfig.needsTexts && perSlideTexts.some((t) => !t.trim()))
        return "請為每一頁投影片輸入講稿";
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
    if (logoFile) form.append("logo", logoFile);
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
                perSlideTexts={perSlideTexts}
                setPerSlideTexts={setPerSlideTexts}
              />
            )}

            {currentStep === 3 && (
              <VoiceStep
                voice={voice}
                setVoice={setVoice}
                transition={transition}
                setTransition={setTransition}
              />
            )}

            {currentStep === 4 && (
              <ExtrasStep
                transitionDurationMs={transitionDurationMs}
                setTransitionDurationMs={setTransitionDurationMs}
                resolution={resolution}
                setResolution={setResolution}
                logoFile={logoFile}
                setLogoFile={setLogoFile}
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
                transitionLabel={transitionLabel}
                resolutionLabel={resolutionLabel}
                extras={[
                  logoFile && "Logo",
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
  perSlideTexts,
  setPerSlideTexts,
}) {
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
            <label>逐頁講稿</label>
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
          </div>
        )}
      </div>
    </>
  );
}

function VoiceStep({ voice, setVoice, transition, setTransition }) {
  return (
    <>
      <CardHead eyebrow="Step 03" title="語音與轉場" />
      <div className="field-grid">
        <div className="field">
          <label htmlFor="voice-select">配音語者</label>
          <select id="voice-select" value={voice} onChange={(e) => setVoice(e.target.value)}>
            {VOICES.map((v) => (
              <option key={v.value} value={v.value}>
                {v.label}
              </option>
            ))}
          </select>
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
      </div>
    </>
  );
}

function ExtrasStep({
  transitionDurationMs,
  setTransitionDurationMs,
  resolution,
  setResolution,
  logoFile,
  setLogoFile,
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
          <label htmlFor="transition-ms-input">轉場時間（毫秒）</label>
          <input
            id="transition-ms-input"
            type="number"
            min="0"
            value={transitionDurationMs}
            onChange={(e) => setTransitionDurationMs(Number(e.target.value))}
          />
        </div>
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

        <FileField label="Logo 圖片（右上角浮水印）" file={logoFile} onChange={setLogoFile} accept="image/*" />
        <FileField label="背景音樂" file={bgmFile} onChange={setBgmFile} accept="audio/*" />
        <FileField label="片頭影片" file={introFile} onChange={setIntroFile} accept="video/*" />
        <FileField label="片尾影片" file={outroFile} onChange={setOutroFile} accept="video/*" />
      </div>
    </>
  );
}

function FileField({ label, file, onChange, accept }) {
  return (
    <div className="field">
      <label>{label}</label>
      <div className="uploader">
        <input type="file" accept={accept} onChange={(e) => onChange(e.target.files[0] || null)} />
        <span className="filename">{file ? file.name : "未選擇"}</span>
        <span className="btn-mini">選擇</span>
      </div>
    </div>
  );
}

function ReviewStep({ baseName, slideCount, modeLabel, voiceLabel, transitionLabel, resolutionLabel, extras }) {
  const rows = useMemo(
    () => [
      ["課程名稱", baseName || "課程"],
      ["投影片頁數", `${slideCount} 頁`],
      ["講稿模式", modeLabel],
      ["配音語者", voiceLabel],
      ["轉場效果", transitionLabel],
      ["解析度", resolutionLabel],
      ["額外項目", extras.length ? extras.join("、") : "無"],
    ],
    [baseName, slideCount, modeLabel, voiceLabel, transitionLabel, resolutionLabel, extras]
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
        <div className="deliverables">
          {Object.entries(jobStatus.downloads).map(([type, path]) => (
            <a key={type} className="chip" href={downloadUrl(path)} download>
              <span className="ext">{type.toUpperCase()}</span>
              {DOWNLOAD_LABELS[type] || type}
            </a>
          ))}
        </div>
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
