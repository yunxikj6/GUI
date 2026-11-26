"""Collect two stable signed-out original application states on the dedicated cloud source."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageGrab

from cloud_render.chatgpt_process import APP_PAGE, open_chatgpt
from cloud_render.chromium_capture import _exclusive_page, _read_trace, capture_frame
from cloud_render.wire import Frame, encode_message

from .controls import ORIGINAL_SHA256, caption_layers, require


def business_digest(image):
    """Identity check for two source-owned observations. No image leaves the source."""
    rgb = image.convert('RGB')
    require(rgb.size == (1090, 760), 'unsupported application size')
    h = hashlib.sha256(rgb.crop((0, 0, 1026, 36)).tobytes())
    h.update(rgb.crop((0, 36, 1090, 760)).tobytes())
    return h.hexdigest()


def window_origin(desktop, app):
    """Locate this test's signed-out page on its private Xvfb for real pointer hover."""
    desktop, app = desktop.convert('RGB'), app.convert('RGB')
    width, height = desktop.size
    pixels = desktop.tobytes()
    candidates = None
    for y in range(80, app.height - 80):
        x = app.width // 2 - 90
        sample = app.crop((x, y, x + 180, y + 1)).tobytes()
        if len(set(sample)) < 12:
            continue
        matches = set()
        pos = pixels.find(sample)
        while pos >= 0:
            if pos % 3 == 0:
                sy, sx = divmod(pos // 3, width)
                dx, dy = sx - x, sy - y
                if 0 <= dx <= width - app.width and 0 <= dy <= height - app.height:
                    matches.add((dx, dy))
            pos = pixels.find(sample, pos + 1)
        if not matches:
            continue
        candidates = matches if candidates is None else candidates & matches
        if candidates is not None and len(candidates) == 1:
            return candidates.pop()
    raise RuntimeError('could not locate the unique original signed-out window on its private display')


async def geometry(page):
    g = await page.evaluate("""() => { const o = navigator.windowControlsOverlay;
        const r = o?.getTitlebarAreaRect(); return [innerWidth, innerHeight, devicePixelRatio,
        o?.visible && r.x===0 && r.y===0 ? r.right : 0, r?.height,
        matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light']; }""")
    return tuple(g)


def drive(out):
    """Run source-side X input on the private desktop, apart from the app."""
    until = time.monotonic() + 90
    while not (out / 'hover.png').exists():
        require(time.monotonic() < until, 'timed out waiting for original window')
        time.sleep(0.1)
    xid = (out / 'nested-window').read_text().strip()
    photo = Image.open(out / 'hover.png')
    info = subprocess.check_output(['xwininfo', '-id', xid], timeout=5, text=True)
    observed = dict(
        re.findall(
            r'^\s*(Absolute upper-left X|Absolute upper-left Y|Width|Height):\s*(-?\d+)\s*$',
            info,
            re.MULTILINE,
        )
    )
    require(observed.get('Width') == '1280' and observed.get('Height') == '900', 'unexpected private monitor')
    left, top = int(observed['Absolute upper-left X']), int(observed['Absolute upper-left Y'])
    desktop = ImageGrab.grab(bbox=(left, top, left + 1280, top + 900), xdisplay=os.environ['DISPLAY'])
    x, y = window_origin(desktop, photo)
    (out / 'pointer-location.json').write_text(
        json.dumps({'viewportOriginInNestedWindow': [x, y], 'nestedWindow': xid})
    )
    (out / 'hover.png').unlink()
    subprocess.run(['xdotool', 'windowfocus', '--sync', xid], timeout=4, check=True)
    for dx, dy in ((300, 88), (1074, 18), (1041, 18), (300, 88)):
        subprocess.run(
            ['xdotool', 'mousemove', '--window', xid, str(x + dx), str(y + dy)], timeout=3, check=True
        )
        time.sleep(0.45)
    (out / 'hover-finished').touch()


async def titlebar(top, page, pid, out, *, interactive=False):
    before = None if interactive else await geometry(page)
    photo = None if interactive else await page.screenshot()
    done = asyncio.get_running_loop().create_future()
    top.once('Tracing.tracingComplete', lambda result: done.set_result(result))
    await top.send(
        'Tracing.start',
        {
            'transferMode': 'ReturnAsStream',
            'categories': '-*,views,ui,cc,disabled-by-default-devtools.timeline.layers,'
            'disabled-by-default-devtools.timeline.picture,disabled-by-default-cc.debug.picture,disabled-by-default-viz.quads',
        },
    )
    try:
        if interactive:
            # Opening the stock app changes its blank first window to the
            # signed-out view. Observe the native browser compositor activation.
            await page.get_by_text('Sign in another way', exact=True).wait_for(timeout=30000)
            await asyncio.sleep(1.2)
        else:
            (out / '.hover.tmp').write_bytes(photo)
            (out / '.hover.tmp').replace(out / 'hover.png')
            async with asyncio.timeout(20):
                while not (out / 'hover-finished').exists():
                    await asyncio.sleep(0.1)
    finally:
        await top.send('Tracing.end')
    complete = await asyncio.wait_for(done, 15)
    require(not complete.get('dataLossOccurred'), 'incomplete native trace')
    events = await _read_trace(top, complete['stream'])
    from collections import Counter

    (out / 'trace-counts.json').write_text(
        json.dumps(
            {
                'browser': pid,
                'nativeTraceCounts': dict(
                    Counter(
                        e.get('name')
                        for e in events
                        if e.get('pid') == pid
                        and e.get('name')
                        in (
                            'Widget::OnMouseEvent',
                            'cc::DisplayItemList:snapshot',
                            'LayerTreeHostImpl:snapshot',
                        )
                    )
                ),
            }
        )
    )
    final_geometry = await geometry(page)
    require(
        (before is None or before == final_geometry) and _exclusive_page(page), 'window changed during trace'
    )
    return caption_layers(events, browser_pid=pid, geometry=final_geometry)


def _file_sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def publish(path, value):
    temp = path.with_name('.' + path.name)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output)
    temp.replace(path)


