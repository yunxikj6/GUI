# Run in an interactive Windows 11 session. Instructions go to stderr and the JSON report goes to stdout.
[CmdletBinding()]
param([string]$Distro, [ValidateRange(10,900)][int]$TimeoutSeconds = 180, [switch]$VerifyApps)
$ErrorActionPreference = 'Stop'

function Fail-WorkspaceTest([string]$Code) {
    $exception = [InvalidOperationException]::new($Code)
    $exception.Data['WorkspaceCode'] = $Code
    throw $exception
}

function Get-WindowsEnvironment {
    if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or -not [Environment]::UserInteractive -or
        [Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) { Fail-WorkspaceTest 'interactive-windows-required' }
    $build = [int](Get-ItemPropertyValue 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' 'CurrentBuildNumber')
    $product = Get-ItemPropertyValue 'HKLM:\SYSTEM\CurrentControlSet\Control\ProductOptions' 'ProductType'
    if ($build -lt 22000 -or $product -ne 'WinNT') { Fail-WorkspaceTest 'windows-11-required' }
    $dir = [Environment]::SystemDirectory
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        $dir = Join-Path ([Environment]::GetFolderPath('Windows')) 'Sysnative'
    }
    $wsl = Join-Path $dir 'wsl.exe'
    if (-not (Test-Path -LiteralPath $wsl -PathType Leaf)) { Fail-WorkspaceTest 'wsl-missing' }
    if (-not ('CloudWorkspace.DesktopWindow' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'WorkspaceWindowProbe.cs') }
    [pscustomobject]@{ Build=$build; Wsl=$wsl; Cmd=(Join-Path $dir 'cmd.exe') }
}

function New-ChildInfo([string]$Executable) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Executable
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.EnvironmentVariables.Remove('INSTABOX_API_KEY')
    $info.EnvironmentVariables.Remove('CLOUDWORKSPACE_VERIFY_ID')
    $info.EnvironmentVariables.Remove('CLOUDWORKSPACE_VERIFY_APPS')
    $entries = @($info.EnvironmentVariables['WSLENV'] -split ':' | Where-Object {
        $_ -and ($_ -split '/',2)[0] -notin @('INSTABOX_API_KEY','CLOUDWORKSPACE_VERIFY_ID','CLOUDWORKSPACE_VERIFY_APPS')
    })
    $info.EnvironmentVariables['WSLENV'] = $entries -join ':'
    return $info
}

function Quote-ProcessArgument([string]$Value) {
    # ProcessStartInfo.ArgumentList is unavailable in Windows PowerShell 5.1.
    '"' + (($Value -replace '(\\*)"','$1$1\"') -replace '(\\+)$','$1$1') + '"'
}

function Invoke-WorkspaceWsl($System, [string[]]$Arguments, [int]$Seconds) {
    $info = New-ChildInfo $System.Wsl
    $info.Arguments = ($Arguments | ForEach-Object { Quote-ProcessArgument $_ }) -join ' '
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.EnvironmentVariables['WSL_UTF8'] = '1'
    $proc = [Diagnostics.Process]::new()
    $proc.StartInfo = $info
    $bytes = [IO.MemoryStream]::new()
    try {
        if (-not $proc.Start()) { Fail-WorkspaceTest 'wsl-start-failed' }
        $outputTask = $proc.StandardOutput.BaseStream.CopyToAsync($bytes)
        $errorTask = $proc.StandardError.BaseStream.CopyToAsync([IO.Stream]::Null)
        if (-not $proc.WaitForExit($Seconds * 1000)) {
            try { $proc.Kill() } catch {}
            Fail-WorkspaceTest 'wsl-timeout'
        }
        if (-not $outputTask.Wait(2000) -or -not $errorTask.Wait(2000) -or $bytes.Length -gt 131072) {
            Fail-WorkspaceTest 'invalid-wsl-output'
        }
        $raw = $bytes.ToArray()
        # Old WSL versions emit BOM-less UTF-16LE even when WSL_UTF8 is set.
        $encoding = if ($raw -contains 0 -or ($raw.Length -gt 1 -and $raw[0] -eq 255 -and $raw[1] -eq 254)) {
            [Text.UnicodeEncoding]::new($false,$false,$true)
        } else { [Text.UTF8Encoding]::new($false,$true) }
        return [pscustomobject]@{ Code=$proc.ExitCode; Text=$encoding.GetString($raw).TrimStart([char]0xFEFF) }
    } finally { $bytes.Dispose(); $proc.Dispose() }
}

