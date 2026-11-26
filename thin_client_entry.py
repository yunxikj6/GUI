"""Start the standalone display client for a cloud application."""
import argparse
import os
import re


DEFAULT_CLOUD_PORT = '17324'


def add_arguments(parser):
    parser.add_argument('--instabox', action='store_true', help='通过 InstaBox API 连接云端应用绘图服务')
    parser.add_argument('--connect-port', metavar='PORT', help='已转发的本机端口，与 --instabox 同用时为云端端口')
    parser.add_argument('--machine', help='InstaBox 实例 ID，只有一台时自动选择')


def start(options, parser):
    port = options.connect_port
    direct = options.instabox or options.machine is not None
    if options.machine is not None and not re.fullmatch(r'm_[A-Za-z0-9_]+', options.machine):
        parser.error('--machine 需要有效的 InstaBox 实例 ID')
    if port is None and not direct:
        port = os.environ.get('CLOUDWORKSPACE_RENDER_PORT')
        direct = port is None
    if port is None:
        port = DEFAULT_CLOUD_PORT
    if not re.fullmatch(r'[0-9]{1,5}', port) or not 1 <= int(port) <= 65535:
        parser.error('绘图服务端口必须在 1 到 65535 之间')
    arguments = ['--instabox'] if direct else []
    arguments.extend(['--port', str(int(port))])
    if options.machine is not None:
        arguments.extend(['--machine', options.machine])
    from thin_client import viewer
    return viewer.main(arguments)


def main(argv=None):
    parser = argparse.ArgumentParser(description='云端应用薄客户端')
    add_arguments(parser)
    return start(parser.parse_args(argv), parser)


if __name__ == '__main__':
    raise SystemExit(main())
