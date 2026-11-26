package main

import (
	"bytes"
	"encoding/binary"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"unicode/utf16"
)

func encoded(text string, order binary.ByteOrder, bom bool) []byte {
	words := utf16.Encode([]rune(text))
	if bom {
		words = append([]uint16{0xFEFF}, words...)
	}
	b := make([]byte, len(words)*2)
	for i, w := range words {
		order.PutUint16(b[2*i:], w)
	}
	return b
}

func TestDistributionSelection(t *testing.T) {
	for _, tc := range []struct {
		data                 []byte
		explicit, want, code string
	}{
		{[]byte("docker-desktop\r\nUbuntu-24.04\r\n"), "", "Ubuntu-24.04", ""},
		{encoded("Some Distro\r\nUbuntu\r\n", binary.LittleEndian, false), "", "Ubuntu", ""},
		{encoded("Ubuntu\r\nUbuntu-22.04\r\n", binary.LittleEndian, true), "", "", "multiple-ubuntu"},
		{encoded("Ubuntu\r\nUbuntu-22.04\r\n", binary.LittleEndian, true), "Ubuntu-22.04", "Ubuntu-22.04", ""},
		{encoded("我的 Ubuntu 🧑\r\n", binary.BigEndian, true), "我的 Ubuntu 🧑", "我的 Ubuntu 🧑", ""},
		{[]byte("\xef\xbb\xbfUbuntu\n"), "", "Ubuntu", ""},
		{[]byte("MyUbuntu\nDocker-Desktop\n"), "", "", "no-ubuntu"},
		{[]byte("Ubuntu\n"), "ubuntu", "", "distro-missing"},
		{[]byte{0xFF, 0xFE, 0, 0xD8}, "", "", "list-format"},
		{[]byte{0xFF, 0xFE, 0}, "", "", "list-format"},
	} {
		got, err := choose(&bounded{data: tc.data}, tc.explicit)
		if got != tc.want || (err != nil) != (tc.code != "") || (err != nil && err.code != tc.code) {
			t.Errorf("selection %q: got %q %v; want %q %s", tc.explicit, got, err, tc.want, tc.code)
		}
	}
}

func TestBoundedOutput(t *testing.T) {
	b := bounded{}
	io := append([]byte("Ubuntu\n"), bytes.Repeat([]byte("X"), 3*limit)...)
	for _, p := range [][]byte{io[:limit-1], io[limit-1 : limit+1], io[limit+1:]} {
		if n, err := b.Write(p); err != nil || n != len(p) {
			t.Fatalf("write: %d, %v", n, err)
		}
	}
	if !b.truncated || !bytes.Equal(b.data, io[:limit]) {
		t.Fatal("stream was not bounded")
	}
	if _, e := choose(&b, "Ubuntu"); e == nil || e.code != "list-format" {
		t.Fatal("silently used a partial distro list")
	}
}

func TestCLI(t *testing.T) {
	opt, err := parse([]string{"--native", "--distro", "My Ubuntu", "--check-native"})
	if err != nil || opt.distro != "My Ubuntu" || !opt.check || opt.showHelp {
		t.Fatalf("%+v %v", opt, err)
	}
	for _, args := range [][]string{{}, {"--native"}, {"--help"}} {
		opt, err := parse(args)
		if err != nil || opt.showHelp != (len(args) > 0 && args[0] == "--help") {
			t.Error(args, err)
		}
	}
	for _, args := range [][]string{{"--distro"}, {"--distro", ""}, {"--distro", "--native"}, {"--distro", "A", "--distro", "B"}, {"--wsl"}, {"--distro", "A\rB"}} {
		if _, err := parse(args); err == nil {
			t.Errorf("accepted %+v", args)
		}
	}
}

