package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

var kernel = syscall.NewLazyDLL("kernel32.dll")
var getSystemWindowsDirectory = kernel.NewProc("GetSystemWindowsDirectoryW")
var attachConsole = kernel.NewProc("AttachConsole")
var setConsoleHandler = kernel.NewProc("SetConsoleCtrlHandler")
var setupHandler = syscall.NewCallback(func(event uint32) uintptr {
	if event == syscall.CTRL_C_EVENT {
		return 1
	}
	return 0
})
var messageBox = syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW")

// Match the layout of CREDUI_INFOW on both supported Windows architectures.
type credentialPromptInfo struct {
	size    uint32
	parent  uintptr
	message *uint16
	caption *uint16
	banner  uintptr
}

func withCredential(env []string, key, title, instruction, credentialTarget string) ([]string, int) {
	// A GUI-subsystem process launched from Explorer has no terminal for
	// Python's getpass. Ask for the ephemeral pairing code in CredUI.
	for i, entry := range env {
		name, value, ok := strings.Cut(entry, "=")
		if ok && strings.EqualFold(name, key) {
			if value != "" {
				return env, 0
			}
			env = append(env[:i:i], env[i+1:]...)
			break
		}
	}
	dir := windowsDirectory()
	if !filepath.IsAbs(dir) {
		return nil, 1
	}
	prompt := syscall.NewLazyDLL(filepath.Join(dir, "System32", "credui.dll")).NewProc("CredUIPromptForCredentialsW")
	if prompt.Find() != nil {
		return nil, 1
	}
	message, _ := syscall.UTF16PtrFromString(instruction)
	caption, _ := syscall.UTF16PtrFromString(title)
	target, _ := syscall.UTF16PtrFromString(credentialTarget)
	info := credentialPromptInfo{message: message, caption: caption}
	info.size = uint32(unsafe.Sizeof(info))
	username, _ := syscall.UTF16FromString("云端应用")
	var password [256]uint16
	defer clear(password[:])
	var save int32
	// GENERIC_CREDENTIALS | KEEP_USERNAME | ALWAYS_SHOW_UI | DO_NOT_PERSIST.
	// Do not offer to save or retrieve an application's cloud credential.
	result, _, _ := prompt.Call(
		uintptr(unsafe.Pointer(&info)), uintptr(unsafe.Pointer(target)), 0, 0,
		uintptr(unsafe.Pointer(&username[0])), uintptr(len(username)),
		uintptr(unsafe.Pointer(&password[0])), uintptr(len(password)),
		uintptr(unsafe.Pointer(&save)), 0x40000|0x100000|0x80|0x2,
	)
	if result == 1223 { // ERROR_CANCELLED
		return nil, 130
	}
	if result != 0 {
		return nil, 1
	}
	return append(env, key+"="+syscall.UTF16ToString(password[:])), 0
}

func windowsDirectory() string {
	buf := make([]uint16, 32768)
	count, _, _ := getSystemWindowsDirectory.Call(uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)))
	if count == 0 || count >= uintptr(len(buf)) {
		return ""
	}
	return syscall.UTF16ToString(buf[:count])
}

// With a GUI subsystem the inherited standard handles can be absent or point to NUL.
// Give WSL actual console devices and leave sudo prompts and passwords inside that console.
func runSetup(program string, args, env []string) runResult {
	// Reuse the terminal that invoked Setup.cmd when available. Explorer launches need a new one.
	if attached, _, _ := attachConsole.Call(uintptr(^uint32(0))); attached == 0 {
		kernel.NewProc("AllocConsole").Call() // Already attached is harmless.
	}
	input, err := os.OpenFile("CONIN$", os.O_RDWR, 0)
	if err != nil {
		return runResult{exit: -1}
	}
	defer input.Close()
	output, err := os.OpenFile("CONOUT$", os.O_RDWR, 0)
	if err != nil {
		return runResult{exit: -1}
	}
	defer output.Close()
	// Attaching or allocating a console resets the process control-handler table. Register
	// after that step. A non-NULL callback only handles Ctrl+C in this launcher; WSL gets
	// its own event and we wait for its exit. The NULL "ignore Ctrl+C" flag is inherited.
	if ok, _, _ := setConsoleHandler.Call(setupHandler, 1); ok == 0 {
		return runResult{exit: -1}
	}
	defer setConsoleHandler.Call(setupHandler, 0)
	cmd := exec.Command(program, args...)
	cmd.Env, cmd.Stdin, cmd.Stdout, cmd.Stderr = env, input, output, output
	if err := cmd.Run(); err != nil {
		if cmd.ProcessState != nil {
			return runResult{exit: cmd.ProcessState.ExitCode()}
		}
		return runResult{exit: -1}
	}
	return runResult{}
}

