package main

import (
	"fmt"
	"runtime"
	"syscall"
	"unsafe"
)

// These system APIs use current-user registration. The queries neither install nor update packages.
var (
	findFamily      = kernel.NewProc("FindPackagesByPackageFamily")
	getOrigin       = syscall.NewLazyDLL("kernelbase.dll").NewProc("GetStagedPackageOrigin")
	openInfo        = kernel.NewProc("OpenPackageInfoByFullName")
	closeInfo       = kernel.NewProc("ClosePackageInfo")
	getAppIDs       = kernel.NewProc("GetPackageApplicationIds")
	createShellItem = syscall.NewLazyDLL("shell32.dll").NewProc("SHCreateItemFromParsingName")
	ole             = syscall.NewLazyDLL("ole32.dll")
	coInit          = ole.NewProc("CoInitializeEx")
	coUninit        = ole.NewProc("CoUninitialize")
	coCreate        = ole.NewProc("CoCreateInstance")
	coFree          = ole.NewProc("CoTaskMemFree")
	coForeground    = ole.NewProc("CoAllowSetForegroundWindow")
	strLength       = kernel.NewProc("lstrlenW")
	iidShellItem    = syscall.GUID{Data1: 0x43826d1e, Data2: 0xe718, Data3: 0x42ee, Data4: [8]byte{0xbc, 0x55, 0xa1, 0xe2, 0x61, 0xc3, 0x7b, 0xfe}}
	clsidManager    = syscall.GUID{Data1: 0x45ba127d, Data2: 0x10a8, Data3: 0x46ea, Data4: [8]byte{0x8a, 0xb7, 0x56, 0xea, 0x90, 0x78, 0x94, 0x3c}}
	iidManager      = syscall.GUID{Data1: 0x2e941141, Data2: 0x7f97, Data3: 0x4756, Data4: [8]byte{0xba, 0x1d, 0x9d, 0xec, 0xde, 0x89, 0x4a, 0x3d}}
)

func winFailure(action string, code uintptr) *failure {
	return fail("windows-app-api", fmt.Sprintf("Windows %s失败（0x%08X）。请在开始菜单中检查该官方应用，或稍后重试。", action, uint32(code)))
}

// Win32 returns NUL-terminated strings via pointers into the caller-supplied buffer.
func stringInBuffer(buf []uint16, p *uint16) string {
	if len(buf) == 0 || p == nil {
		return ""
	}
	base, addr := uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(p))
	if addr < base || addr-base >= uintptr(len(buf))*2 || (addr-base)%2 != 0 {
		return ""
	}
	start := int((addr - base) / 2)
	for i := start; i < len(buf); i++ {
		if buf[i] == 0 {
			return syscall.UTF16ToString(buf[start:i])
		}
	}
	return ""
}

func hasAppID(name, target string) (bool, *failure) {
	fullName, _ := syscall.UTF16PtrFromString(name)
	var ref uintptr
	code, _, _ := openInfo.Call(uintptr(unsafe.Pointer(fullName)), 0, uintptr(unsafe.Pointer(&ref)))
	if code != 0 {
		return false, winFailure("读取包注册", code)
	}
	defer closeInfo.Call(ref)
	var size, count uint32
	code, _, _ = getAppIDs.Call(ref, uintptr(unsafe.Pointer(&size)), 0, uintptr(unsafe.Pointer(&count)))
	if code == 0 && size == 0 {
		return false, nil
	}
	if code != uintptr(syscall.ERROR_INSUFFICIENT_BUFFER) || size < 2 || size > 1024*1024 {
		return false, winFailure("读取应用入口", code)
	}
	// AppModel.h specifies a pointer table followed by UTF-16 strings in the same byte buffer.
	buf := make([]uint16, (size+1)/2)
	code, _, _ = getAppIDs.Call(ref, uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&count)))
	if code != 0 {
		return false, winFailure("读取应用入口", code)
	}
	if uintptr(count)*unsafe.Sizeof(uintptr(0)) > uintptr(size) {
		return false, winFailure("校验入口清单", 13)
	}
	for _, p := range unsafe.Slice((**uint16)(unsafe.Pointer(&buf[0])), count) {
		if stringInBuffer(buf, p) == target {
			return true, nil
		}
	}
	return false, nil
}