func touch(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, nil, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestLocations(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "winroot")
	// The subprocess executable may only come from the installed Windows system directories.
	touch(t, filepath.Join(base, "wsl.exe"))
	t.Setenv("PATH", base)
	if systemWSL(root) != "" || systemWSL(".") != "" {
		t.Fatal("trusted PATH or relative system root")
	}
	standard := filepath.Join(root, "System32", "wsl.exe")
	native := filepath.Join(root, "Sysnative", "wsl.exe")
	touch(t, standard)
	if systemWSL(root) != standard {
		t.Fatal("missing system32")
	}
	touch(t, native)
	if systemWSL(root) != native {
		t.Fatal("missing sysnative")
	}
	script := filepath.Join(base, "project", "linux", "start-workspace.sh")
	touch(t, script)
	for _, p := range []string{"project/start.exe", "project/windows/start.exe"} {
		if project(filepath.Join(base, p), "linux/start-workspace.sh") != filepath.Join(base, "project") {
			t.Fatal(p)
		}
	}
	if project(filepath.Join(base, "project", "unknown", "child", "start.exe"), "linux/start-workspace.sh") != "" {
		t.Fatal("searched arbitrary ancestors")
	}
}

func TestLaunchSequence(t *testing.T) {
	base := t.TempDir()
	repo := filepath.Join(base, "研发 项目 & more")
	touch(t, filepath.Join(repo, "linux", "start-workspace.sh"))
	exe := filepath.Join(repo, "windows", "launcher.exe")
	wsl := filepath.Join(base, "System32", "wsl.exe")
	touch(t, wsl)
	env := []string{"Path=local", "InstaBox_Api_Key=private", "WSLENV=TERM/u:iNSTABOX_api_key/p:CLOUDWORKSPACE_RENDER_TOKEN:EDITOR", "CLOUDWORKSPACE_RENDER_TOKEN=ephemeral", "EDITOR=vi"}
	wantEnv := []string{"Path=local", "WSLENV=TERM/u:EDITOR", "EDITOR=vi"}
	for _, check := range []bool{false, true} {
		calls := 0
		err := execute(options{distro: "私有 Ubuntu & dev", check: check}, exe, base, env, func(program string, args, environment []string, mode runMode) runResult {
			calls++
			if program != wsl || !reflect.DeepEqual(environment, wantEnv) {
				t.Fatalf("unexpected program or child environment")
			}
			if calls == 1 {
				if mode != listRun || !reflect.DeepEqual(args, []string{"--list", "--quiet"}) {
					t.Fatalf("list: %v", args)
				}
				return runResult{stdout: bounded{data: encoded("Ubuntu\r\n私有 Ubuntu & dev\r\n", binary.LittleEndian, true)}}
			}
			expected := []string{"--distribution", "私有 Ubuntu & dev", "--cd", repo, "--exec", "/bin/bash", "./linux/start-workspace.sh", "--wsl"}
			if check {
				expected = append(expected, "--check")
			}
			if mode != workspaceRun || !reflect.DeepEqual(args, expected) {
				t.Fatalf("launch: %v", args)
			}
			return runResult{}
		})
		if calls != 2 || err != nil {
			t.Fatalf("calls %d, err %v", calls, err)
		}
	}
}

