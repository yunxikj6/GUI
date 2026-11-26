// Replay original application SkPictures with the viewer's pinned CanvasKit.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const zlib = require('node:zlib');

const frameLimit = (32 << 20) + 12;

function draw(kit, bytes) {
  if (bytes.length < 22 || bytes.toString('ascii', 0, 4) !== 'CLRP' || bytes[4] !== 1 || bytes[5] !== 1 ||
      bytes.readUInt16BE(6) || bytes.readUInt32BE(8) !== bytes.length - 12) throw Error('invalid Frame');
  const width = bytes.readUInt16BE(16), height = bytes.readUInt16BE(18), count = bytes.readUInt16BE(20);
  if (!width || !height || width * height * 4 > 32 << 20 || !count || count > 128) throw Error('invalid dimensions');
  const surface = kit.MakeSurface(width, height);
  if (!surface) throw Error('unavailable graphics surface');
  try {
    const canvas = surface.getCanvas();
    canvas.clear(kit.TRANSPARENT);
    let offset = 22;
    for (let i = 0; i < count; i++) {
      const x = bytes.readFloatBE(offset), y = bytes.readFloatBE(offset + 4);
      const w = bytes.readFloatBE(offset + 8), h = bytes.readFloatBE(offset + 12);
      const size = bytes.readUInt32BE(offset + 16);
      offset += 20;
      if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0 ||
          size > (16 << 20) || offset + size > bytes.length) throw Error('invalid picture');
      const picture = kit.MakePicture(bytes.subarray(offset, offset + size));
      if (!picture) throw Error('unsupported original SkPicture');
      try {
        const [l, t, r, b] = picture.cullRect();
        if (![l, t, r, b].every(Number.isFinite) || r <= l || b <= t) throw Error('invalid picture bounds');
        canvas.save();
        canvas.translate(x, y);
        canvas.clipRect([0, 0, w, h], kit.ClipOp.Intersect, false);
        canvas.scale(w / (r - l), h / (b - t));
        canvas.translate(-l, -t);
        canvas.drawPicture(picture);
        canvas.restore();
      } finally {
        picture.delete();
      }
      offset += size;
    }
    if (offset !== bytes.length) throw Error('trailing bytes');
    surface.flush();
    const image = surface.makeImageSnapshot();
    if (!image) throw Error('graphics snapshot failed');
    try {
      const pixels = image.readPixels(0, 0, {width, height, colorType: kit.ColorType.RGBA_8888,
        alphaType: kit.AlphaType.Unpremul, colorSpace: kit.ColorSpace.SRGB});
      if (!pixels || pixels.length !== width * height * 4) throw Error('incomplete local render');
      return pixels;
    } finally {
      image.delete();
    }
  } finally {
    surface.dispose();
  }
}

function readExact(buffer, offset, allowEOF) {
  const start = offset;
  while (offset < buffer.length) {
    const received = fs.readSync(0, buffer, offset, buffer.length - offset);
    if (!received) {
      if (allowEOF && offset === start) return false;
      throw Error('truncated original drawing');
    }
    offset += received;
  }
  return true;
}

function paintPipe(kit) {
  const header = Buffer.alloc(12);
  while (readExact(header, 0, true)) {
    const size = 12 + header.readUInt32BE(8);
    if (size > frameLimit) throw Error('frame too large');
    const bytes = Buffer.allocUnsafe(size);
    header.copy(bytes);
    readExact(bytes, 12, false);
    const pixels = draw(kit, bytes);
    for (let offset = 0; offset < pixels.length;) {
      const written = fs.writeSync(1, pixels, offset, pixels.length - offset);
      if (written <= 0) throw Error('graphics pipe closed');
      offset += written;
    }
  }
}

async function main() {
  const [vendor, input, output] = process.argv.slice(2);
  const pins = JSON.parse(fs.readFileSync(path.join(vendor, 'CanvasKit.json'))).upstreamFilesSha256;
  const wasm = zlib.gunzipSync(fs.readFileSync(path.join(vendor, 'canvaskit.wasm.gz')), {maxOutputLength: 16 << 20});
  const loader = path.resolve(vendor, 'canvaskit.js');
  const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
  if (hash(wasm) !== pins['package/bin/full/canvaskit.wasm'] ||
      hash(fs.readFileSync(loader)) !== pins['package/bin/full/canvaskit.js']) throw Error('CanvasKit digest mismatch');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gnome-original-skp-'));
  try {
    const local = path.join(dir, 'canvaskit.wasm');
    fs.writeFileSync(local, wasm);
    const kit = await require(loader)({locateFile: () => local});
    if (input === '--pipe' && output === undefined) {
      paintPipe(kit);
    } else {
      if (fs.statSync(input).size > frameLimit) throw Error('frame too large');
      fs.writeFileSync(output, draw(kit, fs.readFileSync(input)));
    }
  } finally {
    fs.rmSync(dir, {recursive: true, force: true});
  }
}
main().catch(err => { console.error(err.message); process.exitCode = 1; });
