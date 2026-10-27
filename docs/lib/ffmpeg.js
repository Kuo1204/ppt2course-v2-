// ffmpeg.wasm loader. See BUILD_NOTES.md / the task brief for why this
// exact shape matters -- two different silent-hang bugs were found and
// fixed during the spike this file is built from:
//
//   1. Importing FFmpeg from a CDN and passing a toBlobURL()-wrapped CDN
//      URL as classWorkerURL hangs forever with zero error, because
//      worker.js is itself an ES module with relative imports that can't
//      resolve from a blob: URL. Fix: vendor the three @ffmpeg/* packages
//      same-origin (see standalone/vendor/).
//   2. Even same-origin, passing *relative* path strings to ffmpeg.load()
//      hangs forever with zero error, because the library's internal
//      `new Worker(classWorkerURL)` resolves that relative string against
//      the *library module's own URL*, not the page's URL -- silently
//      doubling the path. Fix: always convert to an absolute URL first,
//      resolved against the real page location (so it still works when
//      GitHub Pages serves this under a sub-path like
//      https://user.github.io/repo/).
import { FFmpeg } from "../vendor/ffmpeg/index.js";
import { fetchFile } from "../vendor/util/index.js";

const abs = (p) => new URL(p, document.baseURI).href;

let ffmpegPromise = null;

export async function getFFmpeg(onLog) {
  if (!ffmpegPromise) {
    ffmpegPromise = (async () => {
      const ffmpeg = new FFmpeg();
      if (onLog) {
        ffmpeg.on("log", ({ message }) => onLog(message));
      }
      // Resolved against document.baseURI (the real page location), NOT
      // against this module's own URL -- see bug #2 above. standalone/'s
      // index.html lives next to vendor/, so these are page-relative paths.
      await ffmpeg.load({
        coreURL: abs("./vendor/core/ffmpeg-core.js"),
        wasmURL: abs("./vendor/core/ffmpeg-core.wasm"),
        classWorkerURL: abs("./vendor/ffmpeg/worker.js"),
      });
      return ffmpeg;
    })();
  }
  return ffmpegPromise;
}

export { fetchFile };
