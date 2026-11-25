package main

import (
	"context"
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

// Exercise the real console handler on Windows without sending a signal to the user's terminal.
func TestSetupCtrlC(t *testing.T) {
	const role = "CLOUD_WORKSPACE_TEST_CTRLC_ROLE"
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	args := []string{"-test.run=^TestSetupCtrlC$"}
	switch os.Getenv(role) {
	case "launcher":
		// The test launcher was created with CREATE_NEW_CONSOLE. Allocating a fresh
		// console also resets Go's runtime handler, as in the GUI EXE's normal path.
		if ok, _, _ := kernel.NewProc("FreeConsole").Call(); ok == 0 {
			t.Fatal("FreeConsole")
		}
		if ok, _, _ := kernel.NewProc("AllocConsole").Call(); ok == 0 {
			t.Fatal("AllocConsole")
		}
		env := append(os.Environ(), role+"=wsl", role+"_PARENT="+strconv.Itoa(os.Getpid()))
		got := runSetup(exe, args, env)
		if uint32(got.exit) != 0xc000013a {
			t.Fatalf("child exit = 0x%08X; want STATUS_CONTROL_C_EXIT", uint32(got.exit))
		}
	case "wsl":
		var clients [3]uint32
		n, _, _ := kernel.NewProc("GetConsoleProcessList").Call(uintptr(unsafe.Pointer(&clients[0])), uintptr(len(clients)))
		self, parent := uint32(os.Getpid()), uint32(os.Getppid())
		if os.Getenv(role+"_PARENT") != strconv.Itoa(int(parent)) || n != 2 || !((clients[0] == self && clients[1] == parent) || (clients[1] == self && clients[0] == parent)) {
			t.Fatalf("expected only the isolated parent and child in this console")
		}
		if ok, _, _ := kernel.NewProc("GenerateConsoleCtrlEvent").Call(syscall.CTRL_C_EVENT, 0); ok == 0 {
			t.Fatal("GenerateConsoleCtrlEvent")
		}
		time.Sleep(4 * time.Second)
		t.Fatal("the child remained alive after Ctrl+C")
	default:
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, exe, args...)
		cmd.Env = append(os.Environ(), role+"=launcher")
		cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x10} // CREATE_NEW_CONSOLE
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("isolated console test: %v\n%s", err, output)
		}
	}
}
