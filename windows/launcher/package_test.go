package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func writeTestClient(t *testing.T, root, folder string, entries ...string) (string, []byte) {
	t.Helper()
	archivePath := filepath.Join(root, "releases", folder, "cloud-workspace.zip")
	touch(t, archivePath)
	var data bytes.Buffer
	z := zip.NewWriter(&data)
	for _, name := range entries {
		w, err := z.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = w.Write([]byte(name)); err != nil {
			t.Fatal(err)
		}
	}
	if err := z.Close(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(archivePath, data.Bytes(), 0600); err != nil {
		t.Fatal(err)
	}
	return archivePath, data.Bytes()
}

func writeReceipt(t *testing.T, root string, metadata map[string]map[string]string) {
	t.Helper()
	receiptPath := filepath.Join(root, "docs", "measurements", "cloud-render.json")
	touch(t, receiptPath)
	body, err := json.Marshal(metadata)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(receiptPath, body, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestSourceCheckoutUsesItsExactReleaseAndCleansUp(t *testing.T) {
	root := t.TempDir()
	archivePath, data := writeTestClient(t, root, "thin-windows", "thin_client_entry.py", "windows/runtime/python.exe")
	writeReceipt(t, root, map[string]map[string]string{"standalonePackage": {"sha256": fmt.Sprintf("%x", sha256.Sum256(data))}})
	folder, cleanup, problem := packagedClient(root, false)
	if problem != nil {
		t.Fatal(problem)
	}
	if folder == root || !regular(filepath.Join(folder, "windows", "runtime", "python.exe")) {
		t.Fatal("missing private expansion")
	}
	cleanup()
	if _, err := os.Stat(folder); !os.IsNotExist(err) {
		t.Fatal("temporary runtime survived")
	}
	if err := os.WriteFile(archivePath, append(data, '!'), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, problem = packagedClient(root, false); problem == nil || problem.code != "package-integrity" {
		t.Fatal("accepted modified archive")
	}
}

func TestCheckoutSelectsOriginalOnlyForDefault(t *testing.T) {
	root := t.TempDir()
	// An older portable runtime unpacked into the checkout still requires archive verification.
	touch(t, filepath.Join(root, "launcher.py"))
	touch(t, filepath.Join(root, "windows", "runtime", "python.exe"))
	touch(t, filepath.Join(root, "thin_client_entry.py"))
	exe := filepath.Join(root, "windows", "CloudLinuxWorkspace-x64.exe")
	originalPath, original := writeTestClient(t, root, "original-win64", "windows/runtime/python.exe", "experiments/original_desktop.py", originalNode)
	_, standard := writeTestClient(t, root, "thin-windows", "windows/runtime/python.exe", "thin_client_entry.py")
	writeReceipt(t, root, map[string]map[string]string{
		"originalDesktopPackage": {"sha256": fmt.Sprintf("%x", sha256.Sum256(original))},
		"standalonePackage":      {"sha256": fmt.Sprintf("%x", sha256.Sum256(standard))},
	})
	for _, tc := range []struct {
		opt          options
		mode         runMode
		wantOriginal bool
	}{
		{options{}, desktopRendererRun, true},
		{options{direct: true}, desktopRendererRun, false},
		{options{desktopForwardedPort: "9011"}, forwardedDesktopRun, false},
		{options{cloudApp: true}, instaboxRendererRun, false},
	} {
		var dir string
		err := execute(tc.opt, exe, "", nil, func(program string, args, env []string, mode runMode) runResult {
			dir = filepath.Dir(filepath.Dir(filepath.Dir(program)))
			if dir == root || !regular(program) || mode != tc.mode {
				t.Fatalf("expected private interpreter and selected mode: %v %v", program, mode)
			}
			if tc.wantOriginal {
				if !regular(filepath.Join(dir, originalNode)) || !reflect.DeepEqual(args, []string{"-I", "-X", "utf8", "-m", "experiments.original_desktop", "--instabox"}) {
					t.Fatalf("expected the original desktop with its bundled Node: %v", args)
				}
			} else if !regular(filepath.Join(dir, "thin_client_entry.py")) || args[3] != filepath.Join(dir, "thin_client_entry.py") {
				t.Fatalf("explicit route did not use its standard client: %v", args)
			}
			return runResult{}
		})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := os.Stat(dir); dir == "" || !os.IsNotExist(err) {
			t.Fatal("private client was not run and cleaned up")
		}
	}
	if err := os.Remove(originalPath); err != nil {
		t.Fatal(err)
	}
	if problem := execute(options{}, exe, "", nil, nil); problem == nil || problem.code != "package-layout" {
		t.Fatal("default route fell through to another release")
	}
}
