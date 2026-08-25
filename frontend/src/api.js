// Empty by default = same-origin relative requests, which is what a combined
// FastAPI-serves-the-frontend deployment needs. Dev overrides this to point
// at a separately running backend (see .env.development).
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

// The citizen-broker platform mounts each service under a per-account path
// prefix (e.g. https://citizen01-prod.wtmec.com/<account>/<project>/) and
// strips that prefix before forwarding to this container. A request built
// from an absolute path like "/api/jobs" skips straight past that prefix and
// hits the platform's own domain root instead of this service -- silently
// (fetch still "succeeds", it just talks to the wrong place). Building every
// URL with a *relative* path (no leading slash) instead lets the browser
// resolve it against the page's own URL, prefix included. That only applies
// to the same-origin (prod) case; when API_BASE_URL points at a separate
// origin (local dev), the path needs its own leading slash again.
function apiUrl(path) {
  return API_BASE_URL ? `${API_BASE_URL}/${path}` : path;
}

// The public deployment's ngrok free-tier domain shows visitors an
// interstitial "You are about to visit..." warning page before the first
// request each browser makes — normally cleared by clicking through once,
// but this header skips it outright. Harmless to send everywhere (local
// dev, Cloudflare, anything else just ignores an unknown header), and
// guards every one of *our own* fetch calls against ever receiving that
// warning's HTML back instead of the JSON they expect.
const NGROK_BYPASS_HEADERS = { "ngrok-skip-browser-warning": "true" };

export async function createJob(formData) {
  const response = await fetch(apiUrl("api/jobs"), {
    method: "POST",
    headers: NGROK_BYPASS_HEADERS,
    body: formData,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `建立任務失敗 (HTTP ${response.status})`);
  }

  return response.json();
}

export async function getJobStatus(jobId) {
  const response = await fetch(apiUrl(`api/jobs/${jobId}`), {
    headers: NGROK_BYPASS_HEADERS,
  });
  if (!response.ok) {
    throw new Error(`查詢任務狀態失敗 (HTTP ${response.status})`);
  }
  return response.json();
}

export function downloadUrl(path) {
  // `path` comes straight from the backend's job-status response (e.g.
  // "api/jobs/{id}/download/mp4", relative on purpose -- see apiUrl above).
  return apiUrl(path);
}

export async function extractScriptText(file) {
  const form = new FormData();
  form.append("file", file);

  const response = await fetch(apiUrl("api/extract-script-text"), {
    method: "POST",
    headers: NGROK_BYPASS_HEADERS,
    body: form,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `講稿檔案解析失敗 (HTTP ${response.status})`);
  }

  return response.json();
}

export async function fetchPptxPreview(pptxFile) {
  const form = new FormData();
  form.append("pptx", pptxFile);

  const response = await fetch(apiUrl("api/pptx-preview"), {
    method: "POST",
    headers: NGROK_BYPASS_HEADERS,
    body: form,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `投影片預覽失敗 (HTTP ${response.status})`);
  }

  return response.json();
}

export async function fetchPptxNotes(pptxFile) {
  const form = new FormData();
  form.append("pptx", pptxFile);

  const response = await fetch(apiUrl("api/pptx-notes"), {
    method: "POST",
    headers: NGROK_BYPASS_HEADERS,
    body: form,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `備忘稿預覽失敗 (HTTP ${response.status})`);
  }

  return response.json();
}

export async function generateScriptPreview({ pptxFile, scriptMode, geminiApiKey, geminiModel, texts }) {
  const form = new FormData();
  form.append("pptx", pptxFile);
  form.append("script_mode", scriptMode);
  form.append("gemini_api_key", geminiApiKey);
  if (geminiModel) form.append("gemini_model", geminiModel);
  if (texts) form.append("texts", JSON.stringify(texts));

  const response = await fetch(apiUrl("api/generate-script"), {
    method: "POST",
    headers: NGROK_BYPASS_HEADERS,
    body: form,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `講稿生成失敗 (HTTP ${response.status})`);
  }

  return response.json();
}

export async function analyzeVisuals({ pptxFile, texts, voice, voiceRate, voiceVolume, geminiApiKey, geminiModel }) {
  const form = new FormData();
  form.append("pptx", pptxFile);
  form.append("texts", JSON.stringify(texts));
  form.append("voice", voice);
  if (voiceRate) form.append("voice_rate", voiceRate);
  if (voiceVolume) form.append("voice_volume", voiceVolume);
  if (geminiApiKey) form.append("gemini_api_key", geminiApiKey);
  if (geminiModel) form.append("gemini_model", geminiModel);

  const response = await fetch(apiUrl("api/analyze-visuals"), {
    method: "POST",
    headers: NGROK_BYPASS_HEADERS,
    body: form,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `視覺素材分析失敗 (HTTP ${response.status})`);
  }

  return response.json();
}

export async function searchMedia({ keyword, slideNumber, mediaType = "image", pexelsApiKey, limit }) {
  const params = new URLSearchParams({
    keyword,
    slide_number: String(slideNumber),
    media_type: mediaType,
  });
  if (pexelsApiKey) params.set("pexels_api_key", pexelsApiKey);
  if (limit) params.set("limit", String(limit));

  const response = await fetch(apiUrl(`api/media-search?${params.toString()}`), {
    headers: NGROK_BYPASS_HEADERS,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `素材搜尋失敗 (HTTP ${response.status})`);
  }

  return response.json();
}

export async function fetchVoicePreview(voice, rate, volume) {
  const params = new URLSearchParams();
  if (rate) params.set("rate", rate);
  if (volume) params.set("volume", volume);
  const query = params.toString() ? `?${params.toString()}` : "";

  const response = await fetch(
    apiUrl(`api/voice-preview/${encodeURIComponent(voice)}${query}`),
    { headers: NGROK_BYPASS_HEADERS }
  );

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `語音預覽失敗 (HTTP ${response.status})`);
  }

  return response.blob();
}
