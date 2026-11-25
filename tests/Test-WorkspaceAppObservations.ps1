# Deterministic test of the production Windows coordinator using synthetic Windows and guest observations.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$windowsDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) 'windows'
. (Join-Path $windowsDirectory 'Test-NativeWorkspace.ps1')
. (Join-Path $windowsDirectory 'VerifyWorkspaceApps.ps1')

function Assert-Check([bool]$Value, [string]$Label) {
    if (-not $Value) { throw "Failed $Label." }
}

$script:IdUnderTest = '00112233445566778899aabbccddeeff'
$script:IdentitySample = [pscustomobject]@{
    Hwnd='0x98'; ProcessId=902; ProcessStartTime=[long]1339920010; Minimized=$false; Foreground=$true
}
$script:HappyFrames = @('F','F','M','F','F','F','F','M','F','F')

function Reset-Scenario([string[]]$Frames, [string[]]$Actions, [int]$MutateAt=0, [hashtable]$Changes=@{}) {
    $script:Case = @{
        Frames=$Frames; Actions=$Actions; Finds=0; Reads=0; MutateAt=$MutateAt; Changes=$Changes
        SlowRead=0; NoMoreWindows=$false; IdleClock=$false
    }
}

function Find-AppTestWindow([string]$Id, [string]$Name) {
    Assert-Check ($Id -ceq $script:IdUnderTest -and $Name -ceq 'Ubuntu-24.04') 'bound Windows query'
    $i = $script:Case.Finds
    $script:Case.Finds++
    if ($i -ge $script:Case.Frames.Count) {
        if ($script:Case.NoMoreWindows) { return }
        throw "Unexpected extra Windows sample $i."
    }
    $code = $script:Case.Frames[$i]
    if ($code -eq 'N') { return }
    $window = $script:IdentitySample.PSObject.Copy()
    switch ($code) {
        'M' { $window.Minimized=$true; $window.Foreground=$false }
        'B' { $window.Foreground=$false }
        'H' { $window.Hwnd='0x99' }
        'P' { $window.ProcessId=903 }
        'T' { $window.ProcessStartTime++ }
        'A' { $window }
        'bad-minimized' { $window.Minimized='False' }
        'bad-foreground' { $window.Foreground=1 }
        'F' { }
        default { throw "Unknown host frame $code." }
    }
    $window
}

function Read-Guest($System, [string]$Root, [string]$Name, [string]$Action, [string]$Id) {
    Assert-Check ($Root -ceq 'C:\fixture' -and $Name -ceq 'Ubuntu-24.04' -and $Id -ceq $script:IdUnderTest) 'guest binding'
    $i = $script:Case.Reads
    $script:Case.Reads++
    if ($i -ge $script:Case.Actions.Count) { throw "Unexpected extra guest read $i." }
    $spec = $script:Case.Actions[$i]
    $client = $spec.TrimEnd('?')
    Assert-Check ($Action -ceq ('app-' + $client)) 'only intended guest action'
    if ($script:Case.SlowRead -eq $script:Case.Reads) { [Threading.Thread]::Sleep(1100) }
    if ($spec.EndsWith('?')) { return @{ status='pending' } }
    $chat = $client -eq 'chatgpt'
    $r = @{
        status='observed'; client=$client; shellPid=142; appPid=164; appStartTicks=[long]5000000001
        windowId='4294967295'; desktopId='chatgpt.desktop'; executableSha256=('a' * 64)
        title='Private document title'; unrelated='/home/private-user/files'
    }
    if (-not $chat) {
        $r.appPid=165; $r.windowId='88'; $r.desktopId='com.anthropic.Claude.desktop'
    }
    if ($script:Case.MutateAt -eq $script:Case.Reads) {
        foreach ($key in $script:Case.Changes.Keys) { $r[$key]=$script:Case.Changes[$key] }
    }
    return ($r | ConvertTo-Json -Depth 3 -Compress | ConvertFrom-Json)
}

function Start-Sleep([int]$Milliseconds) {
    if ($script:Case.IdleClock -and $script:Case.Finds -ge $script:Case.Frames.Count) {
        [Threading.Thread]::Sleep(1100)
    }
}

function Invoke-Scenario([int]$Seconds=10, [System.Collections.IDictionary]$Evidence=([ordered]@{})) {
    Invoke-WorkspaceAppObservations @{} 'C:\fixture' 'Ubuntu-24.04' $script:IdUnderTest $script:IdentitySample @{shellPid=142} $Seconds $Evidence
}

