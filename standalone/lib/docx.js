// Builds a slide-by-slide transcript .docx using the `docx` npm package
// loaded from jsdelivr's ESM wrapper -- fine here since this is a plain web
// page, not a sandboxed Claude Artifact (no CDN allowlist restriction
// applies to this standalone app).
export async function buildTranscriptDocx(baseName, slideTexts) {
  const docxModule = await import("https://cdn.jsdelivr.net/npm/docx@9/+esm");
  const { Document, Packer, Paragraph, TextRun, HeadingLevel } = docxModule;

  const children = [
    new Paragraph({
      text: `${baseName} - 課程講稿`,
      heading: HeadingLevel.TITLE,
    }),
  ];

  slideTexts.forEach((text, i) => {
    children.push(
      new Paragraph({
        text: `第 ${i + 1} 頁`,
        heading: HeadingLevel.HEADING_2,
        spacing: { before: 300, after: 120 },
      })
    );
    const lines = (text || "").split(/\r\n|\r|\n/);
    if (lines.every((l) => !l.trim())) {
      children.push(new Paragraph({ children: [new TextRun({ text: "（無講稿）", italics: true })] }));
    } else {
      for (const line of lines) {
        children.push(new Paragraph({ children: [new TextRun(line)] }));
      }
    }
  });

  const doc = new Document({
    sections: [{ properties: {}, children }],
  });
  const blob = await Packer.toBlob(doc);
  return blob;
}
