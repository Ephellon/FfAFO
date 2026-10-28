/** Browser-side audio fingerprinting; mirrors audio_sig.py algorithm asfp-v1. */
export const ALPHABET = "346789ABCDEFGHJKLMNPQRTUVWXYabcdefghijkmnpqrtwxyz";
export const SIGNATURE_SEPARATOR = "_"; // not in ALPHABET, so a signature always splits unambiguously
export const ALGORITHM_VERSION = "asfp-v1";
export const TARGET_RATE = 11025;
const BASE = ALPHABET.length;
const FRAME = 2048, HOP = 512, SEGMENTS = 6, BANDS = 29, FP_BYTES = 21, ROOT_BYTES = 20;

export function encodeBytes(bytes) {
  let n = 0n;
  for (const b of bytes) n = (n << 8n) | BigInt(b);
  const width = Math.ceil(bytes.length * 8 / Math.log2(BASE));
  const chars = Array(width);
  for (let i = width - 1; i >= 0; i--) {
    chars[i] = ALPHABET[Number(n % BigInt(BASE))];
    n /= BigInt(BASE);
  }
  if (n !== 0n) throw new Error("Alphabet width cannot represent this byte sequence");
  return chars.join("");
}

export function decodeBytes(text, size) {
  const width = Math.ceil(size * 8 / Math.log2(BASE));
  if (text.length !== width) throw new Error(`Expected ${width} alphabet characters`);
  let n = 0n;
  for (const ch of text) {
    const digit = ALPHABET.indexOf(ch);
    if (digit < 0) throw new Error(`Invalid alphabet character: ${ch}`);
    n = n * BigInt(BASE) + BigInt(digit);
  }
  if (n >= (1n << BigInt(size * 8))) throw new Error("Encoded value exceeds requested byte length");
  const out = new Uint8Array(size);
  for (let i = size - 1; i >= 0; i--) { out[i] = Number(n & 255n); n >>= 8n; }
  return out;
}

export function encodeSignature(perceptual, root) {
  if (perceptual.length !== FP_BYTES || root.length !== ROOT_BYTES) throw new Error("Expected 21-byte fingerprint and 20-byte root");
  return `${encodeBytes(perceptual)}${SIGNATURE_SEPARATOR}${encodeBytes(root)}`;
}

function bandEdges(sampleRate) {
  const low = 80, high = Math.min(5000, sampleRate / 2 * 0.95);
  const edges = [];
  for (let i = 0; i <= BANDS; i++) {
    const hz = low * (high / low) ** (i / BANDS);
    edges.push(Math.max(1, Math.min(FRAME / 2, Math.round(hz * FRAME / sampleRate))));
  }
  for (let i = 1; i < edges.length; i++) edges[i] = Math.max(edges[i], edges[i - 1] + 1);
  edges[edges.length - 1] = Math.min(edges[edges.length - 1], FRAME / 2);
  return edges;
}

function powerSpectrum(input) {
  const n = input.length, re = input.slice(), im = new Float64Array(n);
  let j = 0;
  for (let i = 1; i < n; i++) {
    let bit = n >> 1;
    while (j & bit) { j ^= bit; bit >>= 1; }
    j ^= bit;
    if (i < j) { const t = re[i]; re[i] = re[j]; re[j] = t; }
  }
  for (let size = 2; size <= n; size *= 2) {
    const half = size >> 1, angle = -2 * Math.PI / size, wmr = Math.cos(angle), wmi = Math.sin(angle);
    for (let start = 0; start < n; start += size) {
      let wr = 1, wi = 0;
      for (let k = 0; k < half; k++) {
        const even = start + k, odd = even + half;
        const tr = wr * re[odd] - wi * im[odd], ti = wr * im[odd] + wi * re[odd];
        re[odd] = re[even] - tr; im[odd] = im[even] - ti;
        re[even] += tr; im[even] += ti;
        const nextWr = wr * wmr - wi * wmi;
        wi = wr * wmi + wi * wmr; wr = nextWr;
      }
    }
  }
  const power = new Float64Array(n / 2 + 1);
  for (let i = 0; i < power.length; i++) power[i] = re[i] * re[i] + im[i] * im[i];
  return power;
}

