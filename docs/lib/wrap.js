// Client-side subtitle line-wrapping that keeps every term listed in the
// "自訂詞庫" (custom dictionary) textarea intact instead of letting ffmpeg's
// drawtext box wrap break a term across two lines mid-word. Real Chinese
// word segmentation (jieba etc.) isn't available client-side in a
// zero-backend static page, so this uses a much simpler heuristic: treat
// every listed dictionary term as one atomic "token" (never split), and
// every other character as its own one-character token, then greedily pack
// tokens onto lines up to maxCharsPerLine. This is an intentional
// simplification vs. the real backend's wordseg.py — see BUILD_NOTES.md.

// Splits `text` into tokens where each occurrence of a dictionary term is
// one token (so it can never be broken across a line), and every other
// character is its own token.
function tokenize(text, terms) {
  const sortedTerms = [...terms].filter(Boolean).sort((a, b) => b.length - a.length);
  const tokens = [];
  let i = 0;
  outer: while (i < text.length) {
    for (const term of sortedTerms) {
      if (term.length > 0 && text.startsWith(term, i)) {
        tokens.push(term);
        i += term.length;
        continue outer;
      }
    }
    tokens.push(text[i]);
    i += 1;
  }
  return tokens;
}

// Parses the custom-dictionary textarea (one term per line, blank lines and
// whitespace ignored) into a plain array of terms.
export function parseDictionary(raw) {
  if (!raw) return [];
  return raw
    .split(/\r\n|\r|\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

// Greedily wraps `text` into lines of at most `maxCharsPerLine` visual
// units (CJK characters count as 1 unit each here, which is good enough for
// a monospace-ish CJK subtitle font). A token longer than maxCharsPerLine
// (e.g. a very long dictionary term) is still kept whole on its own line
// rather than being cut, so "never split a listed term" always wins over
// the max-width target.
export function wrapWithDictionary(text, maxCharsPerLine, terms) {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (!normalized) return [];
  const tokens = tokenize(normalized, terms);
  const lines = [];
  let current = "";
  for (const token of tokens) {
    if (current.length > 0 && current.length + token.length > maxCharsPerLine) {
      lines.push(current);
      current = token;
    } else {
      current += token;
    }
  }
  if (current) lines.push(current);
  return lines;
}
