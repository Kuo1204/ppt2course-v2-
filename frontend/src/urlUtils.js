// Resolves a download path (which may already be an absolute dev-mode URL
// like "http://localhost:8000/...", or a same-origin relative path in the
// combined production build) into a URL a *different device* — a phone
// scanning the QR code — can actually reach. "localhost"/"127.0.0.1" only
// ever mean "this device", so a QR code encoding either is unusable from a
// phone even when the backend is reachable on the LAN; swap in whatever
// hostname the browser is currently using instead (e.g. the PC's LAN IP).
export function resolveDownloadUrl(path, currentHostname, currentOrigin) {
  try {
    const url = new URL(path, currentOrigin);
    if (url.hostname === "localhost" || url.hostname === "127.0.0.1") {
      url.hostname = currentHostname;
    }
    return url.href;
  } catch {
    return path;
  }
}
