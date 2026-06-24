#!/usr/bin/env node
/**
 * Generates build/icon.png (512×512) — the LabmateMark tile geometry in PNG
 * form, built from raw pixels using only Node.js built-ins (no canvas/sharp).
 *
 * Tile: rounded rectangle, gradient #6aa6ff → #a78bfa (left→right).
 * Orbit ring: circle, stroke only (dark transparent).
 * Primary circle: large filled circle (dark, bottom-left quadrant).
 * Companion: small filled circle (accent, top-right), same hue as gradient end.
 */

import { createWriteStream } from 'fs';
import { deflateSync } from 'zlib';
import { fileURLToPath } from 'url';
import { join, dirname } from 'path';

const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'build', 'icon.png');
const S = 512; // canvas side in px

// ── Geometry (proportional to the SVG viewBox 0 0 64 64, scaled to S=512) ──
const scale = S / 64;
const rx = Math.round(0.27 * S);           // tile corner radius

const ring = { cx: 32 * scale, cy: 32 * scale, r: 20 * scale };
const primary = { cx: 26 * scale, cy: 38 * scale, r: 8 * scale };
const companion = { cx: 48 * scale, cy: 18 * scale, r: 4.8 * scale };

// ── Colour helpers ────────────────────────────────────────────────────────────
function lerp(a, b, t) { return Math.round(a + (b - a) * t); }

// Brand gradient: #6aa6ff (left) → #a78bfa (right)
const gL = [0x6a, 0xa6, 0xff]; // left colour
const gR = [0xa7, 0x8b, 0xfa]; // right colour

function gradientRgb(x) {
  const t = x / (S - 1);
  return [lerp(gL[0], gR[0], t), lerp(gL[1], gR[1], t), lerp(gL[2], gR[2], t)];
}

function inRoundRect(x, y, r) {
  // Rounded rectangle: clamp corner circles
  const cx = Math.max(r, Math.min(S - r, x));
  const cy = Math.max(r, Math.min(S - r, y));
  return (x - cx) ** 2 + (y - cy) ** 2 <= r * r;
}

function inCircle(x, y, c) { return (x - c.cx) ** 2 + (y - c.cy) ** 2 <= c.r ** 2; }

function onRing(x, y) {
  const d = Math.sqrt((x - ring.cx) ** 2 + (y - ring.cy) ** 2);
  return Math.abs(d - ring.r) < 1.8 * scale; // stroke width ~2.4 scaled
}

// ── Pixel loop ────────────────────────────────────────────────────────────────
// RGBA format: each row = [filter=0, R,G,B,A, R,G,B,A, ...]
const rows = [];
for (let y = 0; y < S; y++) {
  const row = Buffer.alloc(1 + S * 4);
  row[0] = 0; // PNG filter: None
  for (let x = 0; x < S; x++) {
    const off = 1 + x * 4;
    if (!inRoundRect(x, y, rx)) {
      // transparent outside tile
      row[off] = row[off+1] = row[off+2] = row[off+3] = 0;
      continue;
    }
    // Base: gradient fill
    const [r, g, b] = gradientRgb(x);
    let pr = r, pg = g, pb = b, pa = 255;

    // Ring overlay (semi-transparent dark)
    if (onRing(x, y)) { pr = 10; pg = 12; pb = 16; pa = 220; }

    // Primary circle (dark fill)
    if (inCircle(x, y, primary)) { pr = 10; pg = 12; pb = 16; pa = Math.round(255 * 0.92); }

    // Companion circle (accent purple #a78bfa)
    if (inCircle(x, y, companion)) { pr = 0xa7; pg = 0x8b; pb = 0xfa; pa = 255; }

    row[off] = pr; row[off+1] = pg; row[off+2] = pb; row[off+3] = pa;
  }
  rows.push(row);
}

// ── PNG encoding ──────────────────────────────────────────────────────────────
const raw = Buffer.concat(rows);
const compressed = deflateSync(raw, { level: 9 });

const table = Uint32Array.from({ length: 256 }, (_, i) => {
  let c = i;
  for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
  return c;
});
function crc32(buf) {
  let c = 0xFFFFFFFF;
  for (const b of buf) c = table[(c ^ b) & 0xFF] ^ (c >>> 8);
  return (~c) >>> 0;
}
function chunk(type, data) {
  const t = Buffer.from(type, 'ascii');
  const len = Buffer.allocUnsafe(4); len.writeUInt32BE(data.length);
  const crcBuf = Buffer.allocUnsafe(4); crcBuf.writeUInt32BE(crc32(Buffer.concat([t, data])));
  return Buffer.concat([len, t, data, crcBuf]);
}

const ihdr = Buffer.alloc(13);
ihdr.writeUInt32BE(S, 0); ihdr.writeUInt32BE(S, 4);
ihdr[8] = 8; ihdr[9] = 6; // bit depth 8, RGBA

const png = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
  chunk('IHDR', ihdr),
  chunk('IDAT', compressed),
  chunk('IEND', Buffer.alloc(0)),
]);

createWriteStream(OUT).end(png, () => console.log(`icon written → ${OUT}  (${png.length} bytes)`));
