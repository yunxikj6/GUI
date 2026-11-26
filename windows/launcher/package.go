package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

const originalNode = "thin_client/vendor/node/windows-x64/node.exe"

// A source checkout expands the release selected for this invocation. A portable
// client already has its isolated Python runtime and can run in place.
func packagedClient(root string, original bool) (string, func(), *failure) {
	if regular(filepath.Join(root, "windows", "runtime", "python.exe")) {
		return root, func() {}, nil
	}
	packageDir, entry := "thin-windows", "thin_client_entry.py"
	if original {
		packageDir, entry = "original-win64", "experiments/original_desktop.py"
	}
	zipFile := filepath.Join(root, "releases", packageDir, "cloud-workspace.zip")
	receipt, err := os.ReadFile(filepath.Join(root, "docs", "measurements", "cloud-render.json"))
	var release struct {
		StandalonePackage, OriginalDesktopPackage struct{ SHA256 string }
	}
	if err != nil || json.Unmarshal(receipt, &release) != nil {
		return "", nil, fail("package-layout", "缺少内置 Windows 运行时或发行记录，请重新完整解压客户端 ZIP。")
	}
	expected := release.StandalonePackage.SHA256
	if original {
		expected = release.OriginalDesktopPackage.SHA256
	}
	if len(expected) != 64 {
		return "", nil, fail("package-layout", "缺少所选 Windows 发行包的 SHA-256 记录。")
	}
	f, err := os.Open(zipFile)
	if err != nil {
		return "", nil, fail("package-layout", "缺少源码仓库附带的客户端发行 ZIP。")
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > 128<<20 {
		return "", nil, fail("package-layout", "客户端发行 ZIP 的大小或文件类型异常。")
	}
	sum := sha256.New()
	if _, err = io.Copy(sum, f); err != nil || hex.EncodeToString(sum.Sum(nil)) != expected {
		return "", nil, fail("package-integrity", "客户端发行 ZIP 与此提交的 SHA-256 不一致，请重新下载。")
	}
	folder, err := os.MkdirTemp("", "cloud-workspace-")
	if err != nil {
		return "", nil, fail("package-layout", "无法创建客户端运行目录。")
	}
	cleanup := func() { os.RemoveAll(folder) }
	archive, err := zip.NewReader(f, info.Size())
	if err != nil || extractClient(archive, folder, original) != nil || !regular(filepath.Join(folder, "windows", "runtime", "python.exe")) || !regular(filepath.Join(folder, filepath.FromSlash(entry))) || original && !regular(filepath.Join(folder, filepath.FromSlash(originalNode))) {
		cleanup()
		return "", nil, fail("package-layout", "无法解压完整的 Windows 客户端。")
	}
	return folder, cleanup, nil
}

func extractClient(archive *zip.Reader, root string, original bool) error {
	var total uint64
	for _, entry := range archive.File {
		name := filepath.FromSlash(entry.Name)
		maxMember := uint64(64 << 20)
		if original && entry.Name == originalNode {
			maxMember = 96 << 20
		}
		if !fs.ValidPath(entry.Name) || !filepath.IsLocal(name) || strings.ContainsAny(entry.Name, "\\:") || !entry.Mode().IsRegular() || entry.UncompressedSize64 > maxMember || total+entry.UncompressedSize64 > 256<<20 {
			return errors.New("invalid archive member")
		}
		total += entry.UncompressedSize64
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			return err
		}
		input, err := entry.Open()
		if err != nil {
			return err
		}
		output, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			input.Close()
			return err
		}
		_, copyError := io.Copy(output, input)
		input.Close()
		closeError := output.Close()
		if copyError != nil {
			return copyError
		}
		if closeError != nil {
			return closeError
		}
	}
	return nil
}
