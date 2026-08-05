import { describe, expect, it } from "vitest";
import { resolveDownloadUrl } from "./urlUtils";

describe("resolveDownloadUrl", () => {
  it("resolves a relative path against the current page origin", () => {
    const result = resolveDownloadUrl(
      "/api/jobs/abc/download/mp4",
      "192.168.1.23",
      "http://192.168.1.23:5173"
    );
    expect(result).toBe("http://192.168.1.23:5173/api/jobs/abc/download/mp4");
  });

  it("swaps a baked-in localhost hostname for the browser's current LAN hostname", () => {
    // dev mode bakes the backend's own absolute origin (localhost:8000) into
    // the path; a phone on the same wifi can never resolve "localhost".
    const result = resolveDownloadUrl(
      "http://localhost:8000/api/jobs/abc/download/mp4",
      "192.168.1.23",
      "http://192.168.1.23:5173"
    );
    expect(result).toBe("http://192.168.1.23:8000/api/jobs/abc/download/mp4");
  });

  it("swaps a baked-in 127.0.0.1 hostname the same way", () => {
    const result = resolveDownloadUrl(
      "http://127.0.0.1:8000/api/jobs/abc/download/mp4",
      "192.168.1.23",
      "http://192.168.1.23:5173"
    );
    expect(result).toBe("http://192.168.1.23:8000/api/jobs/abc/download/mp4");
  });

  it("leaves an already-LAN-reachable absolute URL untouched", () => {
    const result = resolveDownloadUrl(
      "http://192.168.1.23:8000/api/jobs/abc/download/mp4",
      "192.168.1.23",
      "http://192.168.1.23:5173"
    );
    expect(result).toBe("http://192.168.1.23:8000/api/jobs/abc/download/mp4");
  });

  it("falls back to returning the raw path if URL construction fails", () => {
    const result = resolveDownloadUrl(":::not a url:::", "192.168.1.23", undefined);
    expect(result).toBe(":::not a url:::");
  });
});
