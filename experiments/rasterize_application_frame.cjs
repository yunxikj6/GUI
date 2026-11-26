// Render the original cloud SkPictures with the same pinned full CanvasKit
// runtime as the application viewer. No window image is accepted as input.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const zlib = require('node:zlib');

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
    if (fs.statSync(input).size > (32 << 20) + 12) throw Error('frame too large');
    const bytes = fs.readFileSync(input);
    if (bytes.length < 22 || bytes.toString('ascii', 0, 4) !== 'CLRP' || bytes[4] !== 1 || bytes[5] !== 1 ||
        bytes.readUInt16BE(6) || bytes.readUInt32BE(8) !== bytes.length - 12) throw Error('invalid Frame');
    const width = bytes.readUInt16BE(16), height = bytes.readUInt16BE(18), count = bytes.readUInt16BE(20);
    if (!width || !height || width * height * 4 > 32 << 20 || !count || count > 128) throw Error('invalid dimensions');
    const surface = kit.MakeSurface(width, height), canvas = surface.getCanvas();
    canvas.clear(kit.TRANSPARENT);
    let offset = 22;
    for (let i = 0; i < count; i++) {
      const x = bytes.readFloatBE(offset), y = bytes.readFloatBE(offset + 4);
      const w = bytes.readFloatBE(offset + 8), h = bytes.readFloatBE(offset + 12);
      const size = bytes.readUInt32BE(offset + 16);
      offset += 20;
      if (size > (16 << 20) || offset + size > bytes.length) throw Error('invalid picture length');
      const picture = kit.MakePicture(bytes.subarray(offset, offset + size));
      if (!picture) throw Error('unsupported original SkPicture');
      const [l, t, r, b] = picture.cullRect();
      canvas.save();
      canvas.translate(x, y);
      canvas.clipRect([0, 0, w, h], kit.ClipOp.Intersect, false);
      canvas.scale(w / (r - l), h / (b - t));
      canvas.translate(-l, -t);
      canvas.drawPicture(picture);
      canvas.restore();
      picture.delete();
      offset += size;
    }
    if (offset !== bytes.length) throw Error('trailing bytes');
    surface.flush();
    const image = surface.makeImageSnapshot();
    const pixels = image.readPixels(0, 0, {width, height, colorType: kit.ColorType.RGBA_8888,
      alphaType: kit.AlphaType.Unpremul, colorSpace: kit.ColorSpace.SRGB});
    if (!pixels || pixels.length !== width * height * 4) throw Error('incomplete local render');
    fs.writeFileSync(output, pixels);
    image.delete();
    surface.dispose();
  } finally {
    fs.rmSync(dir, {recursive: true, force: true});
  }
}
main().catch(err => { console.error(err.message); process.exitCode = 1; });
