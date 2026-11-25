package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"
	"unsafe"
)

var kernel = syscall.NewLazyDLL("kernel32.dll")
var getSystemWindowsDirectory = kernel.NewProc("GetSystemWindowsDirectoryW")
var attachConsole = kernel.NewProc("AttachConsole")
var messageBox = syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW")

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
	kernel.NewProc("AllocConsole").Call() // already having a console is fine
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
	interrupts := make(chan os.Signal, 1)
	signal.Notify(interrupts, os.Interrupt) // WSL gets the same control event and can clean up.
	defer signal.Stop(interrupts)
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
		report("Ubuntu GNOME 桌面依赖已就绪。双击 Start.cmd 打开工作区。", false, false)
	}
	if opt.check {
		report("Ubuntu、WSLg 和本机工作区依赖检查通过。", false, true)
	}
}
