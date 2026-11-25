# Interactive Windows installation acceptance. Creates a temporary PNG only.
[CmdletBinding()] param([Parameter(Mandatory=$true)][ValidateSet('ChatGPT','Claude')][string]$App)
$ErrorActionPreference='Stop'
$file=Join-Path ([IO.Path]::GetTempPath()) (('official-app-icon-'+[guid]::NewGuid())+'.png')
$launcher=Join-Path (Split-Path $PSScriptRoot) 'windows/Open-OfficialApp.ps1'
try {
    $r=(& $launcher -App $App -IconPath $file) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $r.status -ne 'icon-written') {
        @{app=$App;status=$r.status;source=$r.source} | ConvertTo-Json -Compress
        if ($r.status -eq 'not-installed') { exit 2 }; exit 3
    }
    Add-Type -AssemblyName System.Drawing
    $bitmap=[Drawing.Bitmap]::new($file)
    try {
        if ($bitmap.RawFormat.Guid -ne [Drawing.Imaging.ImageFormat]::Png.Guid -or $bitmap.Width -lt 1 -or $bitmap.Height -lt 1) { throw 'Expected PNG.' }
        @{app=$App;status='registered-icon-decoded';channel=$r.channel;width=$bitmap.Width;height=$bitmap.Height} | ConvertTo-Json -Compress
    } finally { $bitmap.Dispose() }
} finally { if ([IO.File]::Exists($file)) { [IO.File]::Delete($file) } }