func TestDiagnostics(t *testing.T) {
	base := t.TempDir()
	exe := filepath.Join(base, "start.exe")
	runNone := func(string, []string, []string, runMode) runResult { t.Fatal("ran unexpectedly"); return runResult{} }
	if p := execute(options{}, exe, base, nil, runNone); p == nil || p.code != "package-layout" {
		t.Fatal(p)
	}
	if p := execute(options{native: true}, exe, base, nil, runNone); p == nil || p.code != "package-layout" {
		t.Fatal(p)
	}
	touch(t, filepath.Join(base, "linux", "start-workspace.sh"))
	if p := execute(options{native: true}, exe, base, nil, runNone); p == nil || p.code != "no-wsl" {
		t.Fatal(p)
	}
	touch(t, filepath.Join(base, "System32", "wsl.exe"))
	for _, tc := range []struct {
		list, launch runResult
		code, part   string
		calls        int
	}{
		{runResult{exit: 1}, runResult{}, "list-failed", "WSL", 1},
		{runResult{timedOut: true}, runResult{}, "list-timeout", "超时", 1},
		{runResult{stdout: bounded{data: []byte("Ubuntu\nUbuntu-22.04")}}, runResult{}, "multiple-ubuntu", "多个", 1},
		{runResult{stdout: bounded{data: []byte("Ubuntu")}}, runResult{timedOut: true}, "check-timeout", "超时", 2},
		{runResult{stdout: bounded{data: []byte("Ubuntu")}}, runResult{exit: 2, stderr: bounded{data: []byte("WSLg private-path")}}, "workspace-exit", "WSLg", 2},
		{runResult{stdout: bounded{data: []byte("Ubuntu")}}, runResult{exit: 41, stderr: bounded{data: []byte("请选择本机 Ubuntu private-path")}}, "workspace-exit", "所选发行版不是 Ubuntu", 2},
		{runResult{stdout: bounded{data: []byte("Ubuntu")}}, runResult{exit: 7, stderr: bounded{data: []byte("GNOME private-path")}}, "workspace-exit", "GNOME", 2},
		{runResult{stdout: bounded{data: []byte("Ubuntu")}}, runResult{exit: 53, stdout: bounded{data: []byte("private-path")}}, "workspace-exit", "--check-native", 2},
	} {
		n := 0
		p := execute(options{check: true}, exe, base, nil, func(_ string, _ []string, _ []string, mode runMode) runResult {
			n++
			if mode == listRun {
				return tc.list
			}
			return tc.launch
		})
		if n != tc.calls || p == nil || p.code != tc.code || !strings.Contains(p.message, tc.part) || strings.Contains(p.message, "private-path") {
			t.Errorf("%+v %d", p, n)
		}
	}
}

func TestRenderEntry(t *testing.T) {
	for _, args := range [][]string{{"--connect-port"}, {"--connect-port", ""}, {"--connect-port", "0"}, {"--connect-port", "65536"}, {"--connect-port", "1e1"}, {"--connect-port", "０１"}, {"--connect-port", "8", "--native"}, {"--connect-port", "9", "--distro", "Ubuntu"}, {"--connect-port", "20", "--open-app", "Claude"}, {"--instabox", "--native"}, {"--machine", "--native"}, {"--machine", "example"}, {"--instabox", "--setup-native"}} {
		if _, err := parse(args); err == nil {
			t.Errorf("accepted %q", args)
		}
	}
	base := t.TempDir()
	exe := filepath.Join(base, "windows", "CloudLinuxWorkspace.exe")
	touch(t, filepath.Join(base, "thin_client", "viewer.py"))
	touch(t, filepath.Join(base, "thin_client_entry.py"))
	for _, tc := range []struct {
		portArg           []string
		envPort, wantPort string
		py, direct        bool
		machine           string
	}{
		{nil, "5900", "5900", false, false, ""},
		{[]string{"--connect-port", "5911"}, "5900", "5911", true, false, ""},
		{nil, "", "17324", true, true, ""},
		{[]string{"--instabox"}, "5900", "17324", false, true, ""},
		{[]string{"--instabox", "--connect-port", "17500", "--machine", "m_ab_12"}, "", "17500", true, true, "m_ab_12"},
	} {
		opt, err := parse(tc.portArg)
		if err != nil {
			t.Fatal(err)
		}
		env := []string{"INSTABOX_API_KEY=hidden", "PYLAUNCHER_ALLOW_INSTALL=1", "python_manager_automatic_install=true", "CLOUDWORKSPACE_RENDER_TOKEN=private"}
		var expectedEnv []string
		if tc.envPort != "" {
			pair := "cloudworkspace_render_port=" + tc.envPort
			env = append(env, pair)
			expectedEnv = append(expectedEnv, pair)
		}
		if tc.direct {
			expectedEnv = append(expectedEnv, "INSTABOX_API_KEY=hidden")
		}
		expectedEnv = append(expectedEnv, "CLOUDWORKSPACE_RENDER_TOKEN=private", "PYTHON_MANAGER_AUTOMATIC_INSTALL=false")
		lookup := func(name string) (string, error) {
			if name == "py.exe" && !tc.py {
				return "", os.ErrNotExist
			}
			return filepath.Join(base, name), nil
		}
		calls := 0
		err = launchRenderer(opt, exe, env, lookup, func(program string, args, child []string, mode runMode) runResult {
			calls++
			python := "python.exe"
			want := []string{"-E", "-s", "-X", "utf8", filepath.Join(base, "thin_client_entry.py")}
			wantMode := rendererRun
			if tc.direct {
				want = append(want, "--instabox")
				wantMode = instaboxRendererRun
			}
			want = append(want, "--connect-port", tc.wantPort)
			if tc.machine != "" {
				want = append(want, "--machine", tc.machine)
			}
			if tc.py {
				python = "py.exe"
				want = append([]string{"-3"}, want...)
			}
			if program != filepath.Join(base, python) || !reflect.DeepEqual(args, want) || mode != wantMode {
				t.Fatalf("unexpected viewer invocation %v %v", program, args)
			}
			if !reflect.DeepEqual(child, expectedEnv) {
				t.Fatal("unexpected viewer environment")
			}
			return runResult{exit: 1}
		})
		if calls != 1 || err == nil || err.code != "cloud-render-exit" {
			t.Fatal(calls, err)
		}
	}
}

