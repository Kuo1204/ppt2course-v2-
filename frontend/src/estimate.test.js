import { describe, expect, it, vi } from "vitest";
import {
  estimateGenerationSeconds,
  estimateRemainingSeconds,
  formatEstimatedDuration,
} from "./estimate";

describe("estimateGenerationSeconds", () => {
  it("returns null when there are no slides yet", () => {
    expect(estimateGenerationSeconds({ slideCount: 0 })).toBeNull();
    expect(estimateGenerationSeconds({})).toBeNull();
  });

  it("scales with slide count", () => {
    const five = estimateGenerationSeconds({ slideCount: 5 });
    const ten = estimateGenerationSeconds({ slideCount: 10 });
    expect(ten).toBeGreaterThan(five);
  });

  it("estimates more for a higher output resolution", () => {
    const hd = estimateGenerationSeconds({ slideCount: 10, resolution: "1920x1080" });
    const fourK = estimateGenerationSeconds({ slideCount: 10, resolution: "3840x2160" });
    expect(fourK).toBeGreaterThan(hd);
  });

  it("adds extra time for AI script generation", () => {
    const withoutAi = estimateGenerationSeconds({ slideCount: 5, usesAiGeneration: false });
    const withAi = estimateGenerationSeconds({ slideCount: 5, usesAiGeneration: true });
    expect(withAi).toBeGreaterThan(withoutAi);
  });

  it("adds extra time for logo/bgm/intro-outro extras", () => {
    const base = estimateGenerationSeconds({ slideCount: 5 });
    const withExtras = estimateGenerationSeconds({
      slideCount: 5,
      hasLogo: true,
      hasBgm: true,
      hasIntroOrOutro: true,
    });
    expect(withExtras).toBeGreaterThan(base);
  });

  it("an unknown resolution value falls back to the 1080p multiplier instead of throwing", () => {
    expect(() => estimateGenerationSeconds({ slideCount: 5, resolution: "9999x9999" })).not.toThrow();
  });
});

describe("formatEstimatedDuration", () => {
  it("returns null for a null input", () => {
    expect(formatEstimatedDuration(null)).toBeNull();
  });

  it("rounds down to '不到 1 分鐘' well under a minute", () => {
    expect(formatEstimatedDuration(20)).toBe("不到 1 分鐘");
  });

  it("uses singular phrasing for exactly one minute", () => {
    expect(formatEstimatedDuration(75)).toBe("約 1 分鐘");
  });

  it("rounds to the nearest whole minute otherwise", () => {
    expect(formatEstimatedDuration(150)).toBe("約 3 分鐘"); // 2.5 min rounds up
    expect(formatEstimatedDuration(170)).toBe("約 3 分鐘");
  });
});

describe("estimateRemainingSeconds", () => {
  it("returns null when there's nothing to estimate from", () => {
    expect(estimateRemainingSeconds(null, Date.now())).toBeNull();
    expect(estimateRemainingSeconds(120, null)).toBeNull();
  });

  it("counts down as time passes since the job started", () => {
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now + 30_000); // 30s elapsed
    const remaining = estimateRemainingSeconds(120, now);
    expect(remaining).toBeCloseTo(90, -1);
    vi.restoreAllMocks();
  });

  it("returns null once the job has run past the original estimate", () => {
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now + 200_000); // way past a 120s estimate
    expect(estimateRemainingSeconds(120, now)).toBeNull();
    vi.restoreAllMocks();
  });
});
