# Offline contract tests for the actual Windows host coordinator. No Windows, WSL, user files or cloud account required.
[CmdletBinding()]
param([string]$HostScript = (Join-Path (Split-Path -Parent $PSScriptRoot) 'windows/Test-NativeWorkspace.ps1'))
$ErrorActionPreference = 'Stop'
$HostScript = (Resolve-Path -LiteralPath $HostScript).Path
. $HostScript

function Assert-Equal($Actual, $Expected, [string]$Because) {
    if ($Actual -cne $Expected) { throw "Failed $Because (expected '$Expected', observed '$Actual')." }
}
function Assert-True([bool]$Value, [string]$Because) {
    if (-not $Value) { throw "Failed $Because." }
}

# These are Windows C-runtime process arguments, not commands for cmd.exe to interpret.
$quoteCases = @(
    @{ In=''; Out='""' }, @{ In='plain'; Out='"plain"' }, @{ In='two words'; Out='"two words"' },
    @{ In='say "yes"'; Out='"say \"yes\""' }, @{ In='C:\two words\'; Out='"C:\two words\\"' },
    @{ In='A\"B'; Out='"A\\\"B"' }, @{ In='! & %'; Out='"! & %"' }
)
foreach ($case in $quoteCases) { Assert-Equal (Quote-ProcessArgument $case.In) $case.Out 'process argument quoting' }

# Exercise the real Read-Guest and Resolve-TestDistro before replacing these boundaries for the workflow.
$script:DistroName = 'Ubuntu QA & 24'
$script:MockRoot = 'C:\work path & name'
$script:LastWslArgs = $null
function Invoke-WorkspaceWsl($System, [string[]]$Arguments, [int]$Seconds) {
    $script:LastWslArgs = $Arguments
    if ($Arguments[0] -eq '--list') { return @{ Code=0; Text="$script:DistroName`r`nDebian`r`n" } }
    return @{ Code=0; Text='{"status":"pending"}' }
}
$pick = Resolve-TestDistro @{} $script:DistroName
Assert-Equal $pick $script:DistroName 'registered distro with punctuation'
$probeId = '0123456789abcdef0123456789abcdef'
$receipt = Read-Guest @{} $script:MockRoot $pick 'check' $probeId
$expectedArgs = @('--distribution',$script:DistroName,'--cd',$script:MockRoot,'--exec','/usr/bin/python3','./linux/verify_native_desktop.py','check',$probeId)
Assert-Equal $script:LastWslArgs.Length $expectedArgs.Length 'raw argv count'
for ($i=0; $i -lt $expectedArgs.Length; $i++) { Assert-Equal $script:LastWslArgs[$i] $expectedArgs[$i] "raw argv position $i" }
Assert-Equal $receipt.status 'pending' 'WSL receipt'

function Reset-Case([string]$Name) {
    $script:Mock = @{
        Name=$Name; Finds=0; Attaches=0; Prompts=0; PostChecks=0; PostFinds=0; Launches=0; Cleanups=0; Disposes=0; Stops=0; Sleeps=0
        Id=$null; Expected=$null; Actions=[Collections.Generic.List[string]]::new(); AppSamples=0; LauncherAppFlag=$null
    }
}
function Get-WindowsEnvironment {
    if ($script:Mock.Name -eq 'preflight') { Fail-WorkspaceTest 'fixture-preflight' }
    return @{ Build=22631; Wsl='fixture-wsl'; Cmd='fixture-cmd' }
}
function Resolve-TestDistro($System, [string]$Name) {
    Assert-Equal $Name $script:DistroName 'selected test distro'
    return $Name
}
function New-Window([string]$Hwnd='0xA1', [uint32]$Owner=41, [long]$Created=177, [bool]$Focused=$true) {
    return [pscustomobject]@{ Hwnd=$Hwnd; ProcessId=$Owner; ProcessStartTime=$Created; Foreground=$Focused
        Width=1100; Height=720; ProcessImage='msrdc.exe' }
}
function Find-TestWindow([string]$Id, [string]$Name) {
    $c = $script:Mock
    Assert-Equal $Id $c.Id 'window correlation'
    $c.Finds++
    if ($Name) { Assert-Equal $Name $script:DistroName 'window distro correlation' }
    if ($c.Name -eq 'late-window' -and $c.Finds -eq 2) { [Threading.Thread]::Sleep(1100) }
    if ($c.Finds -eq 1) {
        if ($c.Name -eq 'existed') { New-Window }
        return
    }
    if (($c.Name -eq 'wait-ready' -and $c.Attaches -lt 2) -or $c.Name -eq 'wait-window') { return }
    if ($c.Name -eq 'multiple-at-start') { New-Window; New-Window -Hwnd '0xB2'; return }
    if ($c.Finds -ge 3) {
        if ($c.Name -eq 'reuse-pid') { New-Window -Owner 42; return }
        if ($c.Name -eq 'reuse-start') { New-Window -Created 178; return }
        if ($c.Name -eq 'different-hwnd') { New-Window -Hwnd '0xB2'; return }
        if ($c.Name -eq 'duplicate-after' -and $c.Finds -eq 4) { New-Window; New-Window -Hwnd '0xB2'; return }
    }
    if ($c.Prompts -eq 0) {
        if ($c.Name -eq 'activate-before' -and $c.Finds -in @(2,3)) { New-Window -Focused $false; return }
        if ($c.Name -eq 'activate-after' -and $c.Finds -in @(2,4)) { New-Window -Focused $false; return }
    } else {
        $c.PostFinds++
        if ($c.Name -in @('focus-before','focus-prior','refocus') -and $c.PostFinds -eq 1) { New-Window -Focused $false; return }
        if ($c.Name -eq 'focus-after' -and $c.PostFinds -eq 2) { New-Window -Focused $false; return }
        if ($c.Name -eq 'missing-after' -and $c.PostFinds -eq 2) { return }
    }
    New-Window
}
function Find-AppTestWindow([string]$Id, [string]$Name) {
    $c = $script:Mock
    Assert-Equal $Id $c.Id 'app window correlation'
    Assert-Equal $Name $script:DistroName 'app window distro'
    $c.AppSamples++
    # Each app has two foreground samples, one real minimize sample, then two restored samples.
    Assert-True ($c.AppSamples -le 10) 'expected app host observation count'
    $minimized = $c.AppSamples -in @(3,8)
    $window = New-Window -Focused (-not $minimized)
    $window | Add-Member NoteProperty Minimized $minimized
    if ($minimized) { $window.Width=0; $window.Height=0 }
    return $window
}
function Start-TestDesktop($System, [string]$Root, [string]$Name, [string]$Id, [bool]$ObserveApps=$false) {
    $c = $script:Mock
    Assert-Equal $Name $script:DistroName 'launcher distro'
    Assert-Equal $Id $c.Id 'launcher correlation'
    $c.LauncherAppFlag=$ObserveApps
    $c.Launches++
    if ($c.Name -eq 'launch-error') { Fail-WorkspaceTest 'launcher-start-failed' }
    $proc = [pscustomobject]@{ HasExited=($c.Name -eq 'launcher-exited'); ExitCode=2 }
    $proc | Add-Member ScriptMethod Dispose { $script:Mock.Disposes++ }
    $proc | Add-Member ScriptMethod Kill { $script:Mock.Stops++; throw 'must not stop the operator desktop' }
    $proc | Add-Member ScriptMethod CloseMainWindow { $script:Mock.Stops++; throw 'must not close the operator desktop' }
    return $proc
}
function Stop-Process { $script:Mock.Stops++; throw 'must not terminate the operator desktop' }
function Start-Sleep([int]$Milliseconds) {
    $script:Mock.Sleeps++
    if ($script:Mock.Sleeps -gt 8) { throw 'unexpected polling loop' }
    if ($script:Mock.Name -in @('wait-attach','wait-window') -or ($script:Mock.Name -eq 'wait-folder' -and $script:Mock.PostChecks)) {
        [Threading.Thread]::Sleep(1100)
    }
}
function Read-Guest($System, [string]$Root, [string]$Name, [string]$Action, [string]$Id) {
    $c=$script:Mock
    Assert-Equal $Name $script:DistroName 'guest distro'
    Assert-True ($Id -cmatch '^[0-9a-f]{32}$') 'fresh 128-bit correlation token'
    $c.Actions.Add($Action)
    if ($Action -eq 'prepare') {
        $c.Id=$Id; $c.Expected='Verified-'+$Id.Substring(0,8)
        if ($c.Name -eq 'wrong-prepared') { return @{ status='prepared'; expected='wrong' } }
        return @{ status='prepared'; expected=$c.Expected }
    }
    Assert-Equal $Id $c.Id 'guest action correlation'
    switch ($Action) {
        'attach' {
            $c.Attaches++
            if ($c.Name -eq 'wait-attach' -or ($c.Name -eq 'wait-ready' -and $c.Attaches -eq 1)) { return @{ status='not_ready' } }
            $shell = if ($c.Name -eq 'bad-attach-pid') { 0 } elseif ($c.Name -eq 'opaque-pid') { 'not-a-pid' } else { 301 }
            $backend = if ($c.Name -eq 'wrong-backend') { 'xvfb' } else { 'nested' }
            $wslg = if ($c.Name -eq 'missing-wslg') { $false } elseif ($c.Name -eq 'untyped-wslg') { 'False' } else { $true }
            return @{ status='attached'; expected=$c.Expected; shellPid=$shell; filesPid=302; ubuntuVersion='24.04'; backend=$backend; wslgPresent=$wslg }
        }
        'prompt' {
            Assert-True (-not $c.Instructions.ToString().Contains($c.Expected)) 'folder name was private until the prompt receipt'
            $c.Prompts++
            if ($c.Name -eq 'prompt-error') { Fail-WorkspaceTest 'guest-prompt_failed' }
            $status = if ($c.Name -eq 'wrong-prompt-status') { 'pending' } else { 'prompted' }
            $expected = if ($c.Name -eq 'wrong-prompt-name') { 'wrong' } else { $c.Expected }
            $shell = if ($c.Name -eq 'wrong-prompt-shell') { 876 } else { 301 }
            $files = if ($c.Name -eq 'untyped-prompt-files') { '302' } else { 302 }
            return @{ status=$status; expected=$expected; shellPid=$shell; filesPid=$files }
        }
        'check' {
            if ($c.Name -eq 'check-exception') { throw 'PRIVATE_FIXTURE_DIAGNOSTIC' }
            if (-not $c.Prompts) {
                Assert-True (-not $c.Instructions.ToString().Contains($c.Expected)) 'no folder name before arming'
                $status = if ($c.Name -eq 'folder-before-prompt') { 'verified' } else { 'pending' }
                return @{ status=$status; expected=$c.Expected; shellPid=301; filesPid=302 }
            }
            $c.PostChecks++
            Assert-True ($c.Instructions.ToString().Contains($c.Expected)) 'confirmed prompt displayed before verification'
            if ($c.Name -eq 'late-folder') { [Threading.Thread]::Sleep(1100) }
            $pending = $c.Name -eq 'wait-folder' -or ($c.Name -in @('wait-ready','focus-prior') -and $c.PostChecks -eq 1) -or
                       ($c.Name -eq 'refocus' -and $c.PostChecks -le 2)
            $status = if ($pending) { 'pending' } else { 'verified' }
            $shell = if ($c.Name -eq 'wrong-shell') { 999 } elseif ($c.Name -eq 'opaque-pid') { 'not-a-pid' } else { 301 }
            $files = if ($c.Name -eq 'wrong-files') { 999 } elseif ($c.Name -eq 'untyped-check') { '302' } else { 302 }
            return @{ status=$status; expected=$c.Expected; shellPid=$shell; filesPid=$files; folder='PRIVATE_UNUSED_PATH' }
        }
        'cleanup' {
            $c.Cleanups++
            if ($c.Name -in @('cleanup-error','apps-cleanup-error')) { throw 'PRIVATE_CLEANUP' }
            $status=if ($c.Name -eq 'preserve') { 'preserved' } else { 'cleaned' }
            return @{ status=$status }
        }
        { $_ -in @('app-chatgpt','app-claude') } {
            $client = $Action.Substring(4)
            $app = @{ status='observed'; client=$client; desktopId='chatgpt.desktop'; shellPid=301
                appPid=701; appStartTicks=[long]5100007001; windowId='44'; executableSha256=('a' * 64)
                title='PRIVATE_DOCUMENT_TITLE' }
            if ($client -eq 'claude') {
                $app.desktopId='com.anthropic.Claude.desktop'; $app.appPid=702; $app.windowId='55'
                if ($c.Name -eq 'apps-claude-replaced' -and $c.AppSamples -eq 9) { $app.windowId='56' }
            }
            return $app
        }
    }
    throw 'Unexpected guest action'
}
$checked=0
function Check-Case([string]$Name, [string]$ErrorCode='', [int]$Launches=1, [int]$Disposes=1, [int]$Cleanups=1, [switch]$CheckApps) {
    Reset-Case $Name
    $startup = if ($Name -in @('wait-attach','wait-window','late-window')) { 1 } else { 15 }
    $interaction = if ($Name -in @('wait-folder','late-folder')) { 1 } else { 15 }
    $stderr=[Console]::Error
    $script:Mock.Instructions = [IO.StringWriter]::new()
    try {
        [Console]::SetError($script:Mock.Instructions)
        $result = @(Invoke-NativeWorkspaceTest $script:DistroName $interaction $startup -CheckApps:$CheckApps)
    } finally {
        [Console]::SetError($stderr)
        $script:Mock.Instructions.Dispose()
    }
    Assert-Equal $result.Count 1 "$Name output count"
    $r=$result[0]
    if ($ErrorCode) {
        Assert-Equal $r.status 'error' "$Name status"
        Assert-Equal $r.errorCode $ErrorCode "$Name error code"
    } else {
        Assert-Equal $r.status 'verified' "$Name status"
        Assert-Equal $r.phase 'complete' "$Name phase"
        Assert-Equal $r.window.processCreationFileTime 177 "$Name process creation"
        Assert-Equal $r.foregroundObserved $true "$Name foreground"
    }
    Assert-Equal $script:Mock.Launches $Launches "$Name launch count"
    if ($Launches) { Assert-Equal $script:Mock.LauncherAppFlag ([bool]$CheckApps) "$Name desktop receives opt-in flag" }
    if (-not $CheckApps) {
        Assert-Equal $script:Mock.AppSamples 0 "$Name default skips application windows"
        Assert-True (-not $r.Contains('apps')) "$Name default omits application receipts"
    }
    Assert-Equal $script:Mock.Disposes $Disposes "$Name dispose count"
    Assert-Equal $script:Mock.Cleanups $Cleanups "$Name cleanup count"
    Assert-Equal $script:Mock.Stops 0 "$Name no application terminated"
    if ($Cleanups) { Assert-Equal $script:Mock.Actions[$script:Mock.Actions.Count-1] 'cleanup' "$Name cleanup is last" }
    $json = $r | ConvertTo-Json -Compress -Depth 5
    Assert-True ($json -notmatch 'PRIVATE_|Cloud Linux Workspace') "$Name redaction"
    $script:checked++
    return $r
}

$normal=Check-Case 'normal'
Assert-Equal ($script:Mock.Actions -join ',') 'prepare,attach,check,prompt,check,cleanup' 'normal action order'
Assert-Equal $normal.cleanup 'cleaned' 'normal cleanup'
$apps=Check-Case 'apps' -CheckApps
Assert-Equal ($apps.apps.Keys -join ',') 'chatgpt,claude' 'both completed application observations'
Assert-Equal ($script:Mock.Actions -join ',') 'prepare,attach,check,prompt,check,app-chatgpt,app-chatgpt,app-claude,app-claude,cleanup' 'guest observes each client before and after minimization'
Assert-Equal $script:Mock.AppSamples 10 'host minimize and restore samples for both clients'
foreach ($client in 'chatgpt','claude') {
    $entry=$apps.apps[$client]
    Assert-True ($entry.hostForegroundBefore -and $entry.hostMinimized -and $entry.hostForegroundAfter) "$client confirmed host transitions"
}
Assert-Equal $apps.apps.chatgpt.desktopId 'chatgpt.desktop' 'ChatGPT desktop entry'
Assert-Equal $apps.apps.claude.desktopId 'com.anthropic.Claude.desktop' 'Claude desktop entry'
$partial=Check-Case 'apps-claude-replaced' 'app-window-changed' -CheckApps
Assert-Equal $partial.phase 'observe-apps' 'second app failure phase'
Assert-Equal ($partial.apps.Keys -join ',') 'chatgpt' 'only first completed app survives second app failure'
Assert-True $partial.apps.chatgpt.hostForegroundAfter 'first client finished restore before second client failure'
Assert-Equal $partial.cleanup 'cleaned' 'app failure still cleans the guest fixture'
$cleanup=Check-Case 'apps-cleanup-error' 'cleanup-failed' -CheckApps
Assert-Equal $cleanup.cleanup 'failed' 'cleanup failure after successful app observations'
Assert-Equal ($cleanup.apps.Keys -join ',') 'chatgpt,claude' 'cleanup failure retains both completed observations'
$null=Check-Case 'wait-ready'
Assert-Equal ($script:Mock.Actions -join ',') 'prepare,attach,attach,check,prompt,check,check,cleanup' 'pending polling order'
$preserved=Check-Case 'preserve'
Assert-Equal $preserved.cleanup 'preserved' 'extra user content preserved'
$null=Check-Case 'preflight' 'fixture-preflight' 0 0 0
$null=Check-Case 'wrong-prepared' 'wrong-folder-name' 0 0
$null=Check-Case 'existed' 'window-already-existed' 0 0
$null=Check-Case 'launch-error' 'launcher-start-failed' 1 0
$null=Check-Case 'launcher-exited' 'launcher-exited'
$null=Check-Case 'bad-attach-pid' 'invalid-session-identity'
$null=Check-Case 'opaque-pid' 'invalid-session-identity'
foreach ($bad in 'wrong-backend','missing-wslg','untyped-wslg') { $null=Check-Case $bad 'wslg-guest-not-confirmed' }
$null=Check-Case 'reuse-pid' 'window-changed'
$null=Check-Case 'reuse-start' 'window-changed'
$null=Check-Case 'different-hwnd' 'window-changed'
$null=Check-Case 'multiple-at-start' 'ambiguous-window'
$null=Check-Case 'duplicate-after' 'ambiguous-window'
foreach ($phase in 'before','after') {
    $null=Check-Case "activate-$phase"
    Assert-Equal ($script:Mock.Actions -join ',') 'prepare,attach,check,check,prompt,check,cleanup' "wait for both foreground samples: $phase"
}
$null=Check-Case 'refocus'
Assert-Equal $script:Mock.PostChecks 3 'foreground recovered while the folder was still absent'
$null=Check-Case 'folder-before-prompt' 'folder-before-prompt'
Assert-Equal $script:Mock.Prompts 0 'existing folder never prompts'
$null=Check-Case 'prompt-error' 'guest-prompt_failed'
foreach ($bad in 'wrong-prompt-status','wrong-prompt-name','wrong-prompt-shell','untyped-prompt-files') {
    $null=Check-Case $bad 'invalid-prompt-receipt'
    Assert-Equal $script:Mock.PostChecks 0 "$bad never starts verification"
}
foreach ($phase in 'prior','before','after') { $null=Check-Case "focus-$phase" 'foreground-not-observed' }
$null=Check-Case 'missing-after' 'foreground-not-observed'
$null=Check-Case 'wrong-shell' 'session-changed'
$null=Check-Case 'wrong-files' 'session-changed'
$null=Check-Case 'untyped-check' 'session-changed'
$null=Check-Case 'check-exception' 'probe-failed'
$failed=Check-Case 'cleanup-error' 'cleanup-failed'
Assert-Equal $failed.cleanup 'failed' 'cleanup failure receipt'
$null=Check-Case 'wait-attach' 'gnome-session-timeout'
$null=Check-Case 'wait-window' 'wslg-window-timeout'
$null=Check-Case 'wait-folder' 'folder-timeout'
$null=Check-Case 'late-folder' 'folder-timeout'
$null=Check-Case 'late-window' 'wslg-window-timeout'

# Check the real command entry point's exit code and stdout in a separate PowerShell process.
# Add a failing preflight mock before the unmodified entry point, so this stays safe on an interactive Windows PC.
$tokens=$null; $parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($HostScript,[ref]$tokens,[ref]$parseErrors)
Assert-Equal $parseErrors.Count 0 'production script parsing'
$entry=$ast.EndBlock.Statements[-1]
Assert-True ($entry -is [Management.Automation.Language.IfStatementAst]) 'CLI entry remains last'
$text=[IO.File]::ReadAllText($HostScript)
$offset=$entry.Extent.StartOffset
$temp=Join-Path ([IO.Path]::GetTempPath()) ('Workspace host '+[Guid]::NewGuid().ToString('N'))
$null=[IO.Directory]::CreateDirectory($temp)
try {
    $copy=Join-Path $temp 'Fixture Probe.ps1'
    $mockCommand="function Get-WindowsEnvironment { Fail-WorkspaceTest 'fixture-preflight' }"
    [IO.File]::WriteAllText($copy, $text.Insert($offset, $mockCommand+[Environment]::NewLine), [Text.UTF8Encoding]::new($true))
    $engine=(Get-Process -Id $PID).Path
    $stdout=@(& $engine -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $copy)
    Assert-Equal $LASTEXITCODE 1 'CLI failure exit code'
    Assert-Equal $stdout.Count 1 'exactly one JSON stdout line'
    $report=$stdout[0] | ConvertFrom-Json
    Assert-Equal $report.status 'error' 'CLI error status'
    Assert-Equal $report.errorCode 'fixture-preflight' 'CLI failure reason'
    Assert-Equal $report.cleanup 'not-needed' 'CLI preflight cleanup'

    # Exercise the public switch binding without launching a desktop in this separate process.
    $capture = @'
function Invoke-NativeWorkspaceTest([string]$Name, [int]$InteractionSeconds, [switch]$CheckApps) {
    return @{ status='verified'; checkApps=[bool]$CheckApps }
}
'@
    [IO.File]::WriteAllText($copy, $text.Insert($offset,$capture+[Environment]::NewLine), [Text.UTF8Encoding]::new($true))
    foreach ($optIn in @($false,$true)) {
        $flags = @()
        if ($optIn) { $flags += '-VerifyApps' }
        $stdout=@(& $engine -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $copy @flags)
        Assert-Equal $LASTEXITCODE 0 'CLI switch forwarding exit code'
        Assert-Equal $stdout.Count 1 'CLI switch forwarding emits one JSON line'
        Assert-Equal (($stdout[0] | ConvertFrom-Json).checkApps) $optIn 'CLI opt-in forwarded to coordinator'
    }
} finally { [IO.Directory]::Delete($temp,$true) }
"Passed $checked host workflow scenarios, WSL argv/quoting checks, and CLI failure and opt-in checks."
