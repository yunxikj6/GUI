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

// The small source checkout carries the self-contained release once. Extract that
// exact release to an invocation-private directory when launching from the checkout.
func packagedClient(root string) (string, func(), *failure) {
	if regular(filepath.Join(root, "windows", "runtime", "python.exe")) {
		return root, func() {}, nil
	}
	zipFile := filepath.Join(root, "releases", "thin-windows", "cloud-workspace.zip")
	receipt, err := os.ReadFile(filepath.Join(root, "docs", "measurements", "cloud-render.json"))
	var release struct{ StandalonePackage struct{ SHA256 string } }
	if err != nil || json.Unmarshal(receipt, &release) != nil || len(release.StandalonePackage.SHA256) != 64 {
		return "", nil, fail("package-layout", "缺少内置 Windows 运行时或发行记录，请重新完整解压客户端 ZIP。")
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
	if _, err = io.Copy(sum, f); err != nil || hex.EncodeToString(sum.Sum(nil)) != release.StandalonePackage.SHA256 {
		return "", nil, fail("package-integrity", "客户端发行 ZIP 与此提交的 SHA-256 不一致，请重新下载。")
	}
	folder, err := os.MkdirTemp("", "cloud-workspace-")
	if err != nil {
		return "", nil, fail("package-layout", "无法创建客户端运行目录。")
	}
	cleanup := func() { os.RemoveAll(folder) }
	archive, err := zip.NewReader(f, info.Size())
	if err != nil || extractClient(archive, folder) != nil || !regular(filepath.Join(folder, "windows", "runtime", "python.exe")) || !regular(filepath.Join(folder, "thin_client_entry.py")) {
		cleanup()
		return "", nil, fail("package-layout", "无法解压完整的 Windows 客户端。")
	}
	return folder, cleanup, nil
}

func extractClient(archive *zip.Reader, root string) error {
	var total uint64
	for _, entry := range archive.File {
		name := filepath.FromSlash(entry.Name)
		if !fs.ValidPath(entry.Name) || !filepath.IsLocal(name) || strings.ContainsAny(entry.Name, "\\:") || !entry.Mode().IsRegular() || entry.UncompressedSize64 > 64<<20 || total+entry.UncompressedSize64 > 256<<20 {
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
