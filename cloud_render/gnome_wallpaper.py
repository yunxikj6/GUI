"""One immutable Ubuntu Noble graphics resource shared by a consenting display."""
import hashlib
import struct
from pathlib import Path

WIDTH, HEIGHT = 3840, 2160
PIXELS = WIDTH * HEIGHT
RGBX_SHA256 = bytes.fromhex('16ff86e9d3bb20925bb14030ead8ea83631eaa97efb010c1f643f24759c16019')
RGBX_99_SHA256 = bytes.fromhex('b4da4262ad652769b76d6d97c46514ea7e611362f436be6f965edf90c9330ca1')
RGB_XZ_SHA256 = bytes.fromhex('93aa46251c15eed89240c8815e76c351575083a4f6717b2288d78177a395078a')
XZ_BYTES = 5787728
PUT = 5
REFERENCE = 0xffffff01
MAX_REFERENCES = 4
HEADER = struct.Struct('<II')
TRANSFER = struct.Struct('<11I')


def decoded_asset(path):
    """Verify the distributed original RGB pixels before advertising support."""
    import lzma
    with Path(path).open('rb') as f:
        encoded = f.read(XZ_BYTES + 1)
    if len(encoded) != XZ_BYTES or hashlib.sha256(encoded).digest() != RGB_XZ_SHA256:
        raise ValueError('desktop artwork has the wrong content digest')
    try:
        archive = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=32 << 20)
        rgb = archive.decompress(encoded, max_length=PIXELS * 3 + 1)
    except lzma.LZMAError as exc:
        raise ValueError('invalid Ubuntu artwork archive') from exc
    if len(rgb) != PIXELS * 3 or not archive.eof or archive.unused_data:
        raise ValueError('invalid original Ubuntu desktop artwork')
    rgbx = bytearray(PIXELS * 4)
    for channel in range(3):
        rgbx[channel::4] = rgb[channel::3]
    if hashlib.sha256(rgbx).digest() != RGBX_SHA256:
        raise ValueError('decoded desktop pixels differ from the original Mesa resource')
    return bytes(rgbx)


def _complete_upload(desc):
    handle, level, stride, layer_stride, x, y, z, width, height, depth, size = TRANSFER.unpack_from(desc)
    return (handle != 0 and (level, x, y, z, width, height, depth, size) ==
            (0, 0, 0, 0, WIDTH, HEIGHT, 1, PIXELS * 4)
            and stride in (0, WIDTH * 4) and layer_stride in (0, size))


def put_reference(payload):
    """Match actual texels. Neither the dimensions nor a handle identify an asset."""
    if len(payload) != TRANSFER.size + PIXELS * 4:
        return None
    return reference_for_upload(payload[:TRANSFER.size], memoryview(payload)[TRANSFER.size:])


def reference_for_upload(desc, pixels, *, x99=False):
    if len(desc) != TRANSFER.size or len(pixels) != PIXELS * 4 or not _complete_upload(desc):
        return None
    digest = hashlib.sha256(pixels).digest()
    if digest != RGBX_SHA256 and not (x99 and digest == RGBX_99_SHA256):
        return None
    return HEADER.pack((TRANSFER.size + 32) // 4, REFERENCE) + desc + digest


def expand_reference(body, asset, *, x99=False):
    digest = body[TRANSFER.size:]
    if len(digest) != 32 or (digest != RGBX_SHA256 and not (x99 and digest == RGBX_99_SHA256)):
        raise ValueError('unknown desktop resource reference')
    size = TRANSFER.unpack_from(body)[-1]
    if asset is None or size != len(asset) or not _complete_upload(body):
        raise ValueError('referenced original asset is unavailable')
    if digest == RGBX_99_SHA256:
        # Two independently measured cloud uploads have the same RGB samples
        # as the bundled artwork, with each fourth byte set to decimal 99.
        native = bytearray(asset)
        native[3::4] = bytes([99]) * PIXELS
        if hashlib.sha256(native).digest() != digest:
            raise ValueError('reconstructed Ubuntu texture differs from its source')
        asset = native
    return HEADER.pack(TRANSFER.size // 4 + (size + 3) // 4, PUT) + body[:TRANSFER.size], asset
