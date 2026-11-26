"""Validate an isolated source recording prior to releasing any application picture reference."""

import argparse
import json
from collections import defaultdict
from hashlib import sha256
from pathlib import Path

from PIL import Image

from cloud_render import gnome_commands as mesa

from .capture import business_digest
from .controls import ORIGINAL_SHA256, require


def read_witness(path, identity):
    require(path.stat().st_size <= 16 << 20, 'source witness file too large')
    peers, roles, top, labels, commits = {}, [], {}, {}, set()
    shas, textures, uploads = {}, {}, {}
    cogl = set()
    with path.open('rb') as f:
        for line in f:
            fields = line.decode('ascii').split()
            name = fields.pop(0)
            if name == 'XDG_SURFACE':
                roles.append(tuple(map(int, fields)))
            elif name == 'TOPLEVEL':
                c, xdg, t = map(int, fields)
                require((c, xdg) not in top, 'xdg_surface reused')
                top[c, xdg] = t
            elif name == 'APP_ID':
                c, t, label = fields
                require((int(c), int(t)) not in labels, 'app ID changed')
                labels[int(c), int(t)] = label
            elif name == 'COMMIT':
                commits.add(tuple(map(int, fields)))
            elif name == 'PEER':
                *peer, serial = map(int, fields)
                require(serial not in peers, 'attach serial reused')
                peers[serial] = peer
            elif name == 'SHM':
                serial, stride, height, digest = fields
                shas[int(serial)] = (int(stride), int(height), digest)
            elif name in ('COGL_REGION', 'COGL_CREATE'):
                cogl.add(int(fields[0]))
            elif name == 'GL':
                serial, gl, target = map(int, fields)
                require(serial not in textures or textures[serial] == (gl, target), 'GL target changed')
                textures[serial] = (gl, target)
            elif name == 'WIRE':
                serial, offset, *desc = map(int, fields)
                require(offset not in uploads, 'duplicate wire offset')
                uploads[offset] = (serial, desc, f.tell())
    pid = identity['pid']
    require(
        identity['binarySha256'] == ORIGINAL_SHA256 and identity['target'] == 'app://-/index.html',
        'wrong original binary or target',
    )
    match = [
        (c, s, xdg, top[c, xdg])
        for c, p, xdg, s in roles
        if p == pid and labels.get((c, top.get((c, xdg)))) == 'chatgpt'
    ]
    require(len(match) == 1, 'missing exclusive original toplevel')
    client, surface, xdg, toplevel = match[0]
    expected = [client, pid, identity['startTicks'], identity['device'], identity['inode'], surface]
    owned = {}
    for offset, (serial, desc, journal) in uploads.items():
        peer = peers.get(serial)
        require(peer is not None, 'wire contains unknown attach')
        if peer[-2] != surface or peer[0] != client:
            continue
        require(
            peer[:-1] == expected
            and (client, pid, surface, serial) in commits
            and serial in cogl
            and textures.get(serial, (None, None))[1] == 3553
            and serial in shas,
            'incomplete owner chain',
        )
        owned[offset] = (serial, desc, journal, peer[-1], textures[serial][0], shas[serial])
    require(owned, 'no witnessed application uploads')
    return {'connection': client, 'surface': surface, 'xdgSurface': xdg, 'toplevel': toplevel}, owned


