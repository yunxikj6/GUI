# Exercise the real process boundary with this PowerShell executable standing in for wsl.exe.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $repo 'windows/Test-NativeWorkspace.ps1')
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('workspace args ' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $tmp
$priorKey = $env:INSTABOX_API_KEY
$priorList = $env:WSLENV
$priorMarker = $env:CLOUDWORKSPACE_VERIFY_ID
$priorApps = $env:CLOUDWORKSPACE_VERIFY_APPS
$priorEngine = $env:WORKSPACE_TEST_TRANSPORT_ENGINE
try {
    $env:INSTABOX_API_KEY = 'inert-test-only'
    $env:CLOUDWORKSPACE_VERIFY_ID = 'unrelated'
    $env:CLOUDWORKSPACE_VERIFY_APPS = 'unrelated-apps'
    $testList = 'KEEP/p:instabox_api_key/u:CLOUDWORKSPACE_VERIFY_ID/w:cloudworkspace_verify_apps/l:KEEP2:CLOUDWORKSPACE_VERIFY_APPS'
    $env:WSLENV = $testList
    $child = Join-Path $tmp 'inspect.ps1'
    $code = @'
$ErrorActionPreference = 'Stop'
switch ($args[0]) {
    'argv' {
        $result = @{ argv=@($args[1..($args.Count - 1)]); list=$env:WSLENV
          keyPresent=($null -ne [Environment]::GetEnvironmentVariable('INSTABOX_API_KEY'))
          markerPresent=($null -ne [Environment]::GetEnvironmentVariable('CLOUDWORKSPACE_VERIFY_ID'))
          appsPresent=($null -ne [Environment]::GetEnvironmentVariable('CLOUDWORKSPACE_VERIFY_APPS')) }
        $bytes = [Text.Encoding]::UTF8.GetBytes(($result | ConvertTo-Json -Compress))
    }
    'desktop' {
        $result = @{ id=$env:CLOUDWORKSPACE_VERIFY_ID; apps=[Environment]::GetEnvironmentVariable('CLOUDWORKSPACE_VERIFY_APPS')
            list=$env:WSLENV; keyPresent=($null -ne [Environment]::GetEnvironmentVariable('INSTABOX_API_KEY')) }
        [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'desktop.json'), ($result | ConvertTo-Json -Compress))
        exit 0
    }
    'list' { $bytes = [Text.Encoding]::Unicode.GetBytes("Ubuntu-24.04`r`n") }
    'sleep' { Start-Sleep -Seconds 10; exit 0 }
}
$out = [Console]::OpenStandardOutput()
$out.Write($bytes,0,$bytes.Length)
'@
    [IO.File]::WriteAllText($child,$code,[Text.UTF8Encoding]::new($true))
    $system = [pscustomobject]@{Wsl=(Get-Process -Id $PID).Path}
    $prefix = @('-NoLogo','-NoProfile','-File',$child)
    $values = @('plain','team space','C:\ends with space\','a\"b', '100% & hello (QA)!', 'x\y')
    $r = Invoke-WorkspaceWsl $system ($prefix + 'argv' + $values) 10
    $data = $r.Text | ConvertFrom-Json
    if ($r.Code -or ($data.argv -join '|') -cne ($values -join '|') -or $data.keyPresent -or
        $data.markerPresent -or $data.appsPresent -or $data.list -cne 'KEEP/p:KEEP2') {
        throw 'Incorrect process arguments or environment.'
    }
    $listing = Invoke-WorkspaceWsl $system ($prefix+'list') 10
    if ($listing.Text.Trim() -cne 'Ubuntu-24.04') { throw 'BOM-less UTF-16LE listing was not decoded.' }
    try { $null = Invoke-WorkspaceWsl $system ($prefix+'sleep') 1; throw 'Missing process timeout.' }
    catch { if ($_.Exception.Data['WorkspaceCode'] -ne 'wsl-timeout') { throw } }

    # Start-TestDesktop launches an inert local entry here. Inspect the environment in its actual child.
    $env:WORKSPACE_TEST_TRANSPORT_ENGINE = $system.Wsl
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        $cmd = Join-Path ([Environment]::SystemDirectory) 'cmd.exe'
        $launcherFixture = Join-Path $tmp 'Start.cmd'
        $batch = '@"%WORKSPACE_TEST_TRANSPORT_ENGINE%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0inspect.ps1" desktop'
        [IO.File]::WriteAllText($launcherFixture, $batch + "`r`n", [Text.Encoding]::ASCII)
    } else {
        $cmd = Join-Path $tmp 'fixture-cmd'
        $launcherFixture = $cmd
        $shell = @'
#!/bin/sh
exec "$WORKSPACE_TEST_TRANSPORT_ENGINE" -NoLogo -NoProfile -NonInteractive -File "$(dirname "$0")/inspect.ps1" desktop
'@
        [IO.File]::WriteAllText($launcherFixture, $shell.Replace("`r`n","`n") + "`n", [Text.UTF8Encoding]::new($false))
        & chmod 700 $launcherFixture
        if ($LASTEXITCODE -ne 0) { throw 'Cannot prepare the inert process fixture.' }
    }
    $desktopReceipt = Join-Path $tmp 'desktop.json'
    $nonce = '0123456789abcdef0123456789abcdef'
    foreach ($optIn in @($false,$true)) {
        $proc = Start-TestDesktop @{Cmd=$cmd} $tmp 'Ubuntu fixture' $nonce $optIn
        try {
            if (-not $proc.WaitForExit(15000)) { $proc.Kill(); throw 'Desktop process fixture timed out.' }
            if ($proc.ExitCode -ne 0) { throw 'Desktop process fixture failed.' }
        } finally { $proc.Dispose() }
        $data = [IO.File]::ReadAllText($desktopReceipt) | ConvertFrom-Json
        [IO.File]::Delete($desktopReceipt)
        $expectedList = 'KEEP/p:KEEP2:CLOUDWORKSPACE_VERIFY_ID/u'
        if ($optIn) { $expectedList += ':CLOUDWORKSPACE_VERIFY_APPS/u' }
        if ($data.id -cne $nonce -or $data.keyPresent -or $data.list -cne $expectedList -or
            ($optIn -and $data.apps -cne '1') -or (-not $optIn -and $null -ne $data.apps)) {
            throw 'Desktop child received incorrect verification markers.'
        }
    }
    if ($env:INSTABOX_API_KEY -ne 'inert-test-only' -or $env:CLOUDWORKSPACE_VERIFY_ID -ne 'unrelated' -or
        $env:CLOUDWORKSPACE_VERIFY_APPS -ne 'unrelated-apps' -or $env:WSLENV -cne $testList) {
        throw 'Parent environment was modified.'
    }
    'Process transport passed.'
} finally {
    $env:INSTABOX_API_KEY = $priorKey
    $env:CLOUDWORKSPACE_VERIFY_ID = $priorMarker
    $env:CLOUDWORKSPACE_VERIFY_APPS = $priorApps
    $env:WSLENV = $priorList
    $env:WORKSPACE_TEST_TRANSPORT_ENGINE = $priorEngine
    if (Test-Path -LiteralPath $child -PathType Leaf) { Remove-Item -LiteralPath $child }
    if ($launcherFixture) { [IO.File]::Delete($launcherFixture) }
    if ($desktopReceipt) { [IO.File]::Delete($desktopReceipt) }
    [IO.Directory]::Delete($tmp)
}
