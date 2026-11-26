"""Replay the two witnessed application states from a binary stream on Windows."""
import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sys

from experiments.application_surface import Binding, SurfaceError
from experiments.live_application_surface import LiveSurfaceStream
from experiments.persistent_application_painter import OriginalPicturePainter
from thin_client.virgl_windows import replay


def render(source, manifest, output, *, node='node', runtime_dir=None):
    fields = manifest['binding']
    binding = Binding(bytes.fromhex(fields['session']), bytes.fromhex(fields['executableSha256']),
                      fields['pid'], bytes.fromhex(fields['processInstance']), fields['surface'],
                      fields['handle'], fields['creation'])
    if set(manifest['states']) != {'before', 'after'}:
        raise SurfaceError('unexpected original states')
    phases = {bytes.fromhex(digest): label for label, digest in manifest['states'].items()}
    if len(phases) != 2 or any(len(digest) != 32 for digest in phases):
        raise SurfaceError('expected two distinct approved drawings')
    shots = {}
    scanouts = held = 0
    output.mkdir(parents=True, exist_ok=True)
    with OriginalPicturePainter(node=node) as paint:
        def paint_approved(frame):
            if stream.digest not in phases:
                raise SurfaceError('unrecognized original application drawing')
            return paint(frame)
        stream = LiveSurfaceStream(source, session=binding.session, authorize=lambda candidate: candidate == binding,
                                   rasterize=paint_approved)
        def present(width, height, pixels):
            nonlocal scanouts, held
            scanouts += 1
            if not stream.can_present:
                held += 1
                return
            if stream.commit:
                phase = phases.get(stream.digest)
                if phase is None:
                    raise SurfaceError('unrecognized original application drawing')
                (output / (phase + '.bgrx')).write_bytes(pixels)
                shots[phase] = dict(width=width, height=height, ordinal=scanouts,
                                    sha256=hashlib.sha256(pixels).hexdigest())
        replay(stream, present, runtime_dir=runtime_dir)
    if set(shots) != {'before', 'after'}:
        raise SurfaceError('the stream did not deliver both original states')
    return dict(scanouts=scanouts, held=held, pictures=stream.drawings, reused=stream.reuses,
                pending=stream.holds, states=shots)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, help='independently supplied expected binding and state digests')
    parser.add_argument('output', type=Path)
    parser.add_argument('--input', type=Path, help='read a sanitized stream file instead of stdin')
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--node', default='node')
    args = parser.parse_args()
    context = args.input.open('rb') if args.input else nullcontext(sys.stdin.buffer)
    with context as source:
        report = render(source, json.loads(args.manifest.read_bytes()), args.output,
                        node=args.node, runtime_dir=args.runtime_dir)
    (args.output/'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
