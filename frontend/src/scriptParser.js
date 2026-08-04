// Parses a single pasted block of narration marked with page headers like
// "第1頁" or "第一頁" into one script per slide, mirroring how the user
// wrote scripts in their earlier Colab prototype (one big paste instead of
// a textarea per slide).

const CHINESE_DIGITS = { 零: 0, 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
const CHINESE_UNITS = { 十: 10, 百: 100, 千: 1000, 萬: 10000 };

const MARKER_PATTERN =
  /^[ \t]*第[ \t]*([0-9]+|[零一二三四五六七八九十百千萬]+)[ \t]*頁[ \t]*[:：、,，]?[ \t]*/;

function chineseNumeralToNumber(text) {
  if (/^[0-9]+$/.test(text)) return parseInt(text, 10);

  let result = 0;
  let section = 0;
  let number = 0;

  for (const ch of text) {
    if (ch in CHINESE_DIGITS) {
      number = CHINESE_DIGITS[ch];
    } else if (ch in CHINESE_UNITS) {
      const unit = CHINESE_UNITS[ch];
      if (unit === 10000) {
        section = (section + number) * unit;
        result += section;
        section = 0;
      } else {
        section += (number === 0 ? 1 : number) * unit;
      }
      number = 0;
    }
  }

  return result + section + number;
}

export function parseNumberedScript(rawText, expectedSlideCount) {
  const lines = rawText.split(/\r\n|\r|\n/);
  const sections = [];
  let current = null;

  for (const line of lines) {
    const match = line.match(MARKER_PATTERN);
    if (match) {
      current = { page: chineseNumeralToNumber(match[1]), lines: [] };
      const rest = line.slice(match[0].length);
      if (rest.trim()) current.lines.push(rest);
      sections.push(current);
    } else if (current) {
      current.lines.push(line);
    }
  }

  if (sections.length === 0) {
    return {
      error: "找不到任何頁碼標記，請用「第1頁」或「第一頁」這樣的格式標示每一頁講稿。",
    };
  }

  const byPage = new Map();
  for (const section of sections) {
    if (byPage.has(section.page)) {
      return { error: `頁碼標記重複：第 ${section.page} 頁出現了不只一次。` };
    }
    byPage.set(section.page, section.lines.join("\n").trim());
  }

  if (byPage.size !== expectedSlideCount) {
    return {
      error: `偵測到 ${byPage.size} 個頁碼標記，但投影片共有 ${expectedSlideCount} 頁，請修正後再試一次。`,
    };
  }

  const maxPage = Math.max(...byPage.keys());
  if (maxPage !== expectedSlideCount) {
    return {
      error: `頁碼標記應為第 1 到第 ${expectedSlideCount} 頁，但偵測到最大頁碼是第 ${maxPage} 頁，請確認頁碼是否正確。`,
    };
  }

  const texts = [];
  for (let page = 1; page <= expectedSlideCount; page += 1) {
    texts.push(byPage.get(page) || "");
  }
  return { texts };
}
