# Dot-sourced by Test-NativeWorkspace.ps1 in the interactive Windows session.
function Get-WorkspaceAppHostWindow([string]$Id, [string]$Name, [string]$Identity) {
    $windows = @(Find-AppTestWindow $Id $Name)
    if ($windows.Count -gt 1) { Fail-WorkspaceTest 'ambiguous-window' }
    if ($windows.Count -eq 0) { return }
    $sample = $windows[0]
    if ((Window-Identity $sample) -cne $Identity) { Fail-WorkspaceTest 'window-changed' }
    if ($sample.Minimized -isnot [bool] -or $sample.Foreground -isnot [bool]) {
        Fail-WorkspaceTest 'invalid-app-host-window'
    }
    return $sample
}

function Get-WorkspaceAppObservation($Receipt, [string]$Client, [string]$DesktopId, $ShellPid) {
    if ($Receipt.status -isnot [string]) { Fail-WorkspaceTest 'invalid-app-receipt' }
    if ($Receipt.status -ceq 'pending') { return }
    if ($Receipt.status -cne 'observed' -or $Receipt.client -isnot [string] -or $Receipt.client -cne $Client -or
        $Receipt.desktopId -isnot [string] -or $Receipt.desktopId -cne $DesktopId -or -not (Test-GuestPid $Receipt.shellPid) -or
        -not (Test-GuestPid $Receipt.appPid) -or $Receipt.appPid -eq $Receipt.shellPid -or
        ($Receipt.appStartTicks -isnot [long] -and $Receipt.appStartTicks -isnot [int]) -or
        $Receipt.appStartTicks -le 0 -or $Receipt.windowId -isnot [string] -or
        $Receipt.windowId -cnotmatch '\A[1-9][0-9]{0,9}\z' -or $Receipt.executableSha256 -isnot [string] -or
        $Receipt.executableSha256 -cnotmatch '\A[0-9a-f]{64}\z') {
        Fail-WorkspaceTest 'invalid-app-receipt'
    }
    if ($Receipt.shellPid -ne $ShellPid) { Fail-WorkspaceTest 'session-changed' }
    $numericId = [uint32]0
    if (-not [uint32]::TryParse($Receipt.windowId, [ref]$numericId)) { Fail-WorkspaceTest 'invalid-app-receipt' }
    return [ordered]@{
        client=$Client; desktopId=$DesktopId; shellPid=$ShellPid; appPid=$Receipt.appPid
        appStartTicks=$Receipt.appStartTicks; windowId=$Receipt.windowId; executableSha256=$Receipt.executableSha256
    }
}

function Invoke-WorkspaceAppObservations($System, [string]$Root, [string]$Name, [string]$Id,
                                         $Window, $Guest, [int]$InteractionSeconds,
                                         [System.Collections.IDictionary]$Apps = ([ordered]@{})) {
    if (-not (Test-GuestPid $Guest.shellPid)) { Fail-WorkspaceTest 'invalid-session-identity' }
    $identity = Window-Identity $Window
    $clients = [ordered]@{ chatgpt=@('ChatGPT','chatgpt.desktop'); claude=@('Claude','com.anthropic.Claude.desktop') }
    foreach ($client in $clients.Keys) {
        $label, $desktopId = $clients[$client]
        $first = $null
        foreach ($step in @('focus','minimize','restore')) {
            switch ($step) {
                'focus' { [Console]::Error.WriteLine("请在这个 Ubuntu GNOME 桌面内打开 $label，点进它的窗口并保持前台。无需登录或发送消息。") }
                'minimize' { [Console]::Error.WriteLine("已看到 $label。请从 Windows 最小化整个 Ubuntu 桌面，保持几秒钟，以便记录最小化状态。") }
                'restore' { [Console]::Error.WriteLine("已看到桌面最小化。请从 Windows 任务栏还原同一桌面，点回原来的 $label 窗口并保持前台。") }
            }
            $done = $false
            $timer = [Diagnostics.Stopwatch]::StartNew()
            while ($timer.Elapsed.TotalSeconds -lt $InteractionSeconds) {
                $before = Get-WorkspaceAppHostWindow $Id $Name $identity
                if ($timer.Elapsed.TotalSeconds -ge $InteractionSeconds) { break }
                if ($step -eq 'minimize') {
                    if ($before -and $before.Minimized) { $done = $true; break }
                } elseif ($before -and $before.Foreground -and -not $before.Minimized) {
                    $receipt = Read-Guest $System $Root $Name ('app-' + $client) $Id
                    $after = Get-WorkspaceAppHostWindow $Id $Name $identity
                    if ($timer.Elapsed.TotalSeconds -ge $InteractionSeconds) { break }
                    $observed = Get-WorkspaceAppObservation $receipt $client $desktopId $Guest.shellPid
                    if ($observed -and $after -and $after.Foreground -and -not $after.Minimized) {
                        if ($step -eq 'focus') {
                            $first = $observed
                        } elseif ($observed.appPid -ne $first.appPid -or $observed.appStartTicks -ne $first.appStartTicks -or
                                  $observed.windowId -cne $first.windowId -or $observed.executableSha256 -cne $first.executableSha256) {
                            Fail-WorkspaceTest 'app-window-changed'
                        }
                        $done = $true
                        break
                    }
                }
                Start-Sleep -Milliseconds 350
            }
            if (-not $done) { Fail-WorkspaceTest ("app-$client-$step-timeout") }
        }
        $first.hostForegroundBefore = $true
        $first.hostMinimized = $true
        $first.hostForegroundAfter = $true
        $apps[$client] = $first
    }
    return $apps
}
