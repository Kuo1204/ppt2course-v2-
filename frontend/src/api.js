// Empty by default = same-origin relative requests, which is what a combined
// FastAPI-serves-the-frontend deployment needs. Dev overrides this to point
// at a separately running backend (see .env.development).
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

export async function createJob(formData) {
  const response = await fetch(`${API_BASE_URL}/api/jobs`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `建立任務失敗 (HTTP ${response.status})`);
  }

  return response.json();
}

export async function getJobStatus(jobId) {
  const response = await fetch(`${API_BASE_URL}/api/jobs/${jobId}`);
  if (!response.ok) {
    throw new Error(`查詢任務狀態失敗 (HTTP ${response.status})`);
  }
  return response.json();
}

export function downloadUrl(path) {
  return `${API_BASE_URL}${path}`;
}

export async function extractScriptText(file) {
  const form = new FormData();
  form.append("file", file);

  const response = await fetch(`${API_BASE_URL}/api/extract-script-text`, {
    method: "POST",
    body: form,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `講稿檔案解析失敗 (HTTP ${response.status})`);
  }

  return response.json();
}

export async function fetchVoicePreview(voice, rate, volume) {
  const params = new URLSearchParams();
  if (rate) params.set("rate", rate);
  if (volume) params.set("volume", volume);
  const query = params.toString() ? `?${params.toString()}` : "";

  const response = await fetch(
    `${API_BASE_URL}/api/voice-preview/${encodeURIComponent(voice)}${query}`
  );

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `語音預覽失敗 (HTTP ${response.status})`);
  }

  return response.blob();
}
