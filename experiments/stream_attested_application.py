"""Stream a finite source-witnessed native application recording to a paired consumer.

The source resolver must produce the receipt using the original compositor
witness, private original process and source data. The receiver only needs its
independently approved session binding and the sanitized stream on stdin.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

from experiments.application_surface import Binding, SurfaceError
from experiments.live_application_surface import DESKTOP, WitnessedPut, stream_witnessed


def original_binding(session, receipt):
    who, role, rows = receipt['identity'], receipt['wayland'], receipt['puts']
    lifetimes = {(row['handle'], row['creationAmongCreates'], row['generation']) for row in rows}
    if len(lifetimes) != 1 or who['target'] != 'app://-/index.html':
        raise SurfaceError('expected one verified original window lifetime')
    handle, creation, generation = lifetimes.pop()
    if generation != 1:
        raise SurfaceError('unverified original resource generation')
    instance = hashlib.sha256(session + struct.pack('!QQQQQ', who['pid'], who['startTicks'], who['device'],
                                                   who['inode'], role['connection'])).digest()
    return Binding(session, bytes.fromhex(who['binarySha256']), who['pid'], instance, role['surface'], handle, creation)


def stream_resolved(directory, sink, receipt, session):
    """Send a sealed capture; all targeted draw replacements use true byte offsets."""
    binding = original_binding(session, receipt)
    captures = json.loads((directory/'capture.json').read_bytes())
    if captures['identity'] != receipt['identity'] or set(captures['states']) != {'before','after'}:
        raise SurfaceError('source capture changed identity or states')
    assets = {bytes.fromhex(s['pictureSha256']): directory/(phase+'.clrp')
              for phase, s in captures['states'].items()}
    records = {r['byteOffset']:r for r in receipt['puts']}
    if len(records) != len(receipt['puts']):
        raise SurfaceError('duplicate source application offset')
    def inspect(offset, ordinal, desc, generation, _source_pixels):
        row = records.pop(offset, None)
        if row is None:
            if desc[0] == binding.handle and generation == binding.creation:
                raise SurfaceError('missing witness for an original upload')
            return DESKTOP
        if (row['wireCommandOrdinal'], row['creationAmongCreates'], row['handle']) != (ordinal, generation, desc[0]):
            raise SurfaceError('original upload at incorrect source offset')
        digest = bytes.fromhex(row['pictureSha256']) if 'pictureSha256' in row else None
        if digest is not None and digest not in assets:
            raise SurfaceError('unrecognized original application drawing')
        return WitnessedPut(binding,row['commit'],digest)
    def asset(digest, limit):
        path = assets[digest]
        if not 0 < path.stat().st_size <= limit:
            raise SurfaceError('original picture exceeds bound')
        return path.read_bytes()
    with (directory/'commands.virgl').open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest() != receipt['commandSha256']:
            raise SurfaceError('recorded Mesa data changed')
        stream.seek(0)
        stats = stream_witnessed(stream,sink,inspect_put=inspect,fetch_picture=asset)
    if records:
        raise SurfaceError('missing source records')
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence',type=Path)
    parser.add_argument('--manifest', type=Path, required=True, help='write the receiver identity separately')
    args = parser.parse_args()
    if sys.stdout.isatty():
        parser.error('pipe the sanitized graphics stream into the paired receiver')
    # The demo uses the complete source's SHA as its fixture namespace. A live
    # transport chooses its authenticated per-connection session on pairing.
    from experiments.mutter_app.bind import resolve
    receipt = resolve(args.evidence)
    session = bytes.fromhex(receipt['commandSha256'])
    binding = original_binding(session, receipt)
    captures = json.loads((args.evidence/'capture.json').read_bytes())
    manifest = dict(binding=dict(session=session.hex(), executableSha256=binding.executable_sha256.hex(),
                                pid=binding.pid, processInstance=binding.process_instance.hex(),
                                surface=binding.surface_id, handle=binding.handle, creation=binding.creation),
                    states={name: state['pictureSha256'] for name, state in captures['states'].items()})
    args.manifest.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    stats = stream_resolved(args.evidence, sys.stdout.buffer, receipt, session)
    print(json.dumps(stats),file=sys.stderr)


if __name__ == '__main__':
    main()