function Resolve-TestDistro($System, [string]$Name) {
    $list = Invoke-WorkspaceWsl $System @('--list','--quiet') 25
    if ($list.Code -ne 0) { Fail-WorkspaceTest 'wsl-list-failed' }
    $all = @($list.Text -split '\r?\n' | Where-Object { $_ })
    $matchesName = if ($Name) { @($all | Where-Object { $_ -ceq $Name }) }
        else { @($all | Where-Object { $_ -imatch '^Ubuntu(?:-|$)' }) }
    if (@($matchesName).Count -ne 1) { Fail-WorkspaceTest 'select-installed-distro' }
    $selected = [string]@($matchesName)[0]
    # Start.cmd quotes this argument when passing it to the production Windows launcher.
    if ($selected.StartsWith('-') -or $selected -match '["\x00-\x1f]') { Fail-WorkspaceTest 'invalid-distro-argument' }
    return $selected
}

function Start-TestDesktop($System, [string]$Root, [string]$Name, [string]$Id, [bool]$ObserveApps=$false) {
    $info = New-ChildInfo $System.Cmd
    $info.EnvironmentVariables['WORKSPACE_TEST_ENTRY'] = Join-Path $Root 'Start.cmd'
    $info.EnvironmentVariables['WORKSPACE_TEST_DISTRO'] = $Name
    $info.EnvironmentVariables['CLOUDWORKSPACE_VERIFY_ID'] = $Id
    $prior = $info.EnvironmentVariables['WSLENV']
    $info.EnvironmentVariables['WSLENV'] = (@($prior,'CLOUDWORKSPACE_VERIFY_ID/u') | Where-Object { $_ }) -join ':'
    if ($ObserveApps) {
        $info.EnvironmentVariables['CLOUDWORKSPACE_VERIFY_APPS'] = '1'
        $info.EnvironmentVariables['WSLENV'] += ':CLOUDWORKSPACE_VERIFY_APPS/u'
    }
    $info.Arguments = '/d /v:off /s /c ""%WORKSPACE_TEST_ENTRY%" --distro "%WORKSPACE_TEST_DISTRO%""'
    $proc = [Diagnostics.Process]::Start($info)
    if ($null -eq $proc) { Fail-WorkspaceTest 'launcher-start-failed' }
    return $proc
}

function Read-Guest($System, [string]$Root, [string]$Name, [string]$Action, [string]$Id) {
    $argsForWsl = @('--distribution',$Name,'--cd',$Root,'--exec','/usr/bin/python3',
                    './linux/verify_native_desktop.py',$Action,$Id)
    $budget = if ($Action -eq 'attach') { 70 } else { 20 }
    $call = Invoke-WorkspaceWsl $System $argsForWsl $budget
    try { $receipt = $call.Text | ConvertFrom-Json } catch { Fail-WorkspaceTest 'invalid-guest-receipt' }
    if (-not $receipt.status) { Fail-WorkspaceTest 'missing-guest-status' }
    if ($call.Code -ne 0 -or $receipt.status -eq 'error') {
        if ($receipt.code -match '^[a-z][a-z0-9_]{0,40}$') { Fail-WorkspaceTest ('guest-' + $receipt.code) }
        Fail-WorkspaceTest 'guest-command-failed'
    }
    return $receipt
}

function Find-TestWindow([string]$Id, [string]$Name) { [CloudWorkspace.DesktopWindow]::Find($Id,$Name) }
function Find-AppTestWindow([string]$Id, [string]$Name) { [CloudWorkspace.DesktopWindow]::FindIncludingMinimized($Id,$Name) }
function Test-GuestPid($Value) {
    return ($Value -is [int] -or $Value -is [long]) -and $Value -gt 0 -and $Value -le [int]::MaxValue
}
function Window-Identity($Window) { "$($Window.Hwnd)/$($Window.ProcessId)/$($Window.ProcessStartTime)" }

