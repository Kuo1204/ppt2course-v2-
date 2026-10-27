// Reference-pixel-size scaling helpers, ported 1:1 from frontend/src/App.jsx
// (scaledFontSize/scaledLogoWidth/scaledLogoMargin) so the same slider
// position produces the same relative on-screen size regardless of which
// output resolution is chosen, exactly like the real app's UI promises.
export function scaledFontSize(referencePx, width, height) {
  return Math.max(1, Math.round((referencePx * height) / 1080));
}

export function scaledLogoWidth(referencePx, width) {
  return Math.max(1, Math.round((referencePx * width) / 1920));
}

export function scaledLogoMargin(width, referencePx = 12) {
  return Math.max(1, Math.round((referencePx * width) / 1920));
}

export const FONT_SIZES = [
  { value: 46, label: "小" },
  { value: 64, label: "中" },
  { value: 84, label: "大" },
  { value: 106, label: "特大" },
];

export const LOGO_SIZES = [
  { value: 100, label: "小" },
  { value: 160, label: "中" },
  { value: 240, label: "大" },
  { value: 340, label: "特大" },
];

export const TRANSITIONS = [
  { value: "fade", label: "淡入淡出 (fade)" },
  { value: "dissolve", label: "溶解 (dissolve)" },
  { value: "wipeleft", label: "向左擦除 (wipeleft)" },
  { value: "wiperight", label: "向右擦除 (wiperight)" },
  { value: "slideleft", label: "向左滑動 (slideleft)" },
  { value: "slideright", label: "向右滑動 (slideright)" },
  { value: "circlecrop", label: "圓形裁切 (circlecrop)" },
];

export const RESOLUTIONS = [
  { value: "1920x1080", label: "1920 x 1080 (FHD)" },
  { value: "1280x720", label: "1280 x 720 (HD)" },
  { value: "3840x2160", label: "3840 x 2160 (4K)" },
];

export const LOGO_POSITIONS = [
  { value: "top-left", label: "左上" },
  { value: "top-right", label: "右上" },
  { value: "bottom-left", label: "左下" },
  { value: "bottom-right", label: "右下" },
];
