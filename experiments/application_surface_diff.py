"""Quantify RGB differences inside each application region and across the desktop."""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops


def _metrics(delta, mask):
    count = mask.histogram()[255]
    hist = delta.histogram(mask)
    mean = sum((i % 256) * n for i, n in enumerate(hist)) / (3*count) if count else 0
    r, g, b = delta.split()
    max_channel = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return {'pixels': count, 'differentPixels': count - max_channel.histogram(mask)[0],
            'meanAbsoluteChannelError': mean}


def _image(path, raw_size):
    path = Path(path)
    if path.suffix.lower() == '.bgrx':
        if not raw_size or min(raw_size) <= 0 or max(raw_size) > 8192 or path.stat().st_size != 4*raw_size[0]*raw_size[1]:
            raise ValueError('raw scanout needs its exact width and height')
        return Image.frombytes('RGB', tuple(raw_size), path.read_bytes(), 'raw', 'BGRX', 0, 1)
    with Image.open(path) as image:
        return image.convert('RGB')


def compare(source, local, regions, difference=None, raw_size=None):
    original, rendered = _image(source, raw_size), _image(local, raw_size)
    if rendered.size != original.size:
        raise ValueError('source and local image sizes must match')
    delta = ImageChops.difference(original, rendered)
    all_pixels = Image.new('L', original.size, 255)
    outside = all_pixels.copy()
    groups = []
    for left, top, right, bottom in regions:
        if not (0 <= left < right <= original.width and 0 <= top < bottom <= original.height):
            raise ValueError('region outside the supplied desktop')
        box = (left, top, right, bottom)
        selected = Image.new('L', original.size, 0)
        selected.paste(255, box)
        groups.append({'region': box, **_metrics(delta, selected)})
        outside.paste(0, box)
    if difference:
        # Multiply each original RGB difference by eight for a visible artifact.
        delta.point(lambda v: min(255, 8*v)).save(difference)
    return {'size': original.size, 'desktop': _metrics(delta, all_pixels),
            'regions': groups, 'outsideAllRegions': _metrics(delta, outside),
            'differenceBounds': delta.getbbox()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('local', type=Path)
    parser.add_argument('--region', type=int, nargs=4, action='append', required=True,
                        metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'))
    parser.add_argument('--difference', type=Path)
    parser.add_argument('--raw-size', nargs=2, type=int, metavar=('WIDTH', 'HEIGHT'))
    args = parser.parse_args()
    print(json.dumps(compare(args.source, args.local, args.region, args.difference, args.raw_size), indent=2))
