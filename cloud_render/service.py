"""Serve a single cloud-owned application over a paired, forwarded loopback port."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import hmac
import os
from pathlib import Path
import re
import secrets
import sys

from .wire import Click, ClickAck, WireError, async_read_message, encode_message, validate_click


AUTH_PREFIX = b'CLRP-AUTH/1 '
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_-]{43}\Z')


def same_scene(left, right):
    return left.width == right.width and left.height == right.height and left.layers == right.layers


class Session:
    """An application survives viewer disconnects. Only one paired viewer owns input."""

    def __init__(self, page, capture, token, *, poll_seconds=1.0):
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError('无效的绘图会话配对码。')
        self.page = page
        self.capture = capture
        self.expected = AUTH_PREFIX + token.encode('ascii') + b'\n'
        self.busy = False
        self.sequence = 0
        self.poll_seconds = poll_seconds
        self.connections = set()

    async def next_frame(self):
        if self.sequence >= 0xffffffff:
            raise RuntimeError('绘图会话序号已用尽。请重启云端会话。')
        return await asyncio.wait_for(self.capture(self.page, self.sequence + 1), 20)

    async def send(self, writer, frame):
        writer.write(encode_message(frame))
        await asyncio.wait_for(writer.drain(), 10)
        self.sequence = frame.sequence

    async def serve_viewer(self, reader, writer):
        current = await self.next_frame()
        await self.send(writer, current)
        incoming = asyncio.create_task(async_read_message(reader, expected=Click))
        try:
            while not self.page.is_closed():
                done, _ = await asyncio.wait({incoming}, timeout=self.poll_seconds)
                if done:
                    click = incoming.result()
                    if click is None:
                        return
                    applied = click.sequence == current.sequence
                    if applied:
                        validate_click(click, current)
                        # The cloud application may have moved a control since publication.
                        latest = await self.next_frame()
                        applied = same_scene(current, latest)
                    if applied:
                        await self.page.mouse.click(click.x, click.y)
                    writer.write(encode_message(ClickAck(click.sequence, applied)))
                    await asyncio.wait_for(writer.drain(), 10)
                    # A stale click must never act on a different page state. Send the
                    # authoritative view again, without replaying that action.
                    current = await self.next_frame()
                    await self.send(writer, current)
                    incoming = asyncio.create_task(async_read_message(reader, expected=Click))
                else:
                    fresh = await self.next_frame()
                    if not same_scene(fresh, current):
                        current = fresh
                        await self.send(writer, current)
        finally:
            incoming.cancel()
            with suppress(asyncio.CancelledError, OSError, ValueError, EOFError):
                await incoming

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        owned = False
        try:
            if len(self.connections) > 16:
                return
            given = await asyncio.wait_for(reader.readexactly(len(self.expected)), 5)
            if not hmac.compare_digest(given, self.expected) or self.busy:
                return
            self.busy = owned = True
            await self.serve_viewer(reader, writer)
        except (TimeoutError, EOFError, OSError, WireError):
            pass
        except Exception as exc:
            # Never include application content, URLs, or private tracing output in logs.
            print(f'本次云端绘图连接已结束（{type(exc).__name__}）。', file=sys.stderr)
        finally:
            if owned:
                self.busy = False
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.connections.discard(task)

    async def close(self):
        tasks = tuple(self.connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def listen(args, token):
    from .chatgpt_process import open_chatgpt
    from .chromium_capture import capture_frame

    async with open_chatgpt(args.profile, executable=args.executable) as page:
        session = Session(page, capture_frame, token)
        server = await asyncio.start_server(session.accept, '127.0.0.1', args.port, limit=1024, backlog=8)
        async with server:
            print(f'云端原程序绘图端口 {server.sockets[0].getsockname()[1]}。请将该端口私下转发到本机。', flush=True)
            try:
                await server.serve_forever()
            finally:
                await session.close()


def main():
    parser = argparse.ArgumentParser(description='在云端运行原版 ChatGPT，输出本机可重绘的界面指令。')
    parser.add_argument('--port', type=int, default=17324)
    parser.add_argument('--profile', type=Path, default=Path.home() / '.local/share/cloud-linux-workspace/render/chatgpt')
    parser.add_argument('--executable', type=Path, default=Path('/usr/bin/chatgpt'),
                        help='云端 OpenAI 官方软件包中可执行程序的绝对路径')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('端口必须在 0 至 65535 之间。')
    if not args.executable.is_absolute():
        parser.error('请输入云端原厂程序的绝对路径。')
    token = os.environ.pop('CLOUDWORKSPACE_RENDER_TOKEN', None)
    if token is None:
        if not sys.stderr.isatty():
            parser.error('后台运行时请通过 CLOUDWORKSPACE_RENDER_TOKEN 提供私有配对码。')
        token = secrets.token_urlsafe(32)
        print('本次私有配对码 ' + token, file=sys.stderr)
    if not TOKEN_PATTERN.fullmatch(token):
        parser.error('配对码须为 43 个 URL 安全字符。')
    try:
        asyncio.run(listen(args, token))
    except KeyboardInterrupt:
        pass
    except (OSError, ImportError, RuntimeError) as exc:
        print(f'云端绘图服务启动失败（{type(exc).__name__}）。请检查原厂客户端、图形会话和云端依赖。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