def resolve(directory):
    capture = json.loads((directory / 'capture.json').read_text())
    role, markers = read_witness(directory / 'owner.log', capture['identity'])
    rows, states = [], capture['states']
    require(set(states) == {'before', 'after'}, 'signed-out capture requires two authenticated states')
    for label, state in states.items():
        require(
            sha256((directory / (label + '.clrp')).read_bytes()).hexdigest() == state['pictureSha256'],
            'changed local picture',
        )
    live, generation = {}, defaultdict(int)
    keys = set()
    image = Image.new('RGBA', (1090, 760))
    create_count = ordinal = 0
    data_path = directory / 'commands.virgl'
    require(data_path.stat().st_size <= 512 << 20, 'experiment archive exceeds 512 MiB')
    first = None
    unknown = set()
    with data_path.open('rb') as fd:
        while True:
            offset = fd.tell()
            packet = mesa._request(fd)
            if packet is None:
                break
            ordinal += 1
            _, command, payload = packet
            require(offset not in markers or command == mesa.PUT, 'wire offset does not identify a PUT')
            if command == mesa.CREATE:
                handle, _, fmt, _, w, h, *_ = mesa._RESOURCE.unpack(payload)
                generation[handle] += 1
                create_count += 1
                live[handle] = (generation[handle], ordinal, create_count, fmt, w, h)
            elif command == mesa.UNREF:
                live.pop(mesa._WORD.unpack(payload)[0], None)
            elif command == mesa.PUT:
                handle, level, stride, layer_stride, x, y, z, w, h, depth, size = mesa._TRANSFER.unpack_from(
                    payload
                )
                if offset not in markers:
                    if handle in live:
                        unknown.add((handle, live[handle][0]))
                    continue
                serial, desc, journal, buf, gl, shm = markers.pop(offset)
                require(desc == [handle, x, y, w, h, size] and live.get(handle), 'wire descriptor mismatch')
                gen, alloc, create, fmt, fullw, fullh = live[handle]
                keys.add((handle, gen))
                require(
                    (fmt, fullw, fullh, level, z, depth) == (67, 1090, 760, 0, 0, 1)
                    and (shm[0], shm[1], stride) == (4360, 760, 4360)
                    and layer_stride in (0, size)
                    and x + w <= 1090
                    and y + h <= 760
                    and w > 0
                    and h > 0
                    and len(payload) == 44 + size
                    and stride * (h - 1) + w * 4 <= size <= stride * h,
                    'unsupported original texture layout',
                )
                if first is None:
                    require((x, y, w, h) == (0, 0, fullw, fullh), 'missing initial application texture')
                    first = (handle, gen, gl)
                require(first == (handle, gen, gl), 'source app resource was replaced')
                # Mesa sends right-edge excess bytes in padded rows. Preserve only actual damage.
                region = Image.frombytes('RGBA', (w, h), payload[44:], 'raw', 'RGBA', stride)
                image.paste(region, (x, y))
                r, g, b, a = image.split()
                require(
                    sha256(Image.merge('RGBA', (b, g, r, a)).tobytes()).hexdigest() == shm[2],
                    'reconstructed wire bytes disagree with the compositor SHM',
                )
                row = {
                    'byteOffset': offset,
                    'wireCommandOrdinal': ordinal,
                    'creationOrdinal': alloc,
                    'creationAmongCreates': create,
                    'handle': handle,
                    'generation': gen,
                    'glTexture': gl,
                    'buffer': buf,
                    'commit': serial,
                    'witnessByte': journal,
                    'shmSha256': shm[2],
                }
                region_hash = sha256(image.crop((1026, 0, 1090, 36)).convert('RGB').tobytes()).hexdigest()
                # A source-only gate for the observed, at-rest original controls.
                if region_hash == '1d33223cedfb68917cbf807cd1664b20a46466ad61093dd292c6d0da70aec0a7':
                    row['businessRgbSha256'] = business_digest(image)
                rows.append(row)
    require(
        not markers and len(keys) == 1 and keys.isdisjoint(unknown),
        'unmapped write or an unwitnessed transfer to this resource lifetime',
    )
    for name, state in states.items():
        start, end = state['witnessByteStart'], state['witnessByteEnd']
        require(0 <= start <= end, 'invalid witness journal bounds')
        observed = [r for r in rows if r['witnessByte'] <= end]
        require(
            observed and observed[-1].get('businessRgbSha256') == state['businessRgbSha256'],
            'original SHM at the paint boundary does not match the captured app',
        )
        candidates = [r for r in observed if start < r['witnessByte'] <= end]
        if not candidates:
            candidates = [observed[-1]]
        for row in candidates:
            if row.get('businessRgbSha256') == state['businessRgbSha256']:
                require('pictureSha256' not in row, 'overlapping original picture captures')
                row.update(state=name, pictureSha256=state['pictureSha256'])
    with data_path.open('rb') as f:
        from hashlib import file_digest

        stream_sha = file_digest(f, 'sha256').hexdigest()
    return {'commandSha256': stream_sha, 'identity': capture['identity'], 'wayland': role, 'puts': rows}


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('source', type=Path)
    args = cli.parse_args()
    (args.source / 'provenance.json').write_text(json.dumps(resolve(args.source), indent=2))
