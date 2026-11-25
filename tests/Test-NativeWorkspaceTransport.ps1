# Exercise the real process boundary with this PowerShell executable standing in for wsl.exe.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $repo 'windows/Test-NativeWorkspace.ps1')
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('workspace args ' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $tmp
$priorKey = $env:INSTABOX_API_KEY
$priorList = $env:WSLENV
$priorMarker = $env:CLOUDWORKSPACE_VERIFY_ID
try {
    $env:INSTABOX_API_KEY = 'inert-test-only'
    $env:CLOUDWORKSPACE_VERIFY_ID = 'unrelated'
    $env:WSLENV = 'KEEP/p:instabox_api_key/u:CLOUDWORKSPACE_VERIFY_ID/w:KEEP2'
    $child = Join-Path $tmp 'inspect.ps1'
    $code = @'
$ErrorActionPreference = 'Stop'
switch ($args[0]) {
    'argv' {
        $result = @{ argv=@($args[1..($args.Count - 1)]); list=$env:WSLENV
          keyPresent=[bool]$env:INSTABOX_API_KEY; markerPresent=[bool]$env:CLOUDWORKSPACE_VERIFY_ID }
        $bytes = [Text.Encoding]::UTF8.GetBytes(($result | ConvertTo-Json -Compress))
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
        $data.markerPresent -or $data.list -cne 'KEEP/p:KEEP2') { throw 'Incorrect process arguments or environment.' }
    $listing = Invoke-WorkspaceWsl $system ($prefix+'list') 10
    if ($listing.Text.Trim() -cne 'Ubuntu-24.04') { throw 'BOM-less UTF-16LE listing was not decoded.' }
    try { $null = Invoke-WorkspaceWsl $system ($prefix+'sleep') 1; throw 'Missing process timeout.' }
    catch { if ($_.Exception.Data['WorkspaceCode'] -ne 'wsl-timeout') { throw } }
    if ($env:INSTABOX_API_KEY -ne 'inert-test-only' -or $env:CLOUDWORKSPACE_VERIFY_ID -ne 'unrelated') { throw 'Parent environment was modified.' }
    'Process transport passed.'
} finally {
    $env:INSTABOX_API_KEY = $priorKey
    $env:CLOUDWORKSPACE_VERIFY_ID = $priorMarker
    $env:WSLENV = $priorList
    if (Test-Path -LiteralPath $child -PathType Leaf) { Remove-Item -LiteralPath $child }
    [IO.Directory]::Delete($tmp)
}
