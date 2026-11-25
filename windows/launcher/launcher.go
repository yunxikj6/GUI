// Cloud Linux Workspace starts the repository's installed Ubuntu GNOME workspace through WSL.
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

const limit = 64 * 1024
const help = `Cloud Linux Workspace

双击启动本机已安装的 Ubuntu GNOME 工作区。

  --native          使用本机工作区（默认）
  --distro 名称     使用 wsl --list --quiet 中的完整发行版名称
  --check-native    检查本机 Ubuntu、WSLg 和桌面依赖后退出
  --setup-native    在交互终端为 Ubuntu 24.04 安装缺少的 GNOME 依赖
  --open-app 名称   打开 Microsoft Store 版 ChatGPT 或 Claude；可并用 --check-native 仅检测
  --help            显示本说明`

type options struct {
	distro, app            string
	check, setup, showHelp bool
}
type failure struct {
	code, message string
	exit          int
}

func (f failure) Error() string          { return f.message }
func fail(code, message string) *failure { return &failure{code, message, -1} }

func parse(args []string) (options, *failure) {
	var opt options
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--native":
		case "--check-native":
			opt.check = true
		case "--setup-native":
			opt.setup = true
		case "--help":
			opt.showHelp = true
		case "--open-app":
			i++
			if i == len(args) || opt.app != "" || (args[i] != "ChatGPT" && args[i] != "Claude") {
				return opt, fail("arguments", "--open-app 需要指定 ChatGPT 或 Claude。")
			}
			opt.app = args[i]
		case "--distro":
			i++
			if i >= len(args) || opt.distro != "" || !validName(args[i]) {
				return opt, fail("arguments", "--distro 需要一个已安装发行版的完整名称。用 --help 查看用法。")
			}
			opt.distro = args[i]
		default:
			return opt, fail("arguments", "启动参数无法识别。用 --help 查看用法。")
		}
	}
	if opt.setup && (opt.app != "" || opt.check) {
		return opt, fail("arguments", "--setup-native 是交互式安装操作，请单独运行，可用 --distro 选择 Ubuntu。")
	}
	if opt.app != "" && opt.distro != "" {
		return opt, fail("arguments", "--open-app 使用 Windows 应用，不能与 --distro 同时使用。")
	}
	return opt, nil
}

func validName(s string) bool {
	return strings.TrimSpace(s) != "" && !strings.HasPrefix(s, "-") && !strings.ContainsAny(s, "\x00\r\n")
}

type bounded struct {
	data      []byte
	truncated bool
}

func (b *bounded) Write(p []byte) (int, error) {
	n := len(p)
	if left := limit - len(b.data); n > left {
		p = p[:left]
		b.truncated = true
	}
	b.data = append(b.data, p...)
	return n, nil
}

func decode(raw []byte) (string, bool) {
	little := len(raw) >= 2 && raw[0] == 0xff && raw[1] == 0xfe
	big := len(raw) >= 2 && raw[0] == 0xfe && raw[1] == 0xff
	if little || big {
		raw = raw[2:]
	}
	if little || big || bytes.IndexByte(raw, 0) >= 0 {
		if len(raw)%2 != 0 {
			return "", false
		}
		var order binary.ByteOrder = binary.LittleEndian
		if big {
			order = binary.BigEndian
		}
		words := make([]uint16, len(raw)/2)
		for i := range words {
			words[i] = order.Uint16(raw[i*2:])
		}
		for i := 0; i < len(words); i++ {
			w := words[i]
			if 0xD800 <= w && w <= 0xDFFF {
				if w > 0xDBFF || i+1 == len(words) || words[i+1] < 0xDC00 || words[i+1] > 0xDFFF {
					return "", false
				}
				i++
			}
		}
		return strings.TrimRight(string(utf16.Decode(words)), "\x00"), true
	}
	if !utf8.Valid(raw) {
		return "", false
	}
	return strings.TrimPrefix(string(raw), "\ufeff"), true
}

func choose(raw *bounded, requested string) (string, *failure) {
	listing, ok := decode(raw.data)
	if !ok || raw.truncated {
		return "", fail("list-format", "无法完整读取 WSL 发行版清单。请运行 wsl --list --quiet 检查本机 WSL。")
	}
	var candidates []string
	for _, line := range strings.Split(listing, "\n") {
		name := strings.TrimRight(line, "\r")
		if !validName(name) {
			continue
		}
		if requested != "" && name == requested {
			return name, nil
		}
		lower := strings.ToLower(name)
		if lower == "ubuntu" || strings.HasPrefix(lower, "ubuntu-") {
			candidates = append(candidates, name)
		}
	}
	if requested != "" {
		return "", fail("distro-missing", "未找到指定的发行版。请用 wsl --list --quiet 核对完整名称。")
	}
	if len(candidates) == 0 {
		return "", fail("no-ubuntu", "未发现已安装的 Ubuntu。若 Ubuntu 使用自定义名称，请用 --distro 指定。")
	}
	if len(candidates) > 1 {
		return "", fail("multiple-ubuntu", "本机有多个 Ubuntu 发行版。请运行 wsl --list --quiet，然后用 --distro 指定完整名称。")
	}
	return candidates[0], nil
}

