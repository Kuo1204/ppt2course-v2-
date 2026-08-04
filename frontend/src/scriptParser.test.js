import { describe, expect, it } from "vitest";
import { parseNumberedScript } from "./scriptParser";

describe("parseNumberedScript", () => {
  it("splits text marked with Arabic-numeral page markers", () => {
    const raw = "第1頁\n大家好，歡迎收看。\n第2頁\n這是第二頁的內容。";
    const result = parseNumberedScript(raw, 2);
    expect(result.error).toBeUndefined();
    expect(result.texts).toEqual(["大家好，歡迎收看。", "這是第二頁的內容。"]);
  });

  it("splits text marked with Chinese-numeral page markers", () => {
    const raw = "第一頁\n開場白。\n第二頁\n結尾。";
    const result = parseNumberedScript(raw, 2);
    expect(result.texts).toEqual(["開場白。", "結尾。"]);
  });

  it("converts Chinese numerals correctly, including past ten", () => {
    const cases = [
      ["十", 10],
      ["十一", 11],
      ["二十", 20],
      ["二十一", 21],
      ["一百二十三", 123],
    ];
    for (const [numeral, page] of cases) {
      const markers = Array.from({ length: page }, (_, i) => `第${i + 1}頁\nS${i + 1}`).join("\n");
      // sanity check: our generated marker for the target page uses Arabic digits;
      // separately confirm the Chinese-numeral spelling resolves to the same page.
      const chineseOnly = `第${numeral}頁\nCHINESE`;
      const combined = `${markers}\n`.replace(`第${page}頁\nS${page}`, chineseOnly);
      const result = parseNumberedScript(combined, page);
      expect(result.error).toBeUndefined();
      expect(result.texts[page - 1]).toEqual("CHINESE");
    }
  });

  it("accepts content on the same line as the marker", () => {
    const raw = "第1頁：大家好\n第2頁：謝謝收看";
    const result = parseNumberedScript(raw, 2);
    expect(result.texts).toEqual(["大家好", "謝謝收看"]);
  });

  it("joins multiple lines under the same marker into one script", () => {
    const raw = "第1頁\n第一行。\n第二行。\n第2頁\n下一頁。";
    const result = parseNumberedScript(raw, 2);
    expect(result.texts).toEqual(["第一行。\n第二行。", "下一頁。"]);
  });

  it("ignores preamble text before the first marker", () => {
    const raw = "這是一段筆記，跟講稿無關\n第1頁\n真正的講稿";
    const result = parseNumberedScript(raw, 1);
    expect(result.texts).toEqual(["真正的講稿"]);
  });

  it("errors when no markers are found at all", () => {
    const result = parseNumberedScript("完全沒有標記的一段文字", 3);
    expect(result.texts).toBeUndefined();
    expect(result.error).toMatch(/找不到/);
  });

  it("errors when detected marker count is fewer than the slide count", () => {
    const raw = "第1頁\nA\n第2頁\nB";
    const result = parseNumberedScript(raw, 3);
    expect(result.error).toMatch(/2.*3|3.*2/);
  });

  it("errors when detected marker count exceeds the slide count", () => {
    const raw = "第1頁\nA\n第2頁\nB\n第3頁\nC";
    const result = parseNumberedScript(raw, 2);
    expect(result.error).toBeDefined();
  });

  it("errors when a page number is duplicated", () => {
    const raw = "第1頁\nA\n第1頁\nB";
    const result = parseNumberedScript(raw, 2);
    expect(result.error).toMatch(/重複/);
  });

  it("errors when markers are non-sequential even if the count matches", () => {
    // 5 markers, but page 3 is missing and page 6 is out of range
    const raw = "第1頁\nA\n第2頁\nB\n第4頁\nC\n第5頁\nD\n第6頁\nE";
    const result = parseNumberedScript(raw, 5);
    expect(result.error).toBeDefined();
  });

  it("trims whitespace around each slide's script", () => {
    const raw = "第1頁\n   有前後空白的講稿   \n第2頁\n乾淨的講稿";
    const result = parseNumberedScript(raw, 2);
    expect(result.texts).toEqual(["有前後空白的講稿", "乾淨的講稿"]);
  });
});
