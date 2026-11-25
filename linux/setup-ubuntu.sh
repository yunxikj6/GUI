#!/usr/bin/env bash
# Run only by the explicitly selected first-time setup action.
set -eu

check_platform() {
    local ID='' VERSION_ID=''
    . /etc/os-release
    if [[ $ID != ubuntu || $VERSION_ID != 24.04 ]]; then
        echo '此依赖清单适用于 Ubuntu 24.04 LTS。请用 --distro 选择该发行版。' >&2
        return 65
    fi
    if [[ ! -r /mnt/wslg/versions.txt ]]; then
        echo '未检测到 WSLg。请在 Windows 中检查 wsl --version 与 wsl --list --verbose。' >&2
        return 69
    fi
}

has_session_browser() {
    XDG_CURRENT_DESKTOP=ubuntu:GNOME /usr/bin/python3 -I "$(dirname -- "${BASH_SOURCE[0]}")/session_browser.py" --check 2>/dev/null
}

setup() {
    if [[ $# != 1 || $1 != --wsl ]]; then
        echo '用法：bash linux/setup-ubuntu.sh --wsl' >&2
        return 64
    fi
    check_platform || return $?
    local packages=(
        gnome-shell ubuntu-session ubuntu-settings gnome-shell-extension-ubuntu-dock
        gnome-shell-extension-appindicator gnome-shell-extension-desktop-icons-ng
        yaru-theme-gtk yaru-theme-icon ubuntu-wallpapers-noble fonts-ubuntu nautilus gvfs
        xdg-user-dirs dbus-daemon x11-utils xwayland libgl1-mesa-dri
        gnome-text-editor gnome-terminal zenity gnome-keyring python3-venv python3-keyring libnotify-bin fonts-noto-cjk ca-certificates curl gpg
    )
    # Noble's GNOME MIME defaults already select Web when no preferred browser exists.
    if ! has_session_browser; then packages+=(epiphany-browser); fi
    local missing=() package status answer
    for package in "${packages[@]}"; do
        status=$(dpkg-query -W '-f=${db:Status-Status}' -- "$package" 2>/dev/null) || status=''
        [[ $status == installed ]] || missing+=("$package")
    done
    if (( ${#missing[@]} == 0 )); then
        echo 'Ubuntu GNOME 桌面依赖已就绪。'
        return 0
    fi
    if [[ ! -t 0 || ! -t 1 ]]; then
        echo '安装需要交互终端。请在 Windows 中运行 Start.cmd --setup-native。' >&2
        return 70
    fi
    printf '即将从此 Ubuntu 已配置的 APT 源更新索引并安装缺失的桌面软件包及依赖：\n'
    printf '  %s\n' "${missing[@]}"
    if ! read -r -p '继续安装？输入 y 继续，按 Enter 取消 [y/N] ' answer || [[ $answer != [yY] && $answer != [yY][eE][sS] ]]; then
        echo '已取消。'
        return 130
    fi
    local elevate=()
    if (( EUID != 0 )); then elevate=(sudo --); fi
    "${elevate[@]}" apt-get -o APT::Update::Error-Mode=any update || return 71
    "${elevate[@]}" apt-get -o APT::Get::Assume-Yes=false --no-remove --no-install-recommends install "${missing[@]}" || return 72
    echo 'Ubuntu GNOME 桌面依赖安装完成。'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    trap 'exit 130' INT
    setup "$@"
fi
