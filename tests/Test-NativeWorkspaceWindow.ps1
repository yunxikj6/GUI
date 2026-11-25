# Cross-platform pure rules. No calls to Win32 and no observation of a user desktop.
$ErrorActionPreference = 'Stop'
$source = Get-Content -Raw -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'windows/WorkspaceWindowProbe.cs')
$tests = @'
namespace CloudWorkspace {
    public static class WindowRuleTests {
        private static int count;
        private static void Assert(bool yes) { if (!yes) throw new System.Exception("Workspace window rule failed at check " + count); count++; }
        public static int Run() {
            count=0;
            string nonce="0123456789abcdef0123456789abcdef";
            string marker="Cloud Linux Workspace [" + nonce + "]";
            Assert(DesktopWindow.ValidNonce(nonce));
            foreach (string bad in new string[] {null,"","abc",nonce+"a",nonce.ToUpperInvariant(),nonce+"\n","01234567-89ab-cdef-0123-456789abcdef",nonce.Substring(1)+"g","１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２"}) {
                Assert(!DesktopWindow.ValidNonce(bad));
                try { DesktopWindow.Find(bad, "Ubuntu-24.04"); Assert(false); } catch (System.ArgumentException) { Assert(true); }
                try { DesktopWindow.FindIncludingMinimized(bad, "Ubuntu-24.04"); Assert(false); } catch (System.ArgumentException) { Assert(true); }
            }
            Assert(DesktopWindow.MatchesCaption(marker,marker,"Ubuntu"));
            foreach(string distro in new string[]{"Ubuntu","Ubuntu-24.04","Ubuntu 24.04-LTS","Linux","Team_日本","Ubuntu (Dev) [local]",new string('a',255)})
                Assert(DesktopWindow.MatchesCaption(marker+" ("+distro+")",marker,distro));
            foreach(string bad in new string[]{null,"",marker+"x",marker+" ()",marker+" ( Ubuntu)",marker+" (Ubuntu )",marker+" (Ubuntu) - Editor",
                    "ChatGPT - "+marker,"[WARN:COPY MODE] "+marker+" (Linux)",marker+" - Word",marker+" (Ubuntu) (active)",
                    marker+" ([Ubuntu])",marker+" ((Ubuntu))",marker+" (Ubuntu\rtest)",marker+" (Ubuntu\u2028text)",marker+"\n",
                    marker.ToLowerInvariant(),marker+" ("+new string('a',256)+")","Cloud Linux Workspace ["+nonce.Substring(1)+"a]" })
                Assert(!DesktopWindow.MatchesCaption(bad,marker,"Ubuntu"));
            Assert(!DesktopWindow.MatchesCaption(marker+" (Ubuntu)",marker,"Another distro"));
            Assert(DesktopWindow.MatchesCaption(marker+" (Linux)",marker,"Another distro"));
            Assert(DesktopWindow.SupportedHost("MsRdC.exe"));
            Assert(DesktopWindow.SupportedHost("mstsc.EXE"));
            foreach (string fail in new string[]{null,"wsl.exe","wslg.exe","wslhost.exe","notepad.exe","mstsc.exe.config","chatgpt.exe","C:\\Windows\\mstsc.exe"}) Assert(!DesktopWindow.SupportedHost(fail));
            Assert(DesktopWindow.VisibleState(true, false, 0, 0, 1, 1));
            Assert(!DesktopWindow.VisibleState(false, false, 0, 0, 128, 100));
            Assert(!DesktopWindow.VisibleState(true, true, 0, 0, 128, 100));
            Assert(!DesktopWindow.VisibleState(true, false, -1, 0, 128, 100));
            Assert(!DesktopWindow.VisibleState(true, false, 0, 1, 128, 100));
            Assert(!DesktopWindow.VisibleState(true, false, 0, 2, 128, 100));
            Assert(!DesktopWindow.VisibleState(true, false, 0, 0, 0, 100));
            Assert(!DesktopWindow.VisibleState(true, false, 0, 0, 128, -1));
            Assert(DesktopWindow.MatchesPresentation(true, true, 0, 0, true));
            Assert(!DesktopWindow.MatchesPresentation(false, true, 0, 0, true));
            Assert(!DesktopWindow.MatchesPresentation(true, false, 0, 0, true));
            Assert(!DesktopWindow.MatchesPresentation(true, true, -1, 0, true));
            foreach (uint flag in new uint[]{1,2,4,7}) Assert(!DesktopWindow.MatchesPresentation(true, true, 0, flag, true));
            Assert(!DesktopWindow.MatchesPresentation(true, true, 0, 0, false));
            Assert(DesktopWindow.MatchesPresentation(true, false, 0, 0, false));
            string[] expected={"Foreground","Height","Hwnd","Minimized","ProcessId","ProcessImage","ProcessStartTime","Width"};
            var props=typeof(DesktopWindow).GetProperties();
            string[] actual=new string[props.Length];for(int i=0;i<props.Length;i++)actual[i]=props[i].Name;
            System.Array.Sort(actual,System.StringComparer.Ordinal);
            Assert(System.String.Join(",",expected)==System.String.Join(",",actual));
            Assert(typeof(DesktopWindow).GetMethod("Find",new System.Type[]{typeof(string),typeof(string)}).ReturnType==typeof(DesktopWindow[]));
            Assert(typeof(DesktopWindow).GetMethod("FindIncludingMinimized",new System.Type[]{typeof(string),typeof(string)}).ReturnType==typeof(DesktopWindow[]));
            var minimized=typeof(DesktopWindow).GetProperty("Minimized");
            Assert(minimized.PropertyType==typeof(bool) && minimized.GetGetMethod()!=null && minimized.GetSetMethod(true).IsPrivate);
            return count;
        }
    }
}
'@
Add-Type -TypeDefinition ($source + [Environment]::NewLine + $tests)
"Pure rules passed: $([CloudWorkspace.WindowRuleTests]::Run())"