func runProcess(program string, args, env []string, mode runMode) runResult {
	if mode == setupRun {
		return runSetup(program, args, env)
	}
	if mode == rendererRun || mode == instaboxRendererRun || mode == desktopRendererRun {
		var code int
		if mode == instaboxRendererRun || mode == desktopRendererRun {
			env, code = withCredential(env, "INSTABOX_API_KEY", "连接 InstaBox", "请在密码栏输入有权连接此云机的 InstaBox API 密钥。", "CloudLinuxWorkspace.InstaBox")
			if code != 0 {
				return runResult{exit: code}
			}
		}
		if mode == desktopRendererRun {
			env, code = withCredential(env, "CLOUDWORKSPACE_GNOME_TOKEN", "连接云端 GNOME 桌面", "请输入云端 GNOME 桌面服务生成的会话配对码。", "CloudLinuxWorkspace.GNOME")
		} else {
			env, code = withCredential(env, "CLOUDWORKSPACE_RENDER_TOKEN", "连接云端应用", "请在密码栏输入云端应用绘图服务生成的本次会话配对码。", "CloudLinuxWorkspace.Render")
		}
		if code != 0 {
			return runResult{exit: code}
		}
	}
	ctx := context.Background()
	var timeout time.Duration
	if mode == listRun {
		timeout = 20 * time.Second
	} else if args[len(args)-1] == "--check" {
		timeout = 45 * time.Second
	}
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000} // CREATE_NO_WINDOW
	cmd.Env = env
	var result runResult
	cmd.Stdout = &result.stdout
	cmd.Stderr = &result.stderr
	cmd.WaitDelay = 2 * time.Second
	err := cmd.Run()
	if cmd.ProcessState != nil {
		result.exit = cmd.ProcessState.ExitCode()
	} else if err != nil {
		result.exit = -1
	}
	result.timedOut = ctx.Err() == context.DeadlineExceeded
	return result
}

func dialog(text string, failure bool) {
	caption, _ := syscall.UTF16PtrFromString("Cloud Linux Workspace")
	body, _ := syscall.UTF16PtrFromString(text)
	flag := uintptr(0x40) // MB_ICONINFORMATION
	if failure {
		flag = 0x10
	} // MB_ICONERROR
	messageBox.Call(0, uintptr(unsafe.Pointer(body)), uintptr(unsafe.Pointer(caption)), flag)
}

func report(text string, failure, cli bool) {
	file := os.Stdout
	code := syscall.STD_OUTPUT_HANDLE
	if failure {
		file = os.Stderr
		code = syscall.STD_ERROR_HANDLE
	}
	_, err := fmt.Fprintln(file, text)
	if cli && err != nil {
		// A GUI-subsystem executable can also be invoked from a terminal or pipeline.
		attachConsole.Call(uintptr(^uint32(0))) // ATTACH_PARENT_PROCESS
		if h, e := syscall.GetStdHandle(code); e == nil && h != 0 && h != syscall.InvalidHandle {
			if stream := os.NewFile(uintptr(h), "console"); stream != nil {
				_, err = fmt.Fprintln(stream, text)
			}
		}
	}
	if !cli || err != nil {
		dialog(text, failure)
	}
}

func main() {
	opt, problem := parse(os.Args[1:])
	if problem == nil && opt.showHelp {
		report(help, false, true)
		return
	}
	if problem == nil && opt.app != "" {
		var status appStatus
		status, problem = windowsOfficialApp(opt)
		if problem == nil {
			message, _ := json.Marshal(status)
			if opt.check {
				report(string(message), false, true)
			} else {
				fmt.Fprintln(os.Stdout, string(message))
			}
			return
		}
	} else if problem == nil {
		exe, err := os.Executable()
		if err != nil {
			problem = fail("executable-path", "无法定位启动器。请从解压后的项目目录运行。")
		} else {
			problem = execute(opt, exe, windowsDirectory(), os.Environ(), runProcess)
		}
	}
	if problem != nil {
		if problem.code == "render-cancelled" {
			os.Exit(problem.exit)
		}
		if problem.code == "setup-cancelled" {
			report(problem.message, false, true)
			os.Exit(130)
		}
		summary := problem.message
		if problem.exit >= 0 {
			summary += fmt.Sprintf("\n退出码 %d。", problem.exit)
		}
		if base, e := os.UserCacheDir(); e == nil {
			dir := filepath.Join(base, "CloudLinuxWorkspace")
			line := fmt.Sprintf("%s code=%s exit=%d\r\n", time.Now().UTC().Format(time.RFC3339), problem.code, problem.exit)
			if saveFailure(dir, line) == nil {
				summary += "\n本机诊断日志：" + filepath.Join(dir, "launcher-error.log")
			}
		}
		report(summary, true, opt.check || opt.showHelp || problem.code == "arguments")
		os.Exit(1)
	}
	if opt.setup {
		report("Ubuntu GNOME 桌面依赖已就绪。运行 Start.cmd --native 打开本机桌面。", false, false)
	}
	if opt.check {
		report("Ubuntu、WSLg 和本机工作区依赖检查通过。", false, true)
	}
}
