# Run in the signed-in Windows user's interactive session. Writes one JSON object to stdout.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('ChatGPT','Claude')][string]$App,
    [ValidateRange(3,180)][int]$TimeoutSeconds = 30
)
$ErrorActionPreference = 'Stop'
$identityReader = Join-Path $PSScriptRoot 'Open-OfficialApp.ps1'
$App = if ($App -eq 'Claude') { 'Claude' } else { 'ChatGPT' }
$outputRecord = [ordered]@{ app=$App; status='error'; before=@(); after=@() }
$phase = 'windows-environment'

function Read-WindowSnapshot {
    [OfficialDesktop.WindowProbe]::Snapshot($family,$registeredName,$aumid,$imagePath)
}
function Window-Key($window) {
    return "$($window.Hwnd)/$($window.WindowPid)/$($window.AppPid)/$($window.ProcessCreation)"
}
function Public-Windows($snapshot) {
    foreach ($w in $snapshot) {
        [ordered]@{
            hwnd=$w.Hwnd; windowPid=$w.WindowPid; appHwnd=$w.ContentHwnd; appPid=$w.AppPid
            appImage=($w.ImagePath -replace '^.*[\\/]',''); packageFamily=$w.Family; packageFullName=$w.PackageFullName
            processAumid=$w.ProcessAumid; hosted=$w.Hosted; visible=$true; foreground=$w.Foreground
        }
    }
}

try {
    if (-not (Get-Command Get-AppxPackage -ErrorAction SilentlyContinue) -or -not [Environment]::UserInteractive) {
        throw 'An interactive Windows desktop is required.'
    }
    $phase='verify-installation'
    $checked = (& $identityReader -App $App -Check) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $checked.status -ne 'verified') {
        $outputRecord.status=$checked.status
        $outputRecord.source=$checked.source
        $outputRecord | ConvertTo-Json -Depth 5 -Compress
        if ($checked.status -eq 'not-installed') { exit 2 }; exit 3
    }
    $aumid=[string]$checked.aumid
    $outputRecord.channel=$checked.channel
    $outputRecord.aumid=$aumid
    if (-not $aumid -or ($App -eq 'Claude' -and $aumid -ne 'AnthropicPBC.Claude_fnn82j28hfe8t!Claude')) {
        # The optional identity reader also recognizes vendor-direct Claude installs.
        $outputRecord.status='unsupported-channel'
        $outputRecord.source=$checked.source
        $outputRecord | ConvertTo-Json -Depth 5 -Compress
        exit 2
    }
    $family,$applicationId = $aumid -split '!',2
    $packages=@(Get-AppxPackage -Name ($family.Split('_')[0]) |
        Where-Object { $_.PackageFamilyName -eq $family -and $_.Status -eq 'Ok' -and $_.IsDevelopmentMode -eq $false } |
        Sort-Object Version -Descending)
    if ($packages.Count -ne 1) { throw 'The package registration changed or is ambiguous.' }
    $package=$packages[0]
    $registeredName=$package.PackageFullName
    $manifest=Get-AppxPackageManifest -Package $registeredName
    $entry=@($manifest.Package.Applications.Application | Where-Object { $_.Id -eq $applicationId })
    if ($entry.Count -ne 1 -or -not $entry[0].Executable) { throw 'The registered application has no unambiguous executable.' }
    $separator=[IO.Path]::DirectorySeparatorChar
    $root=[IO.Path]::GetFullPath($package.InstallLocation).TrimEnd($separator)+$separator
    $imagePath=[IO.Path]::GetFullPath((Join-Path $root ([string]$entry[0].Executable)))
    if (-not $imagePath.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected package image path.' }
    $outputRecord.packageFullName=$registeredName

    $phase='resolve-native-launcher'
    $machine=if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $arch=switch ($machine) { 'AMD64' { 'x64' } 'ARM64' { 'arm64' } default { throw 'Unsupported Windows architecture.' } }
    $outputRecord.activationLauncher="CloudLinuxWorkspace-$arch.exe"
    $native=Join-Path $PSScriptRoot $outputRecord.activationLauncher
    if (-not (Test-Path -LiteralPath $native -PathType Leaf)) { throw 'The native launcher is missing.' }
    $phase='enumerate-windows'
    if (-not ('OfficialDesktop.WindowProbe' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'OfficialAppWindows.cs') }
    $before=@(Read-WindowSnapshot)
    $old=@{}; foreach ($w in $before) { $old[(Window-Key $w)]=$w.Foreground }
    $outputRecord.before=@(Public-Windows $before)

    $phase='activate'
    # Capturing stdout waits for the GUI-subsystem executable and its JSON receipt.
    $nativeOutput = & $native --open-app $App 2>$null | Out-String
    $outputRecord.launcherExitCode=$LASTEXITCODE
    if ($LASTEXITCODE -ne 0) { throw 'The native application launcher failed.' }
    $receipt=$nativeOutput | ConvertFrom-Json
    if ($receipt.status -ne 'activation-requested' -or $receipt.app -ne $App -or
        $receipt.aumid -ne $aumid -or $receipt.packageFullName -ne $registeredName -or
        [uint32]$receipt.processId -eq 0) { throw 'The native receipt did not match the registered application.' }
    $outputRecord.activationPid=[uint32]$receipt.processId
    $phase='observe-visible-window'
    $timer=[Diagnostics.Stopwatch]::StartNew()
    $previous=@{}
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $now=@(Read-WindowSnapshot)
        $current=@{}
        foreach ($w in $now) { $current[(Window-Key $w)]=$true }
        $stable=@($now | Where-Object { $previous.ContainsKey((Window-Key $_)) })
        $fresh=@($stable | Where-Object { -not $old.ContainsKey((Window-Key $_)) })
        $focused=@($stable | Where-Object { $_.Foreground -and $old.ContainsKey((Window-Key $_)) -and -not $old[(Window-Key $_)] })
        $relation = if ($fresh.Count) { 'new-window' } elseif ($focused.Count) { 'existing-window-became-foreground' }
            elseif ($stable.Count -and $timer.ElapsedMilliseconds -ge 1500) { 'already-visible' }
        if ($relation) {
            $outputRecord.status='visible-window'
            $outputRecord.observation=$relation
            $outputRecord.after=@(Public-Windows $stable)
            $outputRecord.observationMs=$timer.ElapsedMilliseconds
            $outputRecord | ConvertTo-Json -Depth 5 -Compress
            exit 0
        }
        $previous=$current
        Start-Sleep -Milliseconds 400
    }
    $outputRecord.status='window-timeout'
    $outputRecord.observationMs=$timer.ElapsedMilliseconds
    $outputRecord.after=@(Public-Windows $now)
    $outputRecord | ConvertTo-Json -Depth 5 -Compress
    exit 4
} catch {
    # Windows errors may contain user profile paths. Report the stage and exception type only.
    $outputRecord.status='error'
    $outputRecord.stage=$phase
    $outputRecord.errorType=$_.Exception.GetType().Name
    $outputRecord | ConvertTo-Json -Depth 5 -Compress
    exit 3
}
