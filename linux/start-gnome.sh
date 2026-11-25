#!/bin/sh
set -eu
if ! command -v python3 >/dev/null 2>&1; then
    echo '需要 Ubuntu 的 python3 才能启动 GNOME。' >&2
    exit 1
fi
exec python3 "$(dirname -- "$0")/gnome_session.py" "$@"
