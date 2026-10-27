// Offline-after-first-load Chinese TTS, running entirely in the browser via
// @huggingface/transformers (transformers.js) + the BricksDisplay/vits-cmn
// VITS model. This gives exactly ONE fixed voice -- an accepted scope
// trade-off vs. the real backend's 5-voice Edge-TTS picker (see
// BUILD_NOTES.md) -- but needs zero server and zero intranet access; the
// model weights (~tens of MB) download once from the public Hugging Face
// Hub CDN and are cached by the browser afterwards.
//
// CRITICAL: vits-cmn is trained on PINYIN, not raw Hanzi. Feeding it raw
// Chinese text crashes ONNX Runtime with an opaque "Tensor shape.Size()
// must be >= 0" error with no useful stack trace -- do not skip the pinyin
// conversion step below.
import { pinyin } from "https://cdn.jsdelivr.net/npm/pinyin-pro@3/+esm";
import { floatTo16BitWav } from "./wav.js";

// A short run of silence, used instead of calling the model at all for a
// blank/whitespace-only/non-Chinese slide script. Earlier testing found
// that feeding vits-cmn a punctuation-only fallback (e.g. a single "，")
// still crashes ONNX Runtime with the same opaque "Tensor shape.Size()
// must be >= 0" error the raw-Hanzi bug produces -- pinyin-pro converts
// punctuation to nothing, so the model ends up fed a zero-length token
// sequence either way. Skipping the model call entirely for blank input
// sidesteps that crash rather than trying to find a "safe" placeholder
// string.
const SILENT_SLIDE_MS = 800;
function silentWav() {
  const sampleRate = 16000;
  const samples = new Float32Array(Math.round((SILENT_SLIDE_MS / 1000) * sampleRate));
  return { wavBytes: floatTo16BitWav(samples, sampleRate), durationMs: SILENT_SLIDE_MS, samplingRate: sampleRate };
}

let pipelinePromise = null;

async function getSynthesizer(onProgress) {
  if (!pipelinePromise) {
    pipelinePromise = (async () => {
      const { pipeline } = await import(
        "https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.2.0"
      );
      return pipeline("text-to-speech", "BricksDisplay/vits-cmn", {
        dtype: "fp32",
        progress_callback: onProgress,
      });
    })();
  }
  return pipelinePromise;
}

// Converts `text` (Traditional/Simplified Chinese, punctuation is fine) to a
// 16-bit PCM mono WAV Uint8Array plus its raw duration in milliseconds
// (before any rate/atempo adjustment, which is applied later at the ffmpeg
// assembly stage -- see pipeline.js).
export async function synthesizeToWav(text, onProgress) {
  const trimmed = (text || "").trim();
  if (!trimmed) return silentWav();

  const pinyinText = pinyin(trimmed, { toneType: "symbol", type: "string" }).trim();
  // If the slide's text converts to no pinyin at all (e.g. it was only
  // punctuation/emoji/Latin text pinyin-pro doesn't transliterate), calling
  // the model with an effectively-empty token sequence hits the same
  // crash -- fall back to silence instead of crashing the whole job.
  if (!pinyinText) return silentWav();

  const synthesizer = await getSynthesizer(onProgress);
  const output = await synthesizer(pinyinText);
  const { audio, sampling_rate: samplingRate } = output;
  const wavBytes = floatTo16BitWav(audio, samplingRate);
  const durationMs = Math.round((audio.length / samplingRate) * 1000);
  return { wavBytes, durationMs, samplingRate };
}
