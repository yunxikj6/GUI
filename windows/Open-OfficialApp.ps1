# Windows PowerShell 5.1. All discovery is local to the current Windows user.
[CmdletBinding(DefaultParameterSetName='Launch')]
param(
    [Parameter(Mandatory = $true)][ValidateSet('ChatGPT', 'Claude')][string]$App,
    [Parameter(ParameterSetName='Check')][switch]$Check,
    [Parameter(Mandatory=$true,ParameterSetName='Icon')][ValidateNotNullOrEmpty()][string]$IconPath
)
$ErrorActionPreference = 'Stop'
$source = if ($App -eq 'ChatGPT') { 'https://chatgpt.com/download' } else { 'https://claude.com/download' }
$result = [ordered]@{ app = $App; status = 'error'; source = $source }

function Test-ClaudeSignature([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) { return $false }
    $signer = $signature.SignerCertificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    return $signer -in @('Anthropic, PBC', 'Anthropic PBC')
}

function Export-RegisteredIcon($Target, [string]$Destination) {
    if ($Destination -notmatch '^(?:[A-Za-z]:[\\/]|\\\\(?:wsl\$|wsl\.localhost)\\[^\\]+\\)' -or
        [IO.Path]::GetExtension($Destination) -ne '.png') { throw 'Use an absolute local or WSL path ending in .png.' }
    if (-not ('OfficialDesktop.ShellIcon' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'OfficialAppIcon.cs') }
    $shellItem = if ($Target.id) { 'shell:AppsFolder\' + $Target.id } else { $Target.path }
    $pixels = [OfficialDesktop.ShellIcon]::Read($shellItem)
    Add-Type -AssemblyName System.Drawing
    $format = [Drawing.Imaging.PixelFormat]::Format32bppPArgb
    $bitmap = [Drawing.Bitmap]::new($pixels.Width, $pixels.Height, $format)
    $encoded = [IO.MemoryStream]::new()
    try {
        $bounds = [Drawing.Rectangle]::new(0, 0, $pixels.Width, $pixels.Height)
        $locked = $bitmap.LockBits($bounds, [Drawing.Imaging.ImageLockMode]::WriteOnly, $format)
        try {
            $rowBytes = $pixels.Width * 4
            for ($row=0; $row -lt $pixels.Height; $row++) {
                $address = [IntPtr]::Add($locked.Scan0, $row * $locked.Stride)
                [Runtime.InteropServices.Marshal]::Copy($pixels.Bgra, $row * $rowBytes, $address, $rowBytes)
            }
        } finally { $bitmap.UnlockBits($locked) }
        $bitmap.Save($encoded, [Drawing.Imaging.ImageFormat]::Png)
        $out = [IO.File]::Open($Destination, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            if ($out.Length -ne 0) { throw 'The icon destination must be empty or new.' }
            try { $encoded.WriteTo($out); $out.Flush() } catch { $out.SetLength(0); throw }
        } finally { $out.Dispose() }
    } finally { $bitmap.Dispose(); $encoded.Dispose() }
}

function Invoke-PackagedApp([string]$Id) {
    # IApplicationActivationManager reports the activation HRESULT and the application PID.
    if (-not ('OfficialDesktop.Activation' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace OfficialDesktop {
    [ComImport, Guid("2E941141-7F97-4756-BA1D-9DECDE894A3D"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivationManager {
        [PreserveSig] int ActivateApplication(
            [MarshalAs(UnmanagedType.LPWStr)] string appId,
            [MarshalAs(UnmanagedType.LPWStr)] string arguments, uint options, out uint processId);
    }
    [ComImport, Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")]
    class ApplicationActivationManager {}
    public static class Activation {
        public static uint Request(string id) {
            var manager = (IActivationManager)new ApplicationActivationManager();
            try {
                uint pid;
                Marshal.ThrowExceptionForHR(manager.ActivateApplication(id, null, 0, out pid));
                return pid;
            } finally { Marshal.ReleaseComObject(manager); }
        }
    }
}
'@
    }
    return [OfficialDesktop.Activation]::Request($Id)
}

try {
    if (-not (Get-Command Get-AppxPackage -ErrorAction SilentlyContinue)) {
        throw 'Run this entry point with Windows PowerShell on the Windows desktop.'
    }
    # Ordered by supported product/channel. The Codex family also has older Codex-only builds.
    $channels = if ($App -eq 'ChatGPT') {
        @(
            @{ name='OpenAI.Codex'; family='OpenAI.Codex_2p2nqsd0c76g0'; id='App'; label='ChatGPT'; modern=$true }
            @{ name='OpenAI.ChatGPT-Desktop'; family='OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0'; id='ChatGPT'; label='ChatGPT Classic' }
        )
    } else {
        @(
            @{ name='AnthropicPBC.Claude'; family='AnthropicPBC.Claude_fnn82j28hfe8t'; id='Claude'; label='Claude (Microsoft Store)' }
            @{ name='Claude'; family='Claude_pzs8sxrjxfjjc'; id='Claude'; label='Claude (Anthropic MSIX)'; direct=$true }
        )
    }
    $target = $null
    $rejected = $false
    foreach ($channel in $channels) {
        $packages = @(Get-AppxPackage -Name $channel.name -ErrorAction Stop | Sort-Object Version -Descending)
        foreach ($package in $packages) {
            if ($package.PackageFamilyName -ne $channel.family -or $package.Status -ne 'Ok' -or $package.IsDevelopmentMode -ne $false) {
                $rejected = $true
                continue
            }
            if ($channel.direct) {
                if ($package.SignatureKind -notin @('Store', 'Developer', 'Enterprise')) { $rejected = $true; continue }
            } elseif ($package.SignatureKind -ne 'Store') { $rejected = $true; continue }

            $manifest = Get-AppxPackageManifest -Package $package.PackageFullName -ErrorAction Stop
            $entry = @($manifest.Package.Applications.Application | Where-Object { $_.Id -eq $channel.id })
            if ($entry.Count -ne 1) { $rejected = $true; continue }
            $id = $channel.family + '!' + $channel.id
            if ($channel.modern) {
                $visible = @(Get-StartApps -ErrorAction Stop | Where-Object { $_.AppID -eq $id -and $_.Name -match '^ChatGPT(?:\s|$)' })
                if ($visible.Count -eq 0) { continue }
            }
            if ($channel.direct) {
                if ($entry[0].Executable -ne 'app\Claude.exe') { $rejected = $true; continue }
                $path = Join-Path $package.InstallLocation 'app\Claude.exe'
                if (-not (Test-ClaudeSignature $path)) { $rejected = $true; continue }
            }
            $target = @{ id = $id; channel = $channel.label }
            break
        }
        if ($target) { break }
    }

    # Historical per-user desktop installer. This path is distinct from the Claude CLI.
    if (-not $target -and $App -eq 'Claude') {
        $local = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
        $path = Join-Path $local 'AnthropicClaude\Claude.exe'
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $product = (Get-Item -LiteralPath $path).VersionInfo.ProductName
            if ($product -eq 'Claude' -and (Test-ClaudeSignature $path)) {
                $target = @{ path = $path; channel = 'Claude (signed Windows desktop)' }
            } else { $rejected = $true }
        }
    }
    if (-not $target) {
        $result.status = if ($rejected) { 'unverified' } else { 'not-installed' }
        $result.message = if ($rejected) { "The installed identity could not be verified. Official download: $source" } else { "A supported $App desktop app was not found. Official download: $source" }
        $result | ConvertTo-Json -Compress
        if ($rejected) { exit 3 }
        exit 2
    }
    $result.channel = $target.channel
    if ($target.id) { $result.aumid = $target.id } else { $result.executable = $target.path }
    if ($PSCmdlet.ParameterSetName -eq 'Icon') {
        Export-RegisteredIcon $target $IconPath
        $result.status = 'icon-written'
    } elseif ($Check) {
        $result.status = 'verified'
    } else {
        if ($target.id) {
            $result.processId = Invoke-PackagedApp $target.id
        } else {
            $result.processId = (Start-Process -FilePath $target.path -PassThru).Id
        }
        $result.status = 'activation-requested'
    }
    $result | ConvertTo-Json -Compress
    exit 0
} catch {
    $result.message = $_.Exception.Message
    $result | ConvertTo-Json -Compress
    exit 3
}
