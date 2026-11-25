# Synthetic snapshots test the before/after classifications. No OS window is opened.
$ErrorActionPreference='Stop'
$tmp=Join-Path ([IO.Path]::GetTempPath()) ('official synthetic '+[guid]::NewGuid())
$envNames=@('PROCESSOR_ARCHITECTURE','PROCESSOR_ARCHITEW6432','OBS_RECEIPT','OBS_EXIT','OBS_LOG','OBS_ID','OBS_CHANNEL')
$saved=@{}
foreach ($name in $envNames) { $saved[$name]=[Environment]::GetEnvironmentVariable($name) }
[IO.Directory]::CreateDirectory($tmp) | Out-Null
try {
    Copy-Item (Join-Path (Split-Path $PSScriptRoot) 'windows/Test-OfficialAppWindow.ps1') $tmp
    @'
param($App,[switch]$Check)
if (-not $Check) { throw 'The observation script activated with PowerShell instead of the native executable.' }
[ordered]@{status='verified';channel=$env:OBS_CHANNEL;aumid=$env:OBS_ID;source='https://claude.com/download'} | ConvertTo-Json -Compress
exit 0
'@ | Set-Content (Join-Path $tmp 'Open-OfficialApp.ps1')

    $family='AnthropicPBC.Claude_fnn82j28hfe8t'
    $full='AnthropicPBC.Claude_1.2.3.4_x64__fnn82j28hfe8t'
    $env:OBS_CHANNEL='Claude (Microsoft Store)'
    $env:OBS_ID="$family!Claude"
    $env:OBS_LOG=Join-Path $tmp 'argv.txt'
    $env:OBS_EXIT='0'
    $goodReceipt=[ordered]@{ app='Claude';status='activation-requested';channel=$env:OBS_CHANNEL
        aumid=$env:OBS_ID;packageFullName=$full;processId=222 }
    $env:OBS_RECEIPT=$goodReceipt | ConvertTo-Json -Compress
    function Get-AppxPackage {
        [pscustomobject]@{PackageFamilyName=$family;Status='Ok';IsDevelopmentMode=$false;
            PackageFullName=$full;Version=[Version]'1.2.3.4';InstallLocation=(Join-Path $tmp 'private-marker')}
    }
    $manifestApp='Claude'
    $manifestExe='Claude.exe'
    function Get-AppxPackageManifest {
        [pscustomobject]@{Package=@{Applications=@{Application=@{Id=$manifestApp;Executable=$manifestExe}}}}
    }
    $fixture=Join-Path $tmp 'CloudLinuxWorkspace-x64.exe'
    if ([Environment]::OSVersion.Platform -eq 'Win32NT') {
        $source=Join-Path $tmp 'fixture.cs'
        @'
using System;
using System.IO;
class App {
    static int Main(string[] args) {
        File.AppendAllLines(Environment.GetEnvironmentVariable("OBS_LOG"), Environment.GetCommandLineArgs());
        Console.WriteLine(Environment.GetEnvironmentVariable("OBS_RECEIPT"));
        return Int32.Parse(Environment.GetEnvironmentVariable("OBS_EXIT"));
    }
}
'@ | Set-Content $source
        $csc=Join-Path $env:SystemRoot 'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        & $csc /nologo /target:exe "/out:$fixture" $source
        if ($LASTEXITCODE -ne 0) { throw 'Could not build the local argv fixture.' }
    } else {
        $shim=@'
#!/bin/sh
printf '%s\n' "$0" "$@" >> "$OBS_LOG"
printf '%s\n' "$OBS_RECEIPT"
exit "$OBS_EXIT"
'@
        [IO.File]::WriteAllText($fixture,$shim,(New-Object Text.UTF8Encoding $false))
        & chmod u+x $fixture
        if ($LASTEXITCODE -ne 0) { throw 'Could not mark fixture executable.' }
    }
    Copy-Item $fixture (Join-Path $tmp 'CloudLinuxWorkspace-arm64.exe')
    Add-Type -TypeDefinition @'
namespace OfficialDesktop {
    public class FixtureWindow {
        public string Hwnd="0x12",ContentHwnd="0x12",ImagePath;
        public uint WindowPid=1,AppPid=1;
        public long ProcessCreation=4;
        public bool Hosted,Foreground;
        public string Family,PackageFullName,ProcessAumid;
    }
    public static class WindowProbe {
        public static string Mode;
        public static int Count;
        public static FixtureWindow[] Snapshot(string family,string full,string aumid,string image) {
            int n=Count++;
            if ((Mode=="new" && n==0) || (Mode=="transient" && n!=1)) return new FixtureWindow[0];
            return new [] {new FixtureWindow { ImagePath=image,Family=family,PackageFullName=full,ProcessAumid=aumid,Foreground=Mode=="focused" && n>0,ProcessCreation=(Mode=="reused" && n>0) ? 5 : 4 }};
        }
    }
}
'@
    foreach ($c in @(@('new','new-window'),@('reused','new-window'),@('focused','existing-window-became-foreground'),@('existing','already-visible'),@('transient','window-timeout'))) {
        [OfficialDesktop.WindowProbe]::Mode=$c[0];[OfficialDesktop.WindowProbe]::Count=0
        $env:PROCESSOR_ARCHITECTURE='AMD64'
        $env:PROCESSOR_ARCHITEW6432=if ($c[0] -eq 'focused') { 'ARM64' } else { $null }
        $expected=if ($env:PROCESSOR_ARCHITEW6432) { 'CloudLinuxWorkspace-arm64.exe' } else { 'CloudLinuxWorkspace-x64.exe' }
        Remove-Item -LiteralPath $env:OBS_LOG -Force -ErrorAction SilentlyContinue
        $out=& (Join-Path $tmp 'Test-OfficialAppWindow.ps1') -App Claude -TimeoutSeconds 3
        $code=$LASTEXITCODE
        $r=$out | ConvertFrom-Json
        $argv=@(Get-Content -LiteralPath $env:OBS_LOG)
        if ($argv.Count -ne 3 -or (Split-Path -Leaf $argv[0]) -ne $expected -or
            $argv[1] -cne '--open-app' -or $argv[2] -cne 'Claude') { throw "Unexpected native argv: $argv" }
        if ($r.activationLauncher -ne $expected -or $r.launcherExitCode -ne 0 -or $r.activationPid -ne 222 -or
            $r.packageFullName -ne $full -or $r.aumid -ne $env:OBS_ID) { throw $out }
        if ($out -match 'private-marker') { throw 'Location present in report' }
        if ($c[1] -eq 'window-timeout') {
            if ($code -ne 4 -or $r.status -ne 'window-timeout') { throw $out }
        } elseif ($code -ne 0 -or $r.observation -ne $c[1] -or $r.after[0].appImage -ne 'Claude.exe' -or $r.after[0].appPid -eq $r.activationPid) { throw $out }
    }
    foreach ($bad in @('wrong-version','wrong-id','no-pid','failed')) {
        $receipt=$goodReceipt | ConvertTo-Json -Compress | ConvertFrom-Json
        $env:OBS_EXIT='0'
        switch ($bad) {
            'wrong-version' { $receipt.packageFullName='an-older-version' }
            'wrong-id' { $receipt.aumid="$family!Helper" }
            'no-pid' { $receipt.processId=0 }
            'failed' { $env:OBS_EXIT='7' }
        }
        $env:OBS_RECEIPT=$receipt | ConvertTo-Json -Compress
        $out=& (Join-Path $tmp 'Test-OfficialAppWindow.ps1') -App Claude -TimeoutSeconds 3
        $code=$LASTEXITCODE
        $r=$out | ConvertFrom-Json
        if ($code -ne 3 -or $r.stage -ne 'activate' -or $r.launcherExitCode -ne [int]$env:OBS_EXIT -or $r.after.Count) { throw $out }
    }
    Remove-Item -LiteralPath $env:OBS_LOG -Force
    $env:OBS_ID='Claude_pzs8sxrjxfjjc!Claude'
    $out=& (Join-Path $tmp 'Test-OfficialAppWindow.ps1') -App Claude -TimeoutSeconds 3
    $code=$LASTEXITCODE
    $r=$out | ConvertFrom-Json
    if ($code -ne 2 -or $r.status -ne 'unsupported-channel' -or [IO.File]::Exists($env:OBS_LOG)) { throw $out }
    $family='OpenAI.Codex_2p2nqsd0c76g0'
    $full='OpenAI.Codex_1.2.3.4_x64__2p2nqsd0c76g0'
    $env:OBS_ID="$family!App"
    $env:OBS_EXIT='0'
    $env:OBS_CHANNEL='ChatGPT'
    $manifestApp='App'; $manifestExe='ChatGPT.exe'
    $env:OBS_RECEIPT=@{app='ChatGPT';status='activation-requested';channel='ChatGPT';aumid=$env:OBS_ID;
        packageFullName=$full;processId=333} | ConvertTo-Json -Compress
    [OfficialDesktop.WindowProbe]::Mode='new'; [OfficialDesktop.WindowProbe]::Count=0
    $out=& (Join-Path $tmp 'Test-OfficialAppWindow.ps1') -App ChatGPT -TimeoutSeconds 3
    $code=$LASTEXITCODE
    $r=$out | ConvertFrom-Json
    $argv=@(Get-Content -LiteralPath $env:OBS_LOG)
    if ($code -ne 0 -or $argv.Count -ne 3 -or $argv[1] -cne '--open-app' -or $argv[2] -cne 'ChatGPT' -or
        $r.observation -ne 'new-window' -or $r.after[0].appImage -ne 'ChatGPT.exe' -or $r.activationPid -ne 333) { throw $out }
    'Five window transitions, native argv/architecture, receipt failures and unsupported direct-install passed.'
} finally {
    foreach ($name in $envNames) { [Environment]::SetEnvironmentVariable($name,$saved[$name]) }
    [IO.Directory]::Delete($tmp,$true)
}