function Invoke-NativeWorkspaceTest([string]$Name, [int]$InteractionSeconds, [int]$StartupSeconds=150, [switch]$CheckApps) {
    $report = [ordered]@{ schema=1; status='error'; phase='windows-environment'; cleanup='not-needed' }
    $id = [Guid]::NewGuid().ToString('N')
    $expected = 'Verified-' + $id.Substring(0,8)
    $root = Split-Path -Parent $PSScriptRoot
    $ownsFixture = $false
    $launcher = $null
    try {
        $system = Get-WindowsEnvironment
        $report.windowsBuild = $system.Build
        $report.phase = 'select-distro'
        $name = Resolve-TestDistro $system $Name
        $report.phase = 'prepare'
        $prepared = Read-Guest $system $root $name 'prepare' $id
        if ($prepared.status -ne 'prepared') { Fail-WorkspaceTest 'unexpected-prepare-receipt' }
        $ownsFixture = $true
        if ($prepared.expected -cne $expected) { Fail-WorkspaceTest 'wrong-folder-name' }
        [Console]::Error.WriteLine("正在启动 Ubuntu 桌面。等文件管理器打开验证目录后，请按提示新建文件夹。")
        if ($CheckApps) {
            [Console]::Error.WriteLine("创建文件夹后，请按提示依次打开 Ubuntu 里的 ChatGPT 和 Claude。每款都要从 Windows 任务栏最小化整个工作区，稍候再还原并点回原应用。请保留此控制台以查看当前步骤。")
        }
        if (@(Find-TestWindow $id $name).Count) { Fail-WorkspaceTest 'window-already-existed' }
        $report.phase = 'start-workspace'
        $launcher = Start-TestDesktop $system $root $name $id ([bool]$CheckApps)
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $attached = $null
        $window = $null
        while ($timer.Elapsed.TotalSeconds -lt $StartupSeconds) {
            if ($launcher.HasExited -and $launcher.ExitCode -ne 0) { Fail-WorkspaceTest 'launcher-exited' }
            if (-not $attached) {
                $guest = Read-Guest $system $root $name 'attach' $id
                if ($guest.status -eq 'attached') {
                    if ($guest.expected -cne $expected -or -not (Test-GuestPid $guest.shellPid) -or -not (Test-GuestPid $guest.filesPid)) {
                        Fail-WorkspaceTest 'invalid-session-identity'
                    }
                    if ($guest.backend -cne 'nested' -or $guest.wslgPresent -isnot [bool] -or -not $guest.wslgPresent -or
                        $guest.ubuntuVersion -notmatch '^\d+(?:\.\d+)*$') { Fail-WorkspaceTest 'wslg-guest-not-confirmed' }
                    $attached = $guest
                    $report.guest = [ordered]@{ shellPid=$guest.shellPid; filesPid=$guest.filesPid; ubuntuVersion=$guest.ubuntuVersion; backend=$guest.backend; wslgPresent=$guest.wslgPresent }
                } elseif ($guest.status -ne 'not_ready') { Fail-WorkspaceTest 'unexpected-attach-receipt' }
            }
            $windows = @(Find-TestWindow $id $name)
            if ($windows.Count -gt 1) { Fail-WorkspaceTest 'ambiguous-window' }
            if ($timer.Elapsed.TotalSeconds -ge $StartupSeconds) { break }
            if ($attached -and $windows.Count -eq 1) { $window=$windows[0]; break }
            Start-Sleep -Milliseconds 400
        }
        if (-not $attached) { Fail-WorkspaceTest 'gnome-session-timeout' }
        if (-not $window) { Fail-WorkspaceTest 'wslg-window-timeout' }
        $identity = Window-Identity $window
        $report.window = [ordered]@{ hwnd=$window.Hwnd; processId=$window.ProcessId; image=$window.ProcessImage
            processCreationFileTime=$window.ProcessStartTime; clientWidth=$window.Width; clientHeight=$window.Height }
        $report.phase = 'observe-folder'
        [Console]::Error.WriteLine("请点进刚打开的 Ubuntu 文件管理器，等桌面通知给出文件夹名。")
        $timer.Restart()
        $prompted = $false
        $previouslyFocused = $false
        while ($timer.Elapsed.TotalSeconds -lt $InteractionSeconds) {
            $before = @(Find-TestWindow $id $name)
            $disk = Read-Guest $system $root $name 'check' $id
            $after = @(Find-TestWindow $id $name)
            if ($before.Count -gt 1 -or $after.Count -gt 1) { Fail-WorkspaceTest 'ambiguous-window' }
            if ($timer.Elapsed.TotalSeconds -ge $InteractionSeconds) { break }
            foreach ($sample in @($before + $after)) {
                if ((Window-Identity $sample) -cne $identity) { Fail-WorkspaceTest 'window-changed' }
            }
            $focused = $before.Count -eq 1 -and $after.Count -eq 1 -and $before[0].Foreground -and $after[0].Foreground
            if ($disk.expected -cne $expected) { Fail-WorkspaceTest 'wrong-folder-name' }
            if ($disk.status -eq 'verified') {
                if (-not $prompted) { Fail-WorkspaceTest 'folder-before-prompt' }
                if (-not (Test-GuestPid $disk.shellPid) -or -not (Test-GuestPid $disk.filesPid) -or
                    $disk.shellPid -ne $attached.shellPid -or $disk.filesPid -ne $attached.filesPid) { Fail-WorkspaceTest 'session-changed' }
                if (-not $focused -or -not $previouslyFocused) { Fail-WorkspaceTest 'foreground-not-observed' }
                $report.status = 'verified'
                $report.phase = 'complete'
                $report.foregroundObserved = $true
                $report.folderCreation = 'operator-in-files'
                break
            }
            if ($disk.status -ne 'pending') { Fail-WorkspaceTest 'unexpected-check-receipt' }
            if ($focused -and -not $prompted) {
                $notice = Read-Guest $system $root $name 'prompt' $id
                if ($notice.status -cne 'prompted' -or $notice.expected -cne $expected -or
                    -not (Test-GuestPid $notice.shellPid) -or -not (Test-GuestPid $notice.filesPid) -or
                    $notice.shellPid -ne $attached.shellPid -or $notice.filesPid -ne $attached.filesPid) {
                    Fail-WorkspaceTest 'invalid-prompt-receipt'
                }
                [Console]::Error.WriteLine("在 Ubuntu 文件管理器中按 Ctrl+Shift+N，创建文件夹 $expected，然后让此窗口保持在前台，直至验证结束。")
                $prompted = $true
            }
            $previouslyFocused = $focused
            Start-Sleep -Milliseconds 350
        }
        if ($report.status -ne 'verified') { Fail-WorkspaceTest 'folder-timeout' }
        if ($CheckApps) {
            $report.status = 'error'
            $report.phase = 'observe-apps'
            . (Join-Path $PSScriptRoot 'VerifyWorkspaceApps.ps1')
            $report.apps = [ordered]@{}
            $null = Invoke-WorkspaceAppObservations $system $root $name $id $window $attached $InteractionSeconds $report.apps
            $report.status = 'verified'
            $report.phase = 'complete'
        }
    } catch {
        $code = $_.Exception.Data['WorkspaceCode']
        $report.errorCode = if ($code) { $code } else { 'probe-failed' }
    } finally {
        if ($ownsFixture) {
            try {
                $done = Read-Guest $system $root $name 'cleanup' $id
                if ($done.status -notin @('cleaned','preserved')) { throw 'cleanup' }
                $report.cleanup = $done.status
            } catch {
                $report.cleanup = 'failed'
                if ($report.status -eq 'verified') { $report.status='error'; $report.phase='cleanup'; $report.errorCode='cleanup-failed' }
            }
        }
        if ($launcher) { $launcher.Dispose() }
    }
    return $report
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-NativeWorkspaceTest $Distro $TimeoutSeconds -CheckApps:$VerifyApps
    $result | ConvertTo-Json -Depth 4 -Compress
    if ($result.status -ne 'verified') { exit 1 }
}
