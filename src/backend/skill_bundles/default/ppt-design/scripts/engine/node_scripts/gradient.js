// gradient.js — dependency-free PNG gradient baker (WP9).
//
// pptxgenjs has NO native gradient fill (gradients corrupt the file), so the
// industry workaround is to bake a gradient raster and use it as a slide
// background image. This module builds a small linear-gradient PNG entirely
// from Node's built-in `zlib` — no `sharp`, no `canvas`, no new npm deps.
//
// Output is a base64 string in pptxgenjs's expected form
// ("image/png;base64,....") suitable for `slide.background = { data }` or
// `slide.addImage({ data })`.
//
// Kept deliberately small (default 360×203, 16:9): gradients are smooth, so a
// low-res raster scales up cleanly and keeps the .pptx a few KB heavier, not MB.

const zlib = require("zlib");

const CRC_TABLE = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const typeBuf = Buffer.from(type, "ascii");
  const body = Buffer.concat([typeBuf, data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body), 0);
  return Buffer.concat([len, body, crc]);
}

function hexToRgb(hex) {
  const h = String(hex || "").replace(/^#/, "");
  if (!/^[0-9A-Fa-f]{6}$/.test(h)) return [255, 255, 255];
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

function clamp8(v) { return v < 0 ? 0 : v > 255 ? 255 : Math.round(v); }

/**
 * Build a linear-gradient PNG.
 * @param {Object} opts
 *   stops: [{ pos: 0..1, color: "RRGGBB" }, ...]  (>=2)
 *   angleDeg: gradient direction in degrees (0 = left→right, 90 = top→bottom,
 *             135 = top-left → bottom-right). Default 135.
 *   w,h: pixel size (default 360×203).
 *   grain: 0..~6 subtle per-pixel dither to kill banding (default 2).
 * @returns {string} "image/png;base64,...."
 */
function gradientPng(opts = {}) {
  const w = opts.w || 360;
  const h = opts.h || 203;
  const angle = ((opts.angleDeg == null ? 135 : opts.angleDeg) * Math.PI) / 180;
  const grain = opts.grain == null ? 2 : opts.grain;
  let stops = (opts.stops || []).slice();
  if (stops.length < 2) {
    const c = stops[0] ? stops[0].color : "FFFFFF";
    stops = [{ pos: 0, color: c }, { pos: 1, color: c }];
  }
  stops.sort((a, b) => a.pos - b.pos);
  const rgbStops = stops.map(s => ({ pos: Math.max(0, Math.min(1, s.pos)), rgb: hexToRgb(s.color) }));

  // Projection axis: each pixel's position along the gradient is its dot with
  // the unit direction vector, normalized to 0..1 across the image diagonal.
  const dx = Math.cos(angle);
  const dy = Math.sin(angle);
  // range of the projection over the rectangle corners → normalize
  const projs = [
    0 * dx + 0 * dy, (w - 1) * dx + 0 * dy,
    0 * dx + (h - 1) * dy, (w - 1) * dx + (h - 1) * dy,
  ];
  const pMin = Math.min(...projs);
  const pSpan = Math.max(...projs) - pMin || 1;

  function colorAt(t) {
    if (t <= rgbStops[0].pos) return rgbStops[0].rgb;
    const last = rgbStops[rgbStops.length - 1];
    if (t >= last.pos) return last.rgb;
    for (let i = 1; i < rgbStops.length; i++) {
      if (t <= rgbStops[i].pos) {
        const a = rgbStops[i - 1], b = rgbStops[i];
        const f = (t - a.pos) / ((b.pos - a.pos) || 1);
        return [a.rgb[0] + (b.rgb[0] - a.rgb[0]) * f,
                a.rgb[1] + (b.rgb[1] - a.rgb[1]) * f,
                a.rgb[2] + (b.rgb[2] - a.rgb[2]) * f];
      }
    }
    return last.rgb;
  }

  // Raw image: each scanline prefixed by filter byte 0, then RGB triples.
  const raw = Buffer.alloc(h * (1 + w * 3));
  let seed = 0x2545f491;
  for (let y = 0; y < h; y++) {
    const rowStart = y * (1 + w * 3);
    raw[rowStart] = 0; // filter: none
    for (let x = 0; x < w; x++) {
      const t = ((x * dx + y * dy) - pMin) / pSpan;
      const c = colorAt(t);
      // cheap deterministic dither to avoid 8-bit banding on big flat ramps
      let n = 0;
      if (grain) {
        seed = (seed * 1664525 + 1013904223) & 0xffffffff;
        n = ((seed >>> 24) / 255 - 0.5) * grain;
      }
      const off = rowStart + 1 + x * 3;
      raw[off] = clamp8(c[0] + n);
      raw[off + 1] = clamp8(c[1] + n);
      raw[off + 2] = clamp8(c[2] + n);
    }
  }

  const sig = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8;   // bit depth
  ihdr[9] = 2;   // color type 2 = truecolor RGB
  ihdr[10] = 0;  // compression
  ihdr[11] = 0;  // filter
  ihdr[12] = 0;  // interlace
  const idat = zlib.deflateSync(raw, { level: 9 });
  const png = Buffer.concat([
    sig,
    chunk("IHDR", ihdr),
    chunk("IDAT", idat),
    chunk("IEND", Buffer.alloc(0)),
  ]);
  return "image/png;base64," + png.toString("base64");
}

module.exports = { gradientPng };