function Expect-Failure([string]$Code, [int]$Seconds=10, [System.Collections.IDictionary]$Evidence=([ordered]@{})) {
    $got = ''
    try { $null = Invoke-Scenario $Seconds $Evidence } catch {
        $got = $_.Exception.Data['WorkspaceCode']
        if (-not $got) { throw }
    }
    Assert-Check ($got -ceq $Code) "error $Code (received $got)"
}

$script:DefaultActions = @('chatgpt','chatgpt','claude','claude')
$oldError = [Console]::Error
$messages = [IO.StringWriter]::new()
try {
    [Console]::SetError($messages)
    Reset-Scenario $script:HappyFrames $script:DefaultActions
    $results = @(Invoke-Scenario)
    Assert-Check ($results.Count -eq 1 -and $results[0].Count -eq 2) 'single stdout object'
    $apps = $results[0]
    foreach ($client in @('chatgpt','claude')) {
        $app = $apps[$client]
        Assert-Check ($app.hostForegroundBefore -and $app.hostMinimized -and $app.hostForegroundAfter) 'host transitions'
        Assert-Check ($app.client -ceq $client -and $app.shellPid -eq 142 -and $app.appStartTicks -eq 5000000001) 'guest identity'
    }
    Assert-Check ($apps.chatgpt.windowId -ceq '4294967295' -and $apps.claude.windowId -ceq '88') 'stable window identifiers'
    $json = $apps | ConvertTo-Json -Depth 4
    Assert-Check ($json -notmatch 'Private document|private-user|unrelated|title') 'allowlisted output'
    Assert-Check ($messages.ToString().Contains('ChatGPT') -and $messages.ToString().Contains('Claude')) 'operator guidance on stderr'
    Assert-Check ($script:Case.Finds -eq 10 -and $script:Case.Reads -eq 4) 'guest checks bracketed by host observations'

    # Pending, missing and background samples may occur during normal operator navigation.
    $retry = @('N','B','F','F','F','B','F','F','N','B','M','F','F','F','F','F','F','M','F','F')
    Reset-Scenario $retry @('chatgpt?','chatgpt','chatgpt','chatgpt?','chatgpt','claude','claude')
    $null = Invoke-Scenario
    Assert-Check ($script:Case.Reads -eq 7 -and $script:Case.Finds -eq $retry.Count) 'pending and focus retry'

    foreach ($changed in @('H','P','T')) {
        foreach ($at in @(0,1,2,3,4)) {
            $frames = $script:HappyFrames.Clone(); $frames[$at] = $changed
            Reset-Scenario $frames $script:DefaultActions
            Expect-Failure 'window-changed'
        }
    }
    Reset-Scenario @('F','A') $script:DefaultActions
    Expect-Failure 'ambiguous-window'
    foreach ($bad in @('bad-minimized','bad-foreground')) {
        Reset-Scenario @($bad) $script:DefaultActions
        Expect-Failure 'invalid-app-host-window'
    }
    foreach ($fields in @(@{client='claude'}, @{desktopId='other.desktop'}, @{appPid='164'}, @{appPid=142},
            @{appStartTicks='5000000001'}, @{appStartTicks=0}, @{windowId=123}, @{windowId='1.0'},
            @{windowId='0'}, @{windowId='01'}, @{windowId='4294967296'}, @{executableSha256=('A'*64)}, @{status=@('pending')})) {
        Reset-Scenario $script:HappyFrames $script:DefaultActions 1 $fields
        Expect-Failure 'invalid-app-receipt'
    }
    foreach ($change in @(@{windowId='1'}, @{appStartTicks=[long]5000000002}, @{appPid=200}, @{executableSha256=('b'*64)})) {
        Reset-Scenario $script:HappyFrames $script:DefaultActions 2 $change
        Expect-Failure 'app-window-changed'
    }
    Reset-Scenario $script:HappyFrames $script:DefaultActions 4 @{shellPid=143}
    $partial = [ordered]@{}
    Expect-Failure 'session-changed' 10 $partial
    Assert-Check ($partial.Count -eq 1 -and $partial.chatgpt.hostForegroundAfter -and -not $partial.Contains('claude')) 'only fully observed app stays in partial report'
    # Hidden/missing or merely unfocused never counts as Windows IsIconic.
    Reset-Scenario @('F','F','B','N') $script:DefaultActions
    $script:Case.NoMoreWindows=$true; $script:Case.IdleClock=$true
    Expect-Failure 'app-chatgpt-minimize-timeout' 1
    Reset-Scenario $script:HappyFrames $script:DefaultActions
    $script:Case.SlowRead=2
    Expect-Failure 'app-chatgpt-restore-timeout' 1
} finally {
    [Console]::SetError($oldError)
    $messages.Dispose()
}
Write-Output 'Workspace app observation flow passed.'
