const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

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