/** Fingerprint mono PCM resampled to 11025 Hz. Returns 21 bytes. */
export function fingerprint(samples, sampleRate = TARGET_RATE) {
  if (sampleRate !== TARGET_RATE) throw new Error(`PCM must be resampled to ${TARGET_RATE} Hz`);
  if (samples.length < FRAME) throw new Error(`Audio must be at least ${(FRAME / TARGET_RATE).toFixed(3)} seconds`);
  let sumSquares = 0;
  for (const x of samples) sumSquares += x * x;
  const rms = Math.sqrt(sumSquares / samples.length);
  if (!Number.isFinite(rms) || rms < 1e-5) throw new Error("Audio is silent or too quiet to fingerprint");

  const edges = bandEdges(sampleRate);
  const sums = Array.from({ length: SEGMENTS }, () => new Float64Array(BANDS));
  const counts = new Uint32Array(SEGMENTS);
  const window = new Float64Array(FRAME);
  for (let i = 0; i < FRAME; i++) window[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (FRAME - 1));

  for (let start = 0; start + FRAME <= samples.length; start += HOP) {
    const seg = Math.min(SEGMENTS - 1, Math.floor(((start + FRAME / 2) / samples.length) * SEGMENTS));
    const frame = new Float64Array(FRAME);
    for (let i = 0; i < FRAME; i++) frame[i] = samples[start + i] * window[i];
    const power = powerSpectrum(frame);
    for (let b = 0; b < BANDS; b++) {
      let energy = 0;
      for (let k = edges[b]; k < edges[b + 1]; k++) energy += power[k];
      sums[seg][b] += Math.log1p(energy);
    }
    counts[seg]++;
  }
  const features = sums.map((bands, seg) => Array.from(bands, v => v / Math.max(1, counts[seg])));
  const out = new Uint8Array(FP_BYTES);
  let bitIndex = 0;
  for (let seg = 0; seg < SEGMENTS; seg++) {
    for (let b = 0; b < BANDS - 1; b++, bitIndex++) {
      if (features[seg][b] > features[seg][b + 1]) out[bitIndex >> 3] |= 1 << (7 - (bitIndex & 7));
    }
  }
  return out;
}

/** Decode through Web Audio, then linearly resample to the shared target rate. */
export async function decodeFile(file, audioContext = null) {
  const ownsContext = !audioContext;
  const context = audioContext || new AudioContext();
  try {
    const buffer = await context.decodeAudioData(await file.arrayBuffer());
    const mono = new Float32Array(buffer.length);
    const channels = Array.from({ length: buffer.numberOfChannels }, (_, i) => buffer.getChannelData(i));
    // Average channels equally so stereo placement doesn't alter the fingerprint.
    for (const channel of channels) for (let i = 0; i < mono.length; i++) mono[i] += channel[i] / channels.length;
    const outLength = Math.max(1, Math.round(mono.length * TARGET_RATE / buffer.sampleRate));
    const resampled = new Float64Array(outLength);
    const ratio = buffer.sampleRate / TARGET_RATE;
    for (let i = 0; i < outLength; i++) {
      const pos = i * ratio, left = Math.min(mono.length - 1, Math.floor(pos)), right = Math.min(mono.length - 1, left + 1);
      const frac = pos - left;
      resampled[i] = mono[left] * (1 - frac) + mono[right] * frac;
    }
    return { samples: resampled, duration: buffer.duration, rms: Math.sqrt(resampled.reduce((s, x) => s + x*x, 0) / resampled.length) };
  } finally {
    if (ownsContext) await context.close();
  }
}

/** Fingerprint a File and ask the registry to resolve its canonical root. */
export async function resolveFile(file, registryUrl, { fetchImpl = fetch, headers = {} } = {}) {
  const decoded = await decodeFile(file);
  const perceptual = encodeBytes(fingerprint(decoded.samples));
  const response = await fetchImpl(`${registryUrl.replace(/\/$/, "")}/fingerprints/resolve`, {
    method: "POST", headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify({ perceptual, algorithm: ALGORITHM_VERSION, duration: decoded.duration, rms: decoded.rms, source: file.name })
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Registry request failed (${response.status})`);
  return body;
}
