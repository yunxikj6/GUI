"""Report original Wayland pointer delivery in a completed, opt-in private source run."""

import argparse
import json
from pathlib import Path

from .controls import ORIGINAL_SHA256, require
from .live import Journal


def observed_clicks(lines, *, client, pid, surface, verified_commits):
    """Return matched physical left-button pairs on the verified, unscaled window.

    This is a retrospective diagnostic. Window ownership and pointer delivery
    cannot identify or authorize application-internal controls.
    """
    placement = None
    points = {}
    pressed = {}
    clicks = []

    for line in lines:
        fields = line.split()
        if not fields:
            continue
        event = fields[0]
        if event == 'POINTER_DISCONNECT' and int(fields[1]) == client:
            break
        if event == 'PLACEMENT':
            connection, owner, target, commit = map(int, fields[1:5])
            if (connection, owner, target) != (client, pid, surface):
                continue
            buffer = tuple(map(int, fields[5:9]))
            frame = tuple(map(int, fields[9:13]))
            actor = tuple(map(float, fields[13:17]))
            new_placement = (
                buffer
                if commit in verified_commits and buffer == frame == actor and buffer[2:] == (1090, 760)
                else None
            )
            if placement != new_placement:
                pressed.clear()
            placement = new_placement
            continue
        if not event.startswith('POINTER_') or len(fields) < 4:
            continue
        connection, owner, obj = map(int, fields[1:4])
        if (connection, owner) != (client, pid):
            continue
        if event == 'POINTER_SURFACE_GONE' and obj == surface:
            break
        if event == 'POINTER_ENTER':
            points.pop(obj, None)
            pressed.pop(obj, None)
            if int(fields[4]) == surface:
                points[obj] = tuple(map(int, fields[6:8]))
        elif event in ('POINTER_LEAVE', 'POINTER_RELEASE'):
            points.pop(obj, None)
            pressed.pop(obj, None)
        elif event == 'POINTER_MOTION' and obj in points:
            points[obj] = tuple(map(int, fields[5:7]))
        elif event == 'POINTER_BUTTON' and obj in points and placement is not None:
            serial, _, code, state = map(int, fields[4:8])
            if code != 272:
                continue
            x, y = points[obj]
            if not (0 <= x < placement[2] * 256 and 0 <= y < placement[3] * 256):
                pressed.pop(obj, None)
                continue
            point = [placement[0] + x / 256, placement[1] + y / 256]
            if state == 1:
                pressed[obj] = (serial, point)
            elif state == 0 and obj in pressed:
                start_serial, start = pressed.pop(obj)
                clicks.append(
                    {
                        'pointer': obj,
                        'downSerial': start_serial,
                        'upSerial': serial,
                        'downOnDesktop': start,
                        'upOnDesktop': point,
                    }
                )
    return clicks


def report(source):
    identity = json.loads((source / 'identity.json').read_text())
    journal = source / 'owner.log'
    require(
        identity['binarySha256'] == ORIGINAL_SHA256 and identity['target'] == 'app://-/index.html',
        'unexpected original process',
    )
    require((source / 'controller-closed').is_file(), 'finish the private session before reading its journal')
    witness = Journal(journal)
    try:
        witness.update()
    finally:
        if witness.file:
            witness.file.close()
    pid = identity['pid']
    roles = [
        (c, witness.roles[c, witness.tops[c, top]])
        for (c, top), app in witness.ids.items()
        if app == 'chatgpt'
    ]
    require(len(roles) == 1 and roles[0][1][0] == pid, 'missing unique original Wayland toplevel')
    client, (_, surface) = roles[0]
    owner = (client, pid, identity['startTicks'], identity['device'], identity['inode'], surface)
    commits = {
        info['serial']
        for kind, _, info in witness.origins.values()
        if kind == 'SHM'
        and info.get('cogl')
        and info.get('gl', (None, None))[1] == 3553
        and info.get('shm', ())[:2] == (4360, 760)
        and witness.peers.get(info['serial'], ())[:-1] == owner
        and (client, pid, surface, info['serial']) in witness.commits
    }
    require(commits, 'missing completed compositor ownership chain')
    with journal.open(encoding='ascii') as log:
        clicks = observed_clicks(
            log,
            client=client,
            pid=identity['pid'],
            surface=surface,
            verified_commits=commits,
        )
    return {
        'process': identity['pid'],
        'connection': client,
        'surface': surface,
        'observedLeftClicks': clicks,
    }


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('source', type=Path)
    print(json.dumps(report(cli.parse_args().source), indent=2))
