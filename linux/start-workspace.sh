#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
wsl=false
check=false
for arg in "$@"; do
    case "$arg" in
        --wsl) wsl=true ;;
        --check) check=true ;;
        *) echo "不支持的本机工作区参数: $arg" >&2; exit 2 ;;
    esac
done
if "$wsl"; then
    . /etc/os-release
    if [[ "$ID" != ubuntu ]]; then
        echo '请选择本机 Ubuntu。运行 wsl --list --verbose 查看名称，再用 Start.cmd --distro 指定。' >&2
        exit 2
    fi
    if [[ ! -r /mnt/wslg/versions.txt ]]; then
        echo '此发行版需要 WSL 2 和本机 WSLg 图形支持。请检查 wsl --version 与 wsl --list --verbose。' >&2
        exit 2
    fi
fi
/bin/bash ./start-gnome.sh --check
if "$check"; then exit 0; fi
if "$wsl"; then
    python3 ./install-app-launchers.py
    exec /bin/bash ./start-gnome.sh --desktop-shortcuts
fi
exec /bin/bash ./start-gnome.sh
