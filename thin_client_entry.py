"""Open the cloud desktop or the explicitly selected cloud application."""
import argparse
import os
import re


DEFAULT_APP_PORT = '17324'
DEFAULT_DESKTOP_PORT = '17325'


def add_arguments(parser):
    parser.add_argument('--instabox', action='store_true', help='使用 InstaBox 私有连接')
    parser.add_argument('--machine', help='InstaBox 实例 ID，只有一台时自动选择')
    from cloud_render.gnome_input import SIZE_NAMES
    parser.add_argument('--desktop-size', choices=SIZE_NAMES, help='新建 Ubuntu 桌面的尺寸')
    routes = parser.add_mutually_exclusive_group()
    routes.add_argument('--desktop-port', metavar='PORT', help='云端 GNOME 服务端口，默认 17325')
    routes.add_argument('--cloud-app', action='store_true', help='打开单独的云端 ChatGPT 原程序')
    parser.add_argument('--connect-port', metavar='PORT', help='单应用兼容模式的本地转发端口，与 --instabox 同用时为云端端口')


def start(options, parser):
    if options.machine is not None and not re.fullmatch(r'm_[A-Za-z0-9_]+', options.machine):
        parser.error('--machine 需要有效的 InstaBox 实例 ID')
    if options.desktop_port is not None and options.connect_port is not None:
        parser.error('桌面端口和单应用连接端口不能同时指定')
    app = options.cloud_app or options.connect_port is not None
    if app and options.desktop_size is not None:
        parser.error('--desktop-size 只适用于云端桌面')
    if app:
        port = options.connect_port
        direct = options.instabox or options.machine is not None
        if port is None and not direct:
            port = os.environ.get('CLOUDWORKSPACE_RENDER_PORT')
            direct = port is None
        if port is None:
            port = DEFAULT_APP_PORT
    else:
        direct = True
        port = options.desktop_port if options.desktop_port is not None else DEFAULT_DESKTOP_PORT
    if not re.fullmatch(r'[0-9]{1,5}', port) or not 1 <= int(port) <= 65535:
        parser.error('云端连接端口必须在 1 到 65535 之间')
    arguments = ['--instabox'] if direct else []
    arguments.extend(['--port', str(int(port))])
    if options.desktop_size is not None:
        arguments.extend(['--size', options.desktop_size])
    if options.machine is not None:
        arguments.extend(['--machine', options.machine])
    if app:
        from thin_client.viewer import main as open_window
    else:
        from thin_client.gnome_desktop import main as open_window
    return open_window(arguments)


def main(argv=None):
    parser = argparse.ArgumentParser(description='云端 GNOME 桌面')
    add_arguments(parser)
    return start(parser.parse_args(argv), parser)


if __name__ == '__main__':
    raise SystemExit(main())