async def observe_interactive(page, out, native_layers):
    """Repaint verified, signed-out states as the user's original window changes."""
    source = out / 'drawings'
    source.mkdir(mode=0o700)
    observed = {}
    sequence = 0
    while not (out / 'controller-closed').exists() and not page.is_closed():
        if not _exclusive_page(page) or page.url != APP_PAGE:
            return
        known = None
        for label, marker in [('after', 'Amazon Bedrock'), ('before', 'Sign in another way')]:
            if await page.get_by_text(marker, exact=True).is_visible():
                known = label
                break
        if known and await geometry(page) == (1090, 760, 1, 1026, 36, 'light'):
            start = (out / 'owner.log').stat().st_size
            digest = business_digest(Image.open(BytesIO(await page.screenshot())))
            if digest not in observed:
                publish(out / 'pending-scene.json', {'businessRgbSha256': digest})
                frame = await capture_frame(page, sequence + 1)
                end_digest = business_digest(Image.open(BytesIO(await page.screenshot())))
                if digest == end_digest and await geometry(page) == (1090, 760, 1, 1026, 36, 'light'):
                    raw = encode_message(
                        Frame(frame.sequence, frame.width, frame.height, frame.layers + native_layers)
                    )
                    picture = hashlib.sha256(raw).hexdigest()
                    require(len(observed) < 32, 'signed-out scene limit reached; start a new session')
                    (source / (picture + '.clrp')).write_bytes(raw)
                    sequence += 1
                    observed[digest] = {'phase': known, 'pictureSha256': picture, 'witnessByteStart': start}
                    publish(out / 'scenes.json', observed)
        await asyncio.sleep(0.4)


async def collect(out, *, live=False, interactive=False):
    require(
        out.is_dir() and out.stat().st_uid == os.getuid() and not out.stat().st_mode & 0o077,
        'source output must be private',
    )
    installed = Path('/usr/lib/chatgpt/ChatGPT')
    digest = await asyncio.to_thread(_file_sha256, installed)
    require(digest == ORIGINAL_SHA256, 'this source has an unverified vendor executable')
    xid = (out / 'nested-window').read_text().strip()
    require(
        xid.isdigit() or (xid.startswith('0x') and all(c in '0123456789abcdef' for c in xid[2:])),
        'invalid private display window',
    )
    async with open_chatgpt(out / 'profile', executable=out / 'bin' / 'wayland-original') as page:
        require(page.url == APP_PAGE and _exclusive_page(page), 'expected unique original page')
        browser = await page.context.browser.new_browser_cdp_session()
        details = await browser.send('SystemInfo.getProcessInfo')
        parents = [p['id'] for p in details['processInfo'] if p['type'] == 'browser']
        require(len(parents) == 1, 'ambiguous original process')
        pid = parents[0]
        proc = Path('/proc') / str(pid)
        require((proc / 'exe').samefile(installed), 'private pipe and installed process differ')
        stat = installed.stat()
        identity = {
            'pid': pid,
            'startTicks': int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]),
            'device': stat.st_dev,
            'inode': stat.st_ino,
            'binarySha256': digest,
            'target': APP_PAGE,
        }
        publish(out / 'identity.json', identity)
        if not interactive:
            await page.get_by_text('Sign in another way', exact=True).wait_for(timeout=30000)
            await asyncio.sleep(2)
        layers = await titlebar(browser, page, pid, out, interactive=interactive)
        if interactive:
            try:
                await observe_interactive(page, out, layers)
            finally:
                await browser.detach()
            return
        states = {}
        for sequence, (name, label) in enumerate(
            (('before', 'Sign in another way'), ('after', 'Amazon Bedrock')), 1
        ):
            target = page.get_by_text(label, exact=True)
            await target.wait_for(timeout=0 if interactive else (180000 if live else 15000))
            await asyncio.sleep(0.8)
            require(await geometry(page) == (1090, 760, 1, 1026, 36, 'light'), 'application changed geometry')
            start_log = (out / 'owner.log').stat().st_size
            before = business_digest(Image.open(BytesIO(await page.screenshot())))
            frame = await capture_frame(page, sequence)
            after = business_digest(Image.open(BytesIO(await page.screenshot())))
            require(
                before == after and page.url == APP_PAGE and _exclusive_page(page),
                'application changed while its picture was captured',
            )
            payload = encode_message(Frame(sequence, frame.width, frame.height, frame.layers + layers))
            (out / (name + '.clrp')).write_bytes(payload)
            states[name] = {
                'businessRgbSha256': before,
                'pictureSha256': hashlib.sha256(payload).hexdigest(),
                'witnessByteStart': start_log,
                'witnessByteEnd': (out / 'owner.log').stat().st_size,
            }
            publish(out / (name + '.json'), states[name])
            if name == 'before' and not (live or interactive):
                await target.click()
        await browser.detach()
        (out / 'capture.json').write_text(json.dumps({'identity': identity, 'states': states}, indent=2))
        if interactive:
            while not (out / 'controller-closed').exists() and not page.is_closed():
                await asyncio.sleep(0.15)
        elif live:
            # Keep the original native window open while the client renders the
            # last menu commit. The dedicated session owner terminates the source.
            await asyncio.sleep(120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--drive', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--interactive', action='store_true')
    args = parser.parse_args()
    if args.drive:
        drive(args.output)
    else:
        asyncio.run(collect(args.output, live=args.live, interactive=args.interactive))


if __name__ == '__main__':
    main()
