using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace CloudWorkspace {
    public sealed class DesktopWindow {
        public string Hwnd { get; private set; }
        public uint ProcessId { get; private set; }
        public string ProcessImage { get; private set; }
        // The UTC process-creation FILETIME, in 100-nanosecond ticks since 1601-01-01.
        public long ProcessStartTime { get; private set; }
        public bool Foreground { get; private set; }
        public bool Minimized { get; private set; }
        public int Width { get; private set; }
        public int Height { get; private set; }

        private DesktopWindow() { }

        // WSLGd currently selects the WSL installation's msrdc.exe or System32's mstsc.exe.
        // This is a correlated UI observation, not publisher or process attestation.
        internal static bool SupportedHost(string basename) {
            return String.Equals(basename, "msrdc.exe", StringComparison.OrdinalIgnoreCase) ||
                   String.Equals(basename, "mstsc.exe", StringComparison.OrdinalIgnoreCase);
        }

        internal static bool ValidNonce(string nonce) {
            if (nonce == null || nonce.Length != 32) return false;
            foreach (char c in nonce) {
                if (!(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f')) return false;
            }
            return true;
        }

        // Microsoft's Weston RAIL backend sends the app caption verbatim or adds " (distro)".
        internal static bool MatchesCaption(string caption, string marker, string distro) {
            return String.Equals(caption, marker, StringComparison.Ordinal) ||
                   String.Equals(caption, marker + " (" + distro + ")", StringComparison.Ordinal) ||
                   String.Equals(caption, marker + " (Linux)", StringComparison.Ordinal);
        }

        private delegate bool EnumCallback(IntPtr hwnd, IntPtr unused);
        [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)] private struct Point { public int X, Y; }
        [DllImport("user32.dll", SetLastError = true)] private static extern bool EnumWindows(EnumCallback callback, IntPtr unused);
        [DllImport("user32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
        private static extern int GetWindowTextW(IntPtr hwnd, StringBuilder text, int count);
        [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool GetClientRect(IntPtr hwnd, out Rect area);
        [DllImport("user32.dll")] private static extern bool ClientToScreen(IntPtr hwnd, ref Point point);
        [DllImport("user32.dll")] private static extern IntPtr MonitorFromRect(ref Rect rect, uint flags);
        [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(IntPtr hwnd, int attr, out uint value, uint size);
        [DllImport("kernel32.dll")] private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
        private static extern bool QueryFullProcessImageNameW(IntPtr process, uint flags, StringBuilder path, ref uint length);
        [DllImport("kernel32.dll")] private static extern bool GetProcessTimes(IntPtr process, out long created, out long exited, out long kernel, out long user);
        [DllImport("kernel32.dll")] private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

        private static bool HasCaption(IntPtr hwnd, string marker, string distro) {
            // Read a bounded prefix before considering the rest of any window caption.
            var prefix = new StringBuilder(marker.Length + 1);
            if (GetWindowTextW(hwnd, prefix, prefix.Capacity) != marker.Length ||
                !String.Equals(prefix.ToString(), marker, StringComparison.Ordinal)) return false;
            var caption = new StringBuilder(marker.Length + Math.Max(distro.Length, 5) + 5);
            int count = GetWindowTextW(hwnd, caption, caption.Capacity);
            return count > 0 && count < caption.Capacity - 1 && MatchesCaption(caption.ToString(), marker, distro);
        }

        internal static bool MatchesPresentation(bool visible, bool iconic, int dwmResult, uint cloaked, bool minimized) {
            return visible && iconic == minimized && dwmResult == 0 && cloaked == 0;
        }

        internal static bool VisibleState(bool visible, bool iconic, int dwmResult, uint cloaked, int width, int height) {
            return MatchesPresentation(visible, iconic, dwmResult, cloaked, false) && width > 0 && height > 0;
        }

        private const int DwmwaCloaked = 14;

        private static bool HasPresentation(IntPtr hwnd, bool minimized) {
            uint cloaked;
            int result = DwmGetWindowAttribute(hwnd, DwmwaCloaked, out cloaked, 4);
            return MatchesPresentation(IsWindowVisible(hwnd), IsIconic(hwnd), result, cloaked, minimized);
        }

        private static bool VisibleClient(IntPtr hwnd, out int width, out int height) {
            width = height = 0;
            uint cloaked;
            Rect client;
            if (!GetClientRect(hwnd, out client)) return false;
            width = client.Right - client.Left;
            height = client.Bottom - client.Top;
            int dwmResult = DwmGetWindowAttribute(hwnd, DwmwaCloaked, out cloaked, 4);
            if (!VisibleState(IsWindowVisible(hwnd), IsIconic(hwnd), dwmResult, cloaked, width, height)) return false;
            var start = new Point { X = client.Left, Y = client.Top };
            var end = new Point { X = client.Right, Y = client.Bottom };
            if (!ClientToScreen(hwnd, ref start) || !ClientToScreen(hwnd, ref end)) return false;
            var screen = new Rect { Left = Math.Min(start.X, end.X), Top = Math.Min(start.Y, end.Y),
                                    Right = Math.Max(start.X, end.X), Bottom = Math.Max(start.Y, end.Y) };
            return screen.Right > screen.Left && screen.Bottom > screen.Top && MonitorFromRect(ref screen, 0) != IntPtr.Zero;
        }

        private static DesktopWindow Observe(IntPtr hwnd, string marker, string distro, bool includeMinimized) {
            uint pid;
            if (!HasCaption(hwnd, marker, distro) || GetWindowThreadProcessId(hwnd, out pid) == 0) return null;
            const uint QueryLimitedInformation = 0x1000, Synchronize = 0x100000;
            var process = OpenProcess(QueryLimitedInformation | Synchronize, false, pid);
            if (process == IntPtr.Zero) return null;
            try {
                uint length = 32768;
                var path = new StringBuilder((int)length);
                if (!QueryFullProcessImageNameW(process, 0, path, ref length)) return null;
                string basename = System.IO.Path.GetFileName(path.ToString());
                if (!SupportedHost(basename)) return null;
                long created, exited, kernel, user;
                if (!GetProcessTimes(process, out created, out exited, out kernel, out user)) return null;
                bool minimized = IsIconic(hwnd);
                int width = 0, height = 0;
                // An iconic window has no on-screen client area to report.
                if (minimized) {
                    if (!includeMinimized || !HasPresentation(hwnd, true)) return null;
                } else if (!VisibleClient(hwnd, out width, out height)) return null;
                uint currentPid;
                const uint WaitTimeout = 258;
                if (!HasCaption(hwnd, marker, distro) || GetWindowThreadProcessId(hwnd, out currentPid) == 0 ||
                    currentPid != pid || created <= 0 || WaitForSingleObject(process, 0) != WaitTimeout ||
                    !HasPresentation(hwnd, minimized)) return null;
                return new DesktopWindow {
                    Hwnd = "0x" + (IntPtr.Size == 4 ? unchecked((uint)hwnd.ToInt32()).ToString("X") : hwnd.ToInt64().ToString("X")), ProcessId = pid, ProcessImage = basename,
                    ProcessStartTime = created, Foreground = !minimized && GetForegroundWindow() == hwnd,
                    Minimized = minimized, Width = width, Height = height
                };
            } finally { CloseHandle(process); }
        }

        public static DesktopWindow[] Find(string nonce, string distro) {
            return FindWindows(nonce, distro, false);
        }

        public static DesktopWindow[] FindIncludingMinimized(string nonce, string distro) {
            return FindWindows(nonce, distro, true);
        }

        private static DesktopWindow[] FindWindows(string nonce, string distro, bool includeMinimized) {
            if (!ValidNonce(nonce)) throw new ArgumentException("Expected 32 lowercase hexadecimal characters.", "nonce");
            if (String.IsNullOrEmpty(distro)) throw new ArgumentException("Expected the selected distribution.", "distro");
            string marker = "Cloud Linux Workspace [" + nonce + "]";
            var windows = new List<DesktopWindow>();
            if (!EnumWindows(delegate(IntPtr hwnd, IntPtr unused) {
                var observation = Observe(hwnd, marker, distro, includeMinimized);
                if (observation != null) windows.Add(observation);
                return true;
            }, IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error());
            return windows.ToArray();
        }
    }
}
