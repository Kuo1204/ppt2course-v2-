import { useEffect, useRef, useState } from "react";
import "./App.css";
import { createJob, downloadUrl, getJobStatus } from "./api";

const SCRIPT_MODES = [
  { value: "NOTES", label: "使用投影片備忘稿", needsTexts: false, needsApiKey: false },
  { value: "OWN", label: "我自己輸入逐頁講稿", needsTexts: true, needsApiKey: false },
  { value: "AUTO", label: "AI 自動生成講稿 (Gemini)", needsTexts: false, needsApiKey: true },
  { value: "POLISH", label: "AI 潤飾我輸入的講稿 (Gemini)", needsTexts: true, needsApiKey: true },
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

const DOWNLOAD_LABELS = { mp4: "課程影片 (mp4)", srt: "字幕檔 (srt)", docx: "講稿 (docx)" };

function App() {
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
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [logoFile, setLogoFile] = useState(null);
  const [bgmFile, setBgmFile] = useState(null);
  const [introFile, setIntroFile] = useState(null);
  const [outroFile, setOutroFile] = useState(null);

  const [submitting, setSubmitting] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [formError, setFormError] = useState(null);

  const pollTimerRef = useRef(null);

  const modeConfig = SCRIPT_MODES.find((m) => m.value === scriptMode);

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
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setFormError(null);

    if (!pptxFile) {
      setFormError("請上傳 .pptx 檔案");
      return;
    }
    if (imageFiles.length === 0) {
      setFormError("請上傳每一頁投影片的圖片");
      return;
    }
    if (modeConfig.needsApiKey && !geminiApiKey.trim()) {
      setFormError("此模式需要 Gemini API Key");
      return;
    }
    if (modeConfig.needsTexts && perSlideTexts.some((t) => !t.trim())) {
      setFormError("請為每一頁投影片輸入講稿");
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
    <div className="page">
      <header className="page-header">
        <h1>PPT2Course AI</h1>
        <p>把 PowerPoint 投影片轉成有旁白、有字幕的課程影片</p>
      </header>

      {jobId ? (
        <section className="card">
          <h2>任務進度</h2>
          <p className="job-id">任務編號：{jobId}</p>

          {isRunning && (
            <div className="status status-running">
              <span className="spinner" aria-hidden="true" />
              {jobStatus.status === "queued" ? "排隊中，前面的任務跑完就輪到你了..." : "處理中，請稍候..."}
            </div>
          )}

          {isDone && (
            <div className="status status-done">
              <p>完成了！</p>
              <ul className="download-list">
                {Object.entries(jobStatus.downloads).map(([type, path]) => (
                  <li key={type}>
                    <a href={downloadUrl(path)} download>
                      下載{DOWNLOAD_LABELS[type] || type}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {isError && (
            <div className="status status-error">
              <p>發生錯誤：</p>
              <pre>{jobStatus.error}</pre>
            </div>
          )}

          {(isDone || isError) && (
            <button type="button" onClick={resetJob} className="secondary-button">
              建立新任務
            </button>
          )}
        </section>
      ) : (
        <form className="card" onSubmit={handleSubmit}>
          <h2>1. 上傳投影片</h2>
          <label>
            PPTX 檔案
            <input
              type="file"
              accept=".pptx"
              onChange={(e) => setPptxFile(e.target.files[0] || null)}
            />
          </label>
          <label>
            每頁投影片的圖片（依順序全選，張數需與 PPTX 頁數相同）
            <input
              type="file"
              accept="image/*"
              multiple
              onChange={(e) => setImageFiles(Array.from(e.target.files))}
            />
          </label>
          <label>
            課程名稱（輸出檔名）
            <input
              type="text"
              value={baseName}
              onChange={(e) => setBaseName(e.target.value)}
            />
          </label>

          <h2>2. 講稿來源</h2>
          <label>
            模式
            <select value={scriptMode} onChange={(e) => setScriptMode(e.target.value)}>
              {SCRIPT_MODES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>

          {modeConfig.needsApiKey && (
            <label>
              Gemini API Key
              <input
                type="password"
                value={geminiApiKey}
                onChange={(e) => setGeminiApiKey(e.target.value)}
                placeholder="貼上你的 Gemini API Key"
              />
              <a href={GEMINI_API_KEY_URL} target="_blank" rel="noreferrer" className="hint-link">
                還沒有 Gemini API Key？點此免費申請
              </a>
            </label>
          )}

          {modeConfig.needsTexts && imageFiles.length > 0 && (
            <div className="per-slide-texts">
              {imageFiles.map((_, i) => (
                <label key={i}>
                  第 {i + 1} 頁講稿
                  <textarea
                    rows={2}
                    value={perSlideTexts[i] || ""}
                    onChange={(e) => {
                      const next = [...perSlideTexts];
                      next[i] = e.target.value;
                      setPerSlideTexts(next);
                    }}
                  />
                </label>
              ))}
            </div>
          )}

          <h2>3. 語音與轉場</h2>
          <label>
            配音語者
            <select value={voice} onChange={(e) => setVoice(e.target.value)}>
              {VOICES.map((v) => (
                <option key={v.value} value={v.value}>
                  {v.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            轉場效果
            <select value={transition} onChange={(e) => setTransition(e.target.value)}>
              {TRANSITIONS.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>

          <button
            type="button"
            className="advanced-toggle"
            onClick={() => setShowAdvanced((v) => !v)}
          >
            {showAdvanced ? "隱藏進階選項" : "顯示進階選項（畫質、Logo、背景音樂、片頭片尾）"}
          </button>

          {showAdvanced && (
            <div className="advanced-section">
              <label>
                轉場時間（毫秒）
                <input
                  type="number"
                  min="0"
                  value={transitionDurationMs}
                  onChange={(e) => setTransitionDurationMs(Number(e.target.value))}
                />
              </label>
              <label>
                解析度
                <select value={resolution} onChange={(e) => setResolution(e.target.value)}>
                  {RESOLUTIONS.map((r) => (
                    <option key={r.value} value={r.value}>
                      {r.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Logo 圖片（選填，右上角浮水印）
                <input
                  type="file"
                  accept="image/*"
                  onChange={(e) => setLogoFile(e.target.files[0] || null)}
                />
              </label>
              <label>
                背景音樂（選填）
                <input
                  type="file"
                  accept="audio/*"
                  onChange={(e) => setBgmFile(e.target.files[0] || null)}
                />
              </label>
              <label>
                片頭影片（選填）
                <input
                  type="file"
                  accept="video/*"
                  onChange={(e) => setIntroFile(e.target.files[0] || null)}
                />
              </label>
              <label>
                片尾影片（選填）
                <input
                  type="file"
                  accept="video/*"
                  onChange={(e) => setOutroFile(e.target.files[0] || null)}
                />
              </label>
            </div>
          )}

          {formError && <p className="form-error">{formError}</p>}

          <button type="submit" disabled={submitting} className="primary-button">
            {submitting ? "送出中..." : "開始製作課程影片"}
          </button>
        </form>
      )}
    </div>
  );
}

export default App;