func regular(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}
func systemWSL(root string) string {
	if !filepath.IsAbs(root) {
		return ""
	}
	for _, dir := range []string{"Sysnative", "System32"} {
		candidate := filepath.Join(root, dir, "wsl.exe")
		if regular(candidate) {
			return candidate
		}
	}
	return ""
}
func project(exe string) string {
	dir := filepath.Dir(exe)
	for _, root := range []string{dir, filepath.Dir(dir)} {
		if regular(filepath.Join(root, "linux", "start-workspace.sh")) {
			return root
		}
	}
	return ""
}
func childEnv(env []string) []string {
	out := make([]string, 0, len(env))
	for _, pair := range env {
		key, value, ok := strings.Cut(pair, "=")
		if strings.EqualFold(key, "INSTABOX_API_KEY") {
			continue
		}
		if ok && strings.EqualFold(key, "WSLENV") {
			keep := []string{}
			for _, entry := range strings.Split(value, ":") {
				name, _, _ := strings.Cut(entry, "/")
				if entry != "" && !strings.EqualFold(name, "INSTABOX_API_KEY") {
					keep = append(keep, entry)
				}
			}
			pair = key + "=" + strings.Join(keep, ":")
		}
		out = append(out, pair)
	}
	return out
}

type runResult struct {
	stdout, stderr bounded
	exit           int
	timedOut       bool
}
type runMode int

const (
	workspaceRun runMode = iota
	listRun
	setupRun
)

type runner func(program string, args, env []string, mode runMode) runResult

func execute(opt options, exe, windows string, env []string, run runner) *failure {
	root := project(exe)
	if root == "" {
		return fail("package-layout", "找不到 linux/start-workspace.sh。请将启动器和项目一起解压，再从项目目录或 windows 子目录运行。")
	}
	wsl := systemWSL(windows)
	if wsl == "" {
		return fail("no-wsl", "Windows 系统目录中找不到 wsl.exe。请运行 wsl --status 检查本机 WSL。")
	}
	env = childEnv(env)
	list := run(wsl, []string{"--list", "--quiet"}, env, listRun)
	if list.timedOut {
		return fail("list-timeout", "获取发行版清单超时。请在 PowerShell 中运行 wsl --status。")
	}
	if list.exit != 0 {
		return &failure{"list-failed", "WSL 未能列出已安装的发行版。请运行 wsl --list --verbose 检查状态。", list.exit}
	}
	distro, err := choose(&list.stdout, opt.distro)
	if err != nil {
		return err
	}
	script, mode := "./linux/start-workspace.sh", workspaceRun
	if opt.setup {
		script, mode = "./linux/setup-ubuntu.sh", setupRun
		if !regular(filepath.Join(root, "linux", "setup-ubuntu.sh")) {
			return fail("package-layout", "找不到 linux/setup-ubuntu.sh，请重新解压完整项目。")
		}
	}
	args := []string{"--distribution", distro, "--cd", root, "--exec", "/bin/bash", script, "--wsl"}
	if opt.check {
		args = append(args, "--check")
	}
	res := run(wsl, args, env, mode)
	if opt.setup {
		switch {
		case res.exit == 0:
			return nil
		case res.exit == 130 || uint32(res.exit) == 0xc000013a:
			return fail("setup-cancelled", "已取消 Ubuntu 桌面依赖安装。")
		case res.exit == 65:
			return fail("setup-version", "此依赖清单仅支持 Ubuntu 24.04 LTS。请用 --distro 指定该发行版。")
		case res.exit == 69:
			return fail("setup-wslg", "未检测到 WSLg。请在 Windows 中运行 wsl --version 与 wsl --list --verbose 检查。")
		case res.exit == -1 || res.exit == 70:
			return fail("setup-console", "无法启动 WSL 交互终端。请从 Windows 终端重试 Start.cmd --setup-native。")
		default:
			return &failure{"setup-exit", "Ubuntu 桌面依赖准备未完成。请查看安装控制台中的提示，处理后重新运行 --setup-native。", res.exit}
		}
	}
	if res.timedOut {
		return fail("check-timeout", "Ubuntu 工作区检查超时。请在所选 Ubuntu 中运行 bash ./linux/start-workspace.sh --wsl --check。")
	}
	if res.exit == 0 {
		return nil
	}
	// Show only recognized diagnostics. Raw output may include account details or project paths.
	msg := "Ubuntu 工作区未能正常运行。请用 --check-native 检查依赖，并在所选 Ubuntu 中运行 bash ./linux/start-workspace.sh --wsl --check 查看详情。"
	text, _ := decode(res.stderr.data)
	normal := strings.ToLower(text)
	switch {
	case strings.Contains(normal, "请选择本机 ubuntu"):
		msg = "所选发行版不是 Ubuntu。请使用 --distro 选择本机 Ubuntu。"
	case strings.Contains(normal, "wslg"):
		msg = "所选 Ubuntu 未能使用 WSL 2 与 WSLg。请运行 wsl --version 和 wsl --list --verbose 检查。"
	case strings.Contains(normal, "gnome"), strings.Contains(normal, "gtk"):
		msg = "Ubuntu 的 GNOME 图形环境或应用依赖未就绪。请在所选 Ubuntu 的项目目录运行 bash ./linux/start-workspace.sh --wsl --check。"
	}
	return &failure{"workspace-exit", msg, res.exit}
}

func saveFailure(dir, report string) error {
	if dir == "" {
		return fs.ErrInvalid
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".launcher-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	_, werr := tmp.WriteString(report)
	cerr := tmp.Close()
	if err = errors.Join(werr, cerr); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), filepath.Join(dir, "launcher-error.log"))
}
