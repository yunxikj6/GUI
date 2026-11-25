using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

namespace OfficialDesktop {
    public class AppWindow {
        public string Hwnd, ContentHwnd, Family, PackageFullName, ProcessAumid, ImagePath;
        public uint WindowPid, AppPid;
        public long ProcessCreation;
        public bool Hosted, Foreground;
    }

    public static class WindowProbe {
        private delegate bool WindowCallback(IntPtr hwnd, IntPtr data);
        private delegate int QueryId(IntPtr process, ref uint length, StringBuilder text);

        [StructLayout(LayoutKind.Sequential)]
        private struct Rect { public int Left, Top, Right, Bottom; }
        [DllImport("user32.dll", SetLastError = true)] private static extern bool EnumWindows(WindowCallback callback, IntPtr data);
        [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr root, WindowCallback callback, IntPtr data);
        [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
        [DllImport("user32.dll")] private static extern IntPtr MonitorFromWindow(IntPtr hwnd, uint flags);
        [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd, StringBuilder name, int max);
        [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(IntPtr hwnd, int attr, out uint value, uint size);
        [DllImport("kernel32.dll")] private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] private static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder name, ref uint length);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, ExactSpelling=true)] private static extern int GetPackageFamilyName(IntPtr process, ref uint length, StringBuilder value);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, ExactSpelling=true)] private static extern int GetPackageFullName(IntPtr process, ref uint length, StringBuilder value);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, ExactSpelling=true)] private static extern int GetApplicationUserModelId(IntPtr process, ref uint length, StringBuilder value);
        [DllImport("kernel32.dll")] private static extern bool GetExitCodeProcess(IntPtr process, out uint code);
        [DllImport("kernel32.dll")] private static extern bool GetProcessTimes(IntPtr process, out long created, out long exited, out long kernel, out long user);

        private static bool Same(string a, string b) { return string.Equals(a,b,StringComparison.OrdinalIgnoreCase); }

        private static int ReadId(QueryId query, IntPtr process, out string value) {
            value=null;
            uint length=0;
            int result=query(process, ref length, null);
            if (result!=122 || length==0 || length>32768) return result;
            var builder=new StringBuilder((int)length);
            result=query(process, ref length, builder);
            if (result==0) value=builder.ToString();
            return result;
        }

        private static string Image(IntPtr process) {
            uint length=32768;
            var builder=new StringBuilder((int)length);
            return QueryFullProcessImageName(process,0,builder,ref length) ? builder.ToString() : null;
        }

        private static bool HasArea(IntPtr hwnd) {
            Rect rect;
            return IsWindowVisible(hwnd) && GetWindowRect(hwnd,out rect) &&
                   rect.Right>rect.Left && rect.Bottom>rect.Top && MonitorFromWindow(hwnd,0)!=IntPtr.Zero;
        }

        private static bool IsFrameHost(IntPtr hwnd, uint pid) {
            var klass=new StringBuilder(128);
            if (GetClassName(hwnd,klass,klass.Capacity)==0 || klass.ToString()!="ApplicationFrameWindow") return false;
            IntPtr proc=OpenProcess(0x1000,false,pid);
            if (proc==IntPtr.Zero) return false;
            try {
                string system=Environment.GetFolderPath(Environment.SpecialFolder.Windows);
                return Same(Image(proc),Path.Combine(system,@"System32\ApplicationFrameHost.exe"));
            } finally { CloseHandle(proc); }
        }

        // An MSIX desktop process can lack an application ID even when its package ID is present.
        // In that case the exact registered version and the manifest's executable remain mandatory.
        public static bool MatchesPackage(string targetFamily,string targetFull,string targetApp,string targetImage,
                                         string family,string full,string app,string image) {
            return !string.IsNullOrEmpty(family) && !string.IsNullOrEmpty(full) && !string.IsNullOrEmpty(image) &&
                   Same(family,targetFamily) && Same(full,targetFull) && Same(image,targetImage) &&
                   (string.IsNullOrEmpty(app) || Same(app,targetApp));
        }

        public static bool IsLegacyImage(string target,string image) {
            if (string.IsNullOrEmpty(image) || string.IsNullOrEmpty(target)) return false;
            if (Same(target,image)) return true;
            int slash=target.LastIndexOf('\\');
            if (slash<0) return false;
            string folder=target.Substring(0,slash+1);
            return image.StartsWith(folder,StringComparison.OrdinalIgnoreCase) &&
                   Regex.IsMatch(image.Substring(folder.Length),@"^app-[0-9]+(?:\.[0-9]+){1,3}\\Claude\.exe\z",RegexOptions.IgnoreCase|RegexOptions.CultureInvariant);
        }

        private static AppWindow Inspect(IntPtr root,uint rootPid,IntPtr hwnd,uint pid,string targetFamily,
                                         string targetFull,string targetId,string imagePath) {
            IntPtr proc=OpenProcess(0x1000,false,pid);
            if (proc==IntPtr.Zero) return null;
            try {
                string family=null,full=null,app=null,path;
                int familyStatus=ReadId(GetPackageFamilyName,proc,out family);
                if (!string.IsNullOrEmpty(targetFamily)) {
                    if (familyStatus!=0 || !Same(family,targetFamily) || ReadId(GetPackageFullName,proc,out full)!=0) return null;
                    int appStatus=ReadId(GetApplicationUserModelId,proc,out app);
                    if (appStatus!=0 && appStatus!=15703) return null; // APPMODEL_ERROR_NO_APPLICATION
                    path=Image(proc);
                    if (!MatchesPackage(targetFamily,targetFull,targetId,imagePath,family,full,app,path)) return null;
                } else {
                    if (familyStatus!=15700) return null; // APPMODEL_ERROR_NO_PACKAGE
                    path=Image(proc);
                    if (!IsLegacyImage(imagePath,path)) return null;
                }
                long created,exited,kernel,user;
                uint exitCode;
                if (!GetProcessTimes(proc,out created,out exited,out kernel,out user) || !GetExitCodeProcess(proc,out exitCode) || exitCode!=259) return null;
                return new AppWindow {
                    Hwnd="0x"+root.ToInt64().ToString("X"),ContentHwnd="0x"+hwnd.ToInt64().ToString("X"),
                    WindowPid=rootPid,AppPid=pid,ProcessCreation=created,Hosted=root!=hwnd,
                    Family=family,PackageFullName=full,ProcessAumid=app,ImagePath=path,
                    Foreground=root==GetForegroundWindow()
                };
            } finally { CloseHandle(proc); }
        }

        public static AppWindow[] Snapshot(string family,string fullName,string appId,string imagePath) {
            var matches=new List<AppWindow>();
            bool ok=EnumWindows(delegate(IntPtr root,IntPtr unused) {
                uint rootPid,cloaked;
                if (!HasArea(root) || IsIconic(root) || DwmGetWindowAttribute(root,14,out cloaked,4)!=0 || cloaked!=0 ||
                    GetWindowThreadProcessId(root,out rootPid)==0) return true;
                var own=Inspect(root,rootPid,root,rootPid,family,fullName,appId,imagePath);
                if (own!=null) matches.Add(own);
                else if (IsFrameHost(root,rootPid)) {
                    EnumChildWindows(root,delegate(IntPtr child,IntPtr state) {
                        uint childPid;
                        if (GetWindowThreadProcessId(child,out childPid)==0 || childPid==rootPid || !HasArea(child)) return true;
                        var linked=Inspect(root,rootPid,child,childPid,family,fullName,appId,imagePath);
                        if (linked==null) return true;
                        matches.Add(linked);
                        return false;
                    },IntPtr.Zero);
                }
                return true;
            },IntPtr.Zero);
            if (!ok) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            return matches.ToArray();
        }
    }
}