func TestReplaceLog(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "logs")
	for _, s := range []string{"initial failure", "latest failure"} {
		if err := saveFailure(dir, s); err != nil {
			t.Fatal(err)
		}
	}
	items, err := os.ReadDir(dir)
	if err != nil || len(items) != 1 || items[0].Name() != "launcher-error.log" {
		t.Fatalf("files: %v, %v", items, err)
	}
	got, err := os.ReadFile(filepath.Join(dir, items[0].Name()))
	if err != nil || string(got) != "latest failure" {
		t.Fatalf("log: %s %v", got, err)
	}
}

func TestExplicitSetup(t *testing.T) {
	opt, err := parse([]string{"--setup-native", "--distro", "my Ubuntu"})
	if err != nil || !opt.setup || opt.distro != "my Ubuntu" {
		t.Fatal(opt, err)
	}
	for _, args := range [][]string{{"--check-native", "--setup-native"}, {"--setup-native", "--open-app", "Claude"}} {
		if _, err := parse(args); err == nil {
			t.Fatal(args)
		}
	}
	dir := t.TempDir()
	exe := filepath.Join(dir, "windows", "start.exe")
	touch(t, filepath.Join(dir, "linux", "start-workspace.sh"))
	script := filepath.Join(dir, "linux", "setup-ubuntu.sh")
	touch(t, script)
	wsl := filepath.Join(dir, "System32", "wsl.exe")
	touch(t, wsl)
	nativeCtrlC := uint32(0xc000013a) // STATUS_CONTROL_C_EXIT from GetExitCodeProcess
	for _, tc := range []struct {
		exit int
		code string
	}{{int(nativeCtrlC), "setup-cancelled"}, {0, ""}, {-1, "setup-console"}, {65, "setup-version"}, {69, "setup-wslg"}, {70, "setup-console"}, {71, "setup-exit"}, {72, "setup-exit"}, {130, "setup-cancelled"}} {
		n := 0
		problem := execute(opt, exe, dir, []string{"INSTABOX_API_KEY=never-forward"}, func(program string, args, env []string, mode runMode) runResult {
			if program != wsl || len(env) > 0 {
				t.Fatal(program, env)
			}
			n++
			if mode == listRun && n == 1 {
				return runResult{stdout: bounded{data: []byte("Ubuntu\nmy Ubuntu\n")}}
			}
			want := []string{"--distribution", "my Ubuntu", "--cd", dir, "--exec", "/bin/bash", "./linux/setup-ubuntu.sh", "--wsl"}
			if n != 2 || mode != setupRun || !reflect.DeepEqual(args, want) {
				t.Fatal(n, mode, args)
			}
			return runResult{exit: tc.exit}
		})
		if n != 2 || (problem != nil) != (tc.code != "") || (problem != nil && problem.code != tc.code) {
			t.Fatal(n, problem)
		}
	}
	if err := os.Remove(script); err != nil {
		t.Fatal(err)
	}
	p := execute(opt, exe, dir, nil, func(_ string, _ []string, _ []string, mode runMode) runResult {
		if mode != listRun {
			t.Fatal("started without installer")
		}
		return runResult{stdout: bounded{data: []byte("my Ubuntu")}}
	})
	if p == nil || p.code != "package-layout" {
		t.Fatal(p)
	}
}
