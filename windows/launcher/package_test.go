package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestSourceCheckoutUsesItsExactReleaseAndCleansUp(t *testing.T) {
	root := t.TempDir()
	archivePath := filepath.Join(root, "releases", "thin-windows", "cloud-workspace.zip")
	receiptPath := filepath.Join(root, "docs", "measurements", "cloud-render.json")
	touch(t, archivePath)
	touch(t, receiptPath)
	var data bytes.Buffer
	z := zip.NewWriter(&data)
	for _, name := range []string{"thin_client_entry.py", "windows/runtime/python.exe"} {
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
	metadata, err := json.Marshal(map[string]any{"standalonePackage": map[string]string{"sha256": fmt.Sprintf("%x", sha256.Sum256(data.Bytes()))}})
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(receiptPath, metadata, 0600); err != nil {
		t.Fatal(err)
	}
	folder, cleanup, problem := packagedClient(root)
	if problem != nil {
		t.Fatal(problem)
	}
	if folder == root || !regular(filepath.Join(folder, "windows", "runtime", "python.exe")) {
		t.Fatal("missing private expansion")
	}
	cleanup()
	if _, err = os.Stat(folder); !os.IsNotExist(err) {
		t.Fatal("temporary runtime survived")
	}
	if err = os.WriteFile(archivePath, append(data.Bytes(), '!'), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, problem = packagedClient(root); problem == nil || problem.code != "package-integrity" {
		t.Fatal("accepted modified archive")
	}
}
