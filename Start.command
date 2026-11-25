#!/bin/sh
cd -- "$(dirname -- "$0")" || exit 1
if [ "$#" -eq 0 ] && [ "$(uname -s)" = Darwin ]; then
    exec python3 launcher.py --cloud-desktop
fi
exec python3 launcher.py "$@"