func inspectChannel(c channel) (registeredApp, *failure) {
	var reg registeredApp
	name, _ := syscall.UTF16PtrFromString(c.family)
	var count, chars uint32
	const head = 0x10 // PACKAGE_FILTER_HEAD; current user's main app packages
	code, _, _ := findFamily.Call(uintptr(unsafe.Pointer(name)), head, uintptr(unsafe.Pointer(&count)), 0, uintptr(unsafe.Pointer(&chars)), 0, 0)
	if code == 0 && count == 0 {
		return reg, nil
	}
	if code != uintptr(syscall.ERROR_INSUFFICIENT_BUFFER) || count == 0 || count > 256 || chars == 0 || chars > 64*1024 {
		return reg, winFailure("查询已安装应用", code)
	}
	names := make([]*uint16, count)
	flags := make([]uint32, count)
	buf := make([]uint16, chars)
	code, _, _ = findFamily.Call(uintptr(unsafe.Pointer(name)), head, uintptr(unsafe.Pointer(&count)), uintptr(unsafe.Pointer(&names[0])), uintptr(unsafe.Pointer(&chars)), uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&flags[0])))
	if code != 0 {
		return reg, winFailure("读取应用登记", code)
	}
	if count == 0 {
		return reg, nil
	}
	reg.installed = true
	// The activation manager resolves a family and application ID, not a version. Fail closed
	// during an ambiguous deployment instead of verifying one version and activating another.
	if count != 1 {
		return reg, nil
	}
	reg.development = flags[0]&0x10000 != 0 // PACKAGE_PROPERTY_DEVELOPMENT_MODE
	if reg.development || flags[0]&0x0f != 0 {
		return reg, nil
	} // no resource, framework, bundle, or optional packages
	reg.fullName = stringInBuffer(buf, names[0])
	if reg.fullName == "" {
		return reg, winFailure("校验已安装应用", 13)
	}
	fullName, _ := syscall.UTF16PtrFromString(reg.fullName)
	var origin uint32
	code, _, _ = getOrigin.Call(uintptr(unsafe.Pointer(fullName)), uintptr(unsafe.Pointer(&origin)))
	if code != 0 {
		return reg, winFailure("读取软件包来源", code)
	}
	reg.store = origin == 3 // PackageOrigin_Store
	if !reg.store {
		return reg, nil
	}
	registered, problem := hasAppID(reg.fullName, c.aumid())
	if problem != nil {
		return reg, problem
	}
	reg.registered = registered
	if registered && c.checkChatGPTLabel {
		reg.label = shellLabel(c.aumid())
	}
	return reg, nil
}

type comObject struct{ vtable *[6]uintptr }

func (o *comObject) release() { syscall.SyscallN(o.vtable[2], uintptr(unsafe.Pointer(o))) }
func failed(hr uintptr) bool  { return int32(hr) < 0 }

func shellLabel(id string) string {
	path, _ := syscall.UTF16PtrFromString("shell:AppsFolder\\" + id)
	var item *comObject
	hr, _, _ := createShellItem.Call(uintptr(unsafe.Pointer(path)), 0, uintptr(unsafe.Pointer(&iidShellItem)), uintptr(unsafe.Pointer(&item)))
	if failed(hr) || item == nil {
		return ""
	}
	defer item.release()
	var label *uint16
	hr, _, _ = syscall.SyscallN(item.vtable[5], uintptr(unsafe.Pointer(item)), 0, uintptr(unsafe.Pointer(&label))) // IShellItem.GetDisplayName(SIGDN_NORMALDISPLAY)
	if failed(hr) || label == nil {
		return ""
	}
	defer coFree.Call(uintptr(unsafe.Pointer(label)))
	n, _, _ := strLength.Call(uintptr(unsafe.Pointer(label)))
	if n == 0 || n > 4096 {
		return ""
	}
	return syscall.UTF16ToString(unsafe.Slice(label, int(n)))
}

func activateApp(id string) (uint32, *failure) {
	var obj *comObject
	hr, _, _ := coCreate.Call(uintptr(unsafe.Pointer(&clsidManager)), 0, 4, uintptr(unsafe.Pointer(&iidManager)), uintptr(unsafe.Pointer(&obj))) // CLSCTX_LOCAL_SERVER
	if failed(hr) || obj == nil {
		return 0, winFailure("创建应用激活管理器", hr)
	}
	defer obj.release()
	// The shell still controls which foreground-window permissions are available.
	coForeground.Call(uintptr(unsafe.Pointer(obj)), 0)
	name, _ := syscall.UTF16PtrFromString(id)
	var pid uint32
	hr, _, _ = syscall.SyscallN(obj.vtable[3], uintptr(unsafe.Pointer(obj)), uintptr(unsafe.Pointer(name)), 0, 0, uintptr(unsafe.Pointer(&pid)))
	if failed(hr) {
		return 0, winFailure("激活官方应用", hr)
	}
	return pid, nil
}

func windowsOfficialApp(opt options) (appStatus, *failure) {
	for _, p := range []*syscall.LazyProc{findFamily, getOrigin, openInfo, closeInfo, getAppIDs, createShellItem, coInit, coUninit, coCreate, coFree, coForeground, strLength} {
		if p.Find() != nil {
			return appStatus{}, fail("windows-app-api", "当前 Windows 缺少应用包或 Shell 激活接口。请在 Windows 10/11 中运行。")
		}
	}
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	hr, _, _ := coInit.Call(0, 2) // COINIT_APARTMENTTHREADED
	if failed(hr) {
		return appStatus{}, winFailure("初始化应用激活服务", hr)
	}
	defer coUninit.Call()
	return officialApp(opt, inspectChannel, activateApp)
}
