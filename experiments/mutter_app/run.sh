#!/usr/bin/env bash
# Run only on the source Ubuntu machine with its licensed original app installed.
set -eu -o pipefail
umask 077

if [[ ${1:-} != --inside && ${1:-} != --session ]]; then
    src=$(cd "$(dirname "$0")/../.." && pwd -P)
    live=0
    ipc=()
    profile=()
    if [[ $# = 5 && ${1:-} = --interactive && $4 = --profile-fd && $5 =~ ^[0-9]+$ ]]; then
        profile=(--bind-fd "$5" /run/cw-work/persistent --setenv CW_APP_PROFILE /run/cw-work/persistent/app)
        set -- "$1" "$2" "$3"
    fi
    if [[ ( ${1:-} = --live || ${1:-} = --interactive ) && $# = 3 ]]; then
        if [[ $1 = --interactive ]]; then mode=interactive; else mode=proof; fi
        channel=$(realpath -m -- "$2")
        shift 2
        (cd "$src" && python3 -m experiments.mutter_app.live open "$channel")
        channel=$(cd "$channel" && pwd -P)
        ipc=(--bind "$channel" /run/cw-work/pair)
        live=$mode
    fi
    [[ $# = 1 ]] || { echo 'usage: run.sh [--live|--interactive NEW_PAIR_DIRECTORY] NEW_SOURCE_ONLY_OUTPUT_DIRECTORY' >&2; exit 2; }
    # mkdir, without -p, refuses to reuse or overwrite another session.
    mkdir -m 700 -- "$1"
    out=$(cd "$1" && pwd -P)
    python=$(command -v python3)
    if [[ -n ${CW_MUTTER_PREBUILD:-} ]]; then
        # Optional operator-owned build of this exact source for x86-64 Noble.
        cmp "$src/experiments/mutter_app/mutter_witness.c" "$CW_MUTTER_PREBUILD/mutter_witness.c"
        (cd "$CW_MUTTER_PREBUILD" && sha256sum --check --strict SHA256SUMS)
        install -m 500 "$CW_MUTTER_PREBUILD/witness.so" "$out/witness.so"
    fi
    # Keep a source venv usable when the cloud worker lives under a temporary
    # path. Rebind only its interpreter prefixes into the private namespace.
    mapfile -t prefixes < <("$python" -c 'import sys; print(sys.base_prefix); print(sys.prefix)')
    binds=()
    for prefix in "${prefixes[@]}"; do
        case "$prefix" in
            /tmp/*|/run/*|/home/*|/root/*) binds+=(--ro-bind "$prefix" "$prefix");;
        esac
    done
    exec bwrap --die-with-parent --unshare-user --unshare-net --unshare-pid --unshare-ipc \
        --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --tmpfs /run --tmpfs /home --tmpfs /root "${binds[@]}" \
        --dir /run/cw-work --ro-bind "$src" /run/cw-work/source --bind "$out" /run/cw-work/result "${ipc[@]}" --clearenv "${profile[@]}" \
        --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv LANG C.UTF-8 --setenv LC_ALL C.UTF-8 \
        --setenv HOME /run/cw-work/result/home --setenv CW_PYTHON "$python" --setenv CW_LIVE "$live" --chdir /run/cw-work/source \
        -- /bin/bash /run/cw-work/source/experiments/mutter_app/run.sh --inside
fi

out=/run/cw-work/result
export XDG_RUNTIME_DIR=/tmp/cw-session
export XDG_CURRENT_DESKTOP=ubuntu:GNOME DESKTOP_SESSION=ubuntu XDG_SESSION_TYPE=wayland
export GNOME_SHELL_SESSION_MODE=ubuntu GSETTINGS_BACKEND=keyfile
export XDG_DATA_DIRS=/usr/share/ubuntu:/usr/local/share:/usr/share
export LIBGL_ALWAYS_SOFTWARE=1 LP_NUM_THREADS=2 MUTTER_DEBUG_DUMMY_MODE_SPECS=1280x900
export DBUS_SYSTEM_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/system
export PATH=$out/bin:/usr/local/bin:/usr/bin:/bin

if [[ $1 = --inside ]]; then
    mkdir -m 700 -p "$HOME/Desktop" "$XDG_RUNTIME_DIR" "$out/bin"
    test -x /usr/bin/chatgpt && test -x /usr/lib/chatgpt/ChatGPT
    grep -Fxq 'Exec=chatgpt %U' /usr/share/applications/chatgpt.desktop
    if [[ ! -f "$out/witness.so" ]]; then
        gcc -std=gnu11 -shared -fPIC -O2 -Wl,-z,relro,-z,now -o "$out/witness.so" \
            experiments/mutter_app/mutter_witness.c -ldl -lcrypto -pthread
    fi
    cat >"$out/bin/wayland-original" <<'EOF'
#!/bin/sh
unset DISPLAY WAYLAND_SOCKET GALLIUM_DRIVER VTEST_SOCKET_NAME CW_MUTTER_WITNESS
export WAYLAND_DISPLAY=gnome-vgpu
exec /usr/bin/chatgpt --ozone-platform=wayland "$@"
EOF
    cat >"$out/bin/chatgpt" <<'EOF'
#!/bin/sh
cd /run/cw-work/source || exit 1
: > /run/cw-work/result/broker-started
unset GALLIUM_DRIVER VTEST_SOCKET_NAME CW_MUTTER_WITNESS
export WAYLAND_DISPLAY=gnome-vgpu
case "$CW_LIVE" in interactive) set -- --interactive --pair /run/cw-work/pair;; proof) set -- --live;; *) set --;; esac
exec "$CW_PYTHON" -m experiments.mutter_app.capture /run/cw-work/result "$@" > /run/cw-work/result/broker.log 2>&1
EOF
    chmod 700 "$out/bin/"{chatgpt,wayland-original}
    cat >"$XDG_RUNTIME_DIR/system.conf" <<'EOF'
<busconfig><type>system</type><listen>unix:path=/tmp/cw-session/system</listen><auth>EXTERNAL</auth><policy context="default"><allow user="*"/><allow send_destination="*"/><allow receive_sender="*"/><allow own="*"/></policy></busconfig>
EOF
    dbus-daemon --nofork --config-file="$XDG_RUNTIME_DIR/system.conf" >"$out/system.log" 2>&1 &
    sysbus=$!
    trap 'kill "$sysbus" 2>/dev/null || true' EXIT
    xvfb-run -a -s '-screen 0 1400x1000x24 -nolisten tcp +extension GLX' \
        dbus-run-session -- bash experiments/mutter_app/run.sh --session
    exit
fi

gpu= relay= shell= driver= transport= pointer=
cleanup() {
    for pid in "$pointer" "$driver" "$shell" "$transport" "$relay" "$gpu"; do
        [[ -z $pid ]] || kill "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT
wait_for() {
    local until_time=$((SECONDS+$1))
    shift
    until "$@"; do
        if (( SECONDS >= until_time )); then echo "timed out waiting for $1" >&2; return 1; fi
        sleep .12
    done
}
# This controlled experiment supports one application surface. The independent
# Desktop Icons NG client otherwise publishes a full desktop SHM surface.
gsettings set org.gnome.shell disabled-extensions "['ding@rastersoft.com']"
gsettings set org.gnome.shell favorite-apps "['chatgpt.desktop']"
gdbus call --session --dest org.freedesktop.secrets --object-path /org/freedesktop/secrets \
    --method org.freedesktop.Secret.Service.SetAlias default /org/freedesktop/secrets/collection/session >/dev/null
virgl_test_server --no-loop-or-fork --use-egl-surfaceless --socket-path "$XDG_RUNTIME_DIR/gpu" >"$out/gpu.log" 2>&1 &
gpu=$!
wait_for 8 test -S "$XDG_RUNTIME_DIR/gpu"
manual=()
if [[ $CW_LIVE = interactive ]]; then manual=(--interactive); fi
mode=()
raw="$out/commands.virgl"
if [[ $CW_LIVE != 0 ]]; then
    mode=(--live)
    raw="$XDG_RUNTIME_DIR/source-stream"
    mkfifo -m 600 "$raw"
    "$CW_PYTHON" -m experiments.mutter_app.live stream /run/cw-work/pair --source "$out" "${manual[@]}" < "$raw" 2>"$out/transport.log" &
    transport=$!
fi
"$CW_PYTHON" -m experiments.gnome_command_stream record --listen "$XDG_RUNTIME_DIR/source" \
    --gpu "$XDG_RUNTIME_DIR/gpu" "${mode[@]}" > "$raw" 2>"$out/relay.log" &
relay=$!
wait_for 8 test -S "$XDG_RUNTIME_DIR/source"
env CW_MUTTER_WITNESS="$out/owner.log" GALLIUM_DRIVER=virpipe VTEST_SOCKET_NAME="$XDG_RUNTIME_DIR/source" \
    /lib64/ld-linux-x86-64.so.2 --preload "$out/witness.so" /usr/bin/gnome-shell \
    --mode=ubuntu --nested --wayland --no-x11 --wayland-display=gnome-vgpu >"$out/shell.log" 2>&1 &
shell=$!
wait_for 20 test -S "$XDG_RUNTIME_DIR/gnome-vgpu"
visible() {
    local id properties window found=
    while read -r id; do
        properties=$(xprop -id "$id" _NET_WM_PID _NET_WM_NAME 2>/dev/null) || continue
        grep -Fxq "_NET_WM_PID(CARDINAL) = $shell" <<<"$properties" || continue
        grep -Fxq '_NET_WM_NAME(UTF8_STRING) = "gnome-shell"' <<<"$properties" || continue
        window=$(xwininfo -id "$id" 2>/dev/null) || continue
        [[ $(awk '/^ *Width:/ { w=$2 } /^ *Height:/ { h=$2 }
                  /^ *Map State:/ { state=$3 } END { print w,h,state }' <<<"$window") == '1280 900 IsViewable' ]] || continue
        [[ -z $found ]] || return 1
        found=$id
    done < <(xwininfo -root -children | sed -nE 's/^[[:space:]]*(0x[[:xdigit:]]+) .*/\1/p')
    [[ -n $found ]] || return 1
    echo "$found" >"$out/nested-window"
}
wait_for 25 visible
xid=$(cat "$out/nested-window")
xdotool windowfocus --sync "$xid"
dock() {
    gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell \
        --method org.gnome.Shell.Extensions.GetExtensionInfo ubuntu-dock@ubuntu.com 2>/dev/null | grep -q "'state': <1.0>"
}
wait_for 20 dock
if [[ $CW_LIVE = interactive ]]; then
    # No scripted launcher, pointer movement, app click or image recognition.
    # Pipe EOF represents the user's window closing.
    "$CW_PYTHON" -m experiments.mutter_app.live input /run/cw-work/pair --source "$out" --interactive 2>"$out/pointer.log" &
    pointer=$!
    wait -n "$pointer" "$transport" "$relay" "$shell" || true
    wait_for 2 test -e "$out/controller-closed" || { echo 'The desktop transport or source closed.' >&2; exit 1; }
    if [[ -n ${CW_APP_PROFILE:-} && -f "$out/broker-started" ]]; then
        # Closing the compositor first can interrupt Chromium's per-profile
        # writes even after the controller has disconnected.
        wait_for 25 test -e "$out/app-profile-closed"
    fi
    pointer=
else
    "$CW_PYTHON" -m experiments.mutter_app.capture --drive "$out" >"$out/input.log" 2>&1 &
    driver=$!
    if [[ $CW_LIVE = proof ]]; then
        timeout 200 "$CW_PYTHON" -m experiments.mutter_app.live input /run/cw-work/pair --source "$out" 2>"$out/click.log" &
        pointer=$!
    fi
    for _ in 1 2 3 4 5; do
        xdotool mousemove --window "$xid" 31 68 click 1
        if wait_for 4 test -e "$out/broker-started" 2>/dev/null; then break; fi
    done
    wait_for 6 test -e "$out/broker-started"
    wait "$driver"
    driver=
    if [[ $CW_LIVE = proof ]]; then
        wait "$pointer"
        pointer=
        wait_for 60 test -s "$out/capture.json"
        wait_for 60 test -s "$out/forwarded-after.json"
        sleep 2
    else
        wait_for 50 test -s "$out/capture.json"
    fi
fi
kill -TERM "$shell"
wait "$shell" || true
shell=
if [[ $CW_LIVE != interactive ]]; then
    wait "$relay"
    relay=
    if [[ $CW_LIVE = proof ]]; then
        wait "$transport"
        transport=
    else
        "$CW_PYTHON" -m experiments.mutter_app.bind "$out"
    fi
fi
