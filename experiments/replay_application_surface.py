"""Prepare or reproduce the pinned two-state GNOME/application comparison."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

from cloud_render.wire import Frame, Layer, encode_message, read_message
from experiments import gnome_command_stream as mesa
from experiments.application_surface import ApplicationSurfaces, Binding, Commit, SurfaceError
from thin_client.virgl_windows import replay

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / 'docs/measurements/application-surface/capture.json'
STATES = ('before', 'after')
WAIT_FOR_APP = 0x534B5002
APP_PICTURE = 0x534B5001


def _hash(stream):
    digest = hashlib.sha256()
    while chunk := stream.read(65536):
        digest.update(chunk)
    return digest.hexdigest()


def _verify_file(path, wanted):
    with path.open('rb') as stream:
        if _hash(stream) != wanted:
            raise SurfaceError('capture file digest mismatch: ' + path.name)


def _frame(path):
    with path.open('rb') as stream:
        frame = read_message(stream, expected=Frame)
        if frame is None or stream.read(1):
            raise SurfaceError('expected exactly one application drawing')
        return frame


def with_native_controls(page, controls, receipt, binding, style):
    """Join the exact browser-owned drawings at the verified WCO geometry."""
    if (receipt['pid'] != binding['pid'] or receipt['actualStyle'] != style
            or not style['visible'] or style['dpr'] != 1 or style['viewport'] != [page.width, page.height]
            or len(receipt['layers']) != 3 or len(controls.layers) != 3):
        raise SurfaceError('native control surface evidence mismatch')
    rect = style['titlebarRect']
    if (rect['left'], rect['top'], rect['right'] + controls.width, rect['height']) != (0, 0, page.width, controls.height):
        raise SurfaceError('control region does not match source window')
    placed = []
    for index, (layer, proof) in enumerate(zip(controls.layers, receipt['layers'])):
        x, y = rect['right'] + layer.x, layer.y
        w, h = layer.width, layer.height
        if (proof['sourceName'] != ('ChromeNodeCaptionButtonContainer' if index == 0 else 'ChromeNodeCaptionButton')
                or proof['quad'] != [x, y, x+w, y, x+w, y+h, x, y+h]
                or proof['bytes'] != len(layer.skp) or proof['sha256'] != hashlib.sha256(layer.skp).hexdigest()
                or layer.x < 0 or layer.y < 0 or layer.x+w > controls.width or layer.y+h > controls.height):
            raise SurfaceError('native control drawing provenance mismatch')
        placed.append(Layer(x, y, w, h, layer.skp))
    return Frame(page.sequence, page.width, page.height, page.layers + tuple(placed))


def prepare(source, output):
    spec = json.loads(SPEC.read_text())
    for name, pin in spec['inputHash'].items():
        _verify_file(source / name, pin)
    output.mkdir(parents=True, exist_ok=True)
    rows = json.loads((source / 'transfer-identity-table.json').read_bytes())
    binding = spec['binding']
    handle = binding['vtestHandle']
    owned = {r['wireCommandOrdinal']: r for r in rows['records']
             if (r['pid'], r['surface'], r['GL_TEXTURE']) ==
             (binding['pid'], binding['waylandSurface'], binding['glTexture'])}
    if rows['unmatchedTaggedTransfers'] or not all(r['wlShmFormatCanonicalMatches'] for r in owned.values()):
        raise SurfaceError('incomplete resource provenance')
    session = bytes.fromhex(spec['commandSha256'])
    process = hashlib.sha256(session + struct.pack('!QQQQQ', binding['pid'], binding['processStartTicks'],
                                                   binding['device'], binding['inode'], binding['waylandConnectionSerial'])).hexdigest()
    authority = dict(session=session.hex(), exe=binding['sourceExecutableSha256'], pid=binding['pid'],
                     instance=process, surface=binding['waylandSurface'], creation=binding['vtestCreateOrdinal'],
                     handle=handle, commits={}, actions={})
    approvals = {}
    controls = _frame(source / 'original-controls.clrp')
    receipt = json.loads((source / 'original-controls.json').read_bytes())
    for phase in STATES:
        page = _frame(source / f'broker-{phase}.clrp')
        style = json.loads((source / f'stable-wco-{phase}.json').read_bytes())
        wire = encode_message(with_native_controls(page, controls, receipt, binding, style))
        if wire != (source / spec['drawingFiles'][phase]).read_bytes():
            raise SurfaceError('complete drawing differs from traced original layers')
        (output / f'{phase}.clrp').write_bytes(wire)
        state = spec['verifiedStates'][phase]
        digest = hashlib.sha256(wire).digest()
        if digest.hex() != state['sha256'] or len(state['actualCommits']) != len(state['transferOrdinals']):
            raise SurfaceError('incomplete original commit evidence')
        for ordinal, serial in zip(state['transferOrdinals'], state['actualCommits']):
            if ordinal not in owned or ordinal in approvals:
                raise SurfaceError('commit does not identify a unique verified transfer')
            approvals[ordinal] = phase, serial, digest
    seen = set()
    with gzip.open(source/'commands.virgl.gz', 'rb') as incoming, gzip.open(output/'desktop.commands.gz', 'wb') as desktop:
        ordinal = 0
        while msg := mesa._request(incoming):
            ordinal += 1
            header, op, data = msg
            observed = owned.get(ordinal)
            if observed is None:
                if op == mesa.PUT and mesa._TRANSFER.unpack_from(data)[0] == handle:
                    raise SurfaceError('unattributed raster upload')
                desktop.write(header + data)
                continue
            if (op != mesa.PUT or observed['resource'] != handle or observed['CoglObject'] != binding['coglObject']
                    or observed['resourceCreationOrdinal'] != binding['wireCreateCommand']
                    or observed['resourceCreationGeneration'] != 1
                    or observed['rawPutSha256'] != hashlib.sha256(data[mesa._TRANSFER.size:]).hexdigest()
                    or mesa._TRANSFER.unpack_from(data)[0] != handle):
                raise SurfaceError('resource lifetime or transfer evidence mismatch')
            seen.add(ordinal)
            if ordinal in approvals:
                phase, serial, digest = approvals[ordinal]
                ticket = hashlib.sha256(session + struct.pack('!II', handle, serial) + digest).digest()
                authority['commits'][ticket.hex()] = dict(serial=serial, sha=digest.hex(), phase=phase)
                authority['actions'][ordinal] = phase
                body = data[:mesa._TRANSFER.size] + ticket
                desktop.write(mesa._HEADER.pack(len(body)//4, APP_PICTURE) + body)
            else:
                desktop.write(mesa._HEADER.pack(1, WAIT_FOR_APP) + mesa._WORD.pack(handle))
                authority['actions'][ordinal] = None
    if set(owned) != seen:
        raise SurfaceError('missing recorded resource transfer')
    (output/'authority.json').write_text(json.dumps(authority), encoding='utf-8')
    _verify_prepared(output, spec)


def _verify_prepared(directory, spec):
    _verify_file(directory/'authority.json', spec['preparedAuthoritySha256'])
    for phase in STATES:
        _verify_file(directory/f'{phase}.clrp', spec['inputHash'][spec['drawingFiles'][phase]])
    with gzip.open(directory/'desktop.commands.gz', 'rb') as stream:
        if _hash(stream) != spec['preparedDesktopSha256']:
            raise SurfaceError('desktop command digest mismatch')


class _SurfaceStream:
    """Expand trusted application references lazily as VirGL consumes vtest."""
    def __init__(self, stream, guard, phases, authority):
        self.stream, self.guard, self.phases, self.authority = stream, guard, phases, authority
        self.buffer = memoryview(b'')
        self.phase = None

    def read(self, n):
        while not self.buffer:
            header = mesa._read(self.stream, 8, allow_eof=True)
            if not header:
                return b''
            words, op = mesa._HEADER.unpack(header)
            if op == WAIT_FOR_APP:
                if words != 1:
                    raise SurfaceError('bad wait record')
                self.guard.wait_for_picture(mesa._WORD.unpack(mesa._read(self.stream, 4))[0])
                self.phase = None
                continue
            if op == APP_PICTURE:
                if words != 19:
                    raise SurfaceError('bad drawing reference')
                desc = mesa._TRANSFER.unpack(mesa._read(self.stream, 44))
                ticket = mesa._read(self.stream, 32)
                layout, pixels = self.guard.local_texture(desc, ticket)
                data = mesa._TRANSFER.pack(*layout) + pixels
                header = mesa._HEADER.pack(len(data)//4, mesa.PUT)
                self.phase = self.phases[self.authority[ticket].picture_sha256.hex()]
            elif op == mesa.PUT:
                desc = mesa._read(self.stream, 44)
                values = mesa._TRANSFER.unpack(desc)
                self.guard.check_raw_upload(values)
                if words != 11 + (values[-1] + 3)//4:
                    raise SurfaceError('bad upload size')
                data = desc + mesa._read(self.stream, values[-1])
            else:
                data = mesa._read(self.stream, words if op == mesa.RENDERER else words*4)
                if op == mesa.CREATE:
                    self.guard.observe_created(mesa._RESOURCE.unpack(data))
                elif op == mesa.UNREF:
                    self.guard.observe_unreferenced(mesa._WORD.unpack(data)[0])
            self.buffer = memoryview(header + data)
        result = self.buffer[:n].tobytes()
        self.buffer = self.buffer[len(result):]
        return result


def present_prepared(directory, on_present, rasterize, *, runtime_dir=None):
    """Run the pinned recorded experiment with a supplied local Skia renderer."""
    spec = json.loads(SPEC.read_text())
    _verify_prepared(directory, spec)
    data = json.loads((directory/'authority.json').read_bytes())
    session = bytes.fromhex(data['session'])
    binding = Binding(session, bytes.fromhex(data['exe']), data['pid'], bytes.fromhex(data['instance']),
                      data['surface'], data['handle'], data['creation'])
    records = {bytes.fromhex(key): Commit(binding, r['serial'], bytes.fromhex(r['sha']))
               for key, r in data['commits'].items()}
    phases = {r['sha']: r['phase'] for r in data['commits'].values()}
    def asset(digest, limit):
        path = directory / (phases[digest.hex()] + '.clrp')
        if path.stat().st_size > limit:
            raise SurfaceError('drawing exceeds bound')
        return path.read_bytes()
    guard = ApplicationSurfaces(session, lambda s, h, g: binding if (s, h, g) ==
                                (session, binding.handle, binding.creation) else None,
                                lambda s, t: records.get(t) if s == session else None, asset, rasterize)
    counts = {'scanouts': 0, 'held': 0}
    with gzip.open(directory/'desktop.commands.gz', 'rb') as incoming:
        desktop = _SurfaceStream(incoming, guard, phases, records)
        def present(w, h, pixels):
            counts['scanouts'] += 1
            if not guard.can_present:
                counts['held'] += 1
            elif desktop.phase:
                on_present(desktop.phase, w, h, pixels, counts['scanouts'])
        replay(desktop, present, runtime_dir=runtime_dir)
    return counts


def render(directory, output, runtime_dir, node):
    spec = json.loads(SPEC.read_text())
    _verify_prepared(directory, spec)
    by_digest = {spec['verifiedStates'][phase]['sha256']: phase for phase in STATES}
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        for phase in STATES:
            subprocess.run([node, str(Path(__file__).with_name('rasterize_application_frame.cjs')),
                            str(ROOT/'thin_client/vendor'), str(directory/f'{phase}.clrp'), str(cache/f'{phase}.rgba')],
                           check=True, timeout=60)
        def raster(frame):
            phase = by_digest[hashlib.sha256(encode_message(frame)).hexdigest()]
            return (cache/(phase+'.rgba')).read_bytes()
        shown = {}
        def save(phase, w, h, pixels, ordinal):
            shown[phase] = {'size': [w, h], 'ordinal': ordinal, 'sha256': hashlib.sha256(pixels).hexdigest()}
            (output/(phase+'.bgrx')).write_bytes(pixels)
        counts = present_prepared(directory, save, raster, runtime_dir=runtime_dir)
        if set(shown) != set(STATES):
            raise SurfaceError('did not present both verified states')
        (output/'scanouts.json').write_text(json.dumps({'states': shown, **counts}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    prep = sub.add_parser('prepare', help='extract verified drawing references on the source side')
    prep.add_argument('evidence', type=Path)
    prep.add_argument('directory', type=Path)
    run = sub.add_parser('render', help='replay the sanitized command bundle on a Windows x64 rendering host')
    run.add_argument('directory', type=Path)
    run.add_argument('output', type=Path)
    run.add_argument('--runtime-dir', type=Path)
    run.add_argument('--node', default='node')
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args.evidence, args.directory)
    else:
        render(args.directory, args.output, args.runtime_dir, args.node)
