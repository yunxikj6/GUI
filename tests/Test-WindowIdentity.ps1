$ErrorActionPreference='Stop'
Add-Type -Path (Join-Path (Split-Path $PSScriptRoot) 'windows/OfficialAppWindows.cs')
$family='OpenAI.Codex_2p2nqsd0c76g0'
$full='ExampleRegisteredPackage_2_x64'
$id="$family!App"
$exe='C:\Program Files\WindowsApps\Example\app\ChatGPT.exe'
function Match($f,$v,$a,$i) { [OfficialDesktop.WindowProbe]::MatchesPackage($family,$full,$id,$exe,$f,$v,$a,$i) }
if (-not (Match $family $full $id $exe)) { throw 'Main identity rejected' }
if (-not (Match $family $full '' $exe)) { throw 'Registered Win32 main without process AUMID rejected' }
if ((Match $family $full "$family!Helper" $exe) -or (Match $family 'old-version' $id $exe) -or
    (Match '' '' '' 'C:\Windows\System32\ApplicationFrameHost.exe') -or (Match $family $full $id 'C:\other\ChatGPT.exe')) { throw 'Unrelated identity accepted' }
$legacy='C:\Users\test\AppData\Local\AnthropicClaude\Claude.exe'
$base=$legacy.Substring(0,$legacy.LastIndexOf('\')+1)
if (-not [OfficialDesktop.WindowProbe]::IsLegacyImage($legacy,$base+'app-1.2.3\Claude.exe')) { throw 'Versioned main rejected' }
foreach ($suffix in @('helper.exe', '..\Claude.exe', 'unregistered\Claude.exe')) {
    if ([OfficialDesktop.WindowProbe]::IsLegacyImage($legacy,$base+$suffix)) { throw 'Unrelated legacy path accepted' }
}
'Compiled Win32 interop. Runtime identity and legacy path filters passed.'
