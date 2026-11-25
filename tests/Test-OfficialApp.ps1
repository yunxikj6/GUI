# Runs without Windows accounts, app registrations, GUI, signing services or network.
$ErrorActionPreference = 'Stop'
$scriptFile = Join-Path (Split-Path $PSScriptRoot) 'windows/Open-OfficialApp.ps1'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString())
[IO.Directory]::CreateDirectory($testRoot) | Out-Null
try {
    $fixturePackages = @{}
    $fixtureNames = @()
    $fixtureApps = @()
    $validSignature = $true
    $fixtureStandalone = $false
    $fixtureSigner = 'Anthropic, PBC'
    $fixtureProduct = 'Claude'
    function Get-AppxPackage {
        [CmdletBinding()]param([string]$Name)
        if ($fixturePackages.ContainsKey($Name)) { $fixturePackages[$Name] }
    }
    function Get-AppxPackageManifest {
        [CmdletBinding()]param([string]$Package)
        [pscustomobject]@{ Package = @{ Applications = @{ Application = $fixtureApps } } }
    }
    function Get-StartApps { [CmdletBinding()]param() $fixtureNames }
    function Get-AuthenticodeSignature {
        [CmdletBinding()]param([string]$LiteralPath)
        $cert = [pscustomobject]@{}
        $cert | Add-Member ScriptMethod GetNameInfo { param($type, $issuer) $fixtureSigner }
        $state = if ($validSignature) { 'Valid' } else { 'NotSigned' }
        [pscustomobject]@{ Status=$state; SignerCertificate=$cert }
    }
    # Fixture only. A missing app must not touch the real LocalAppData tree.
    function Test-Path { [CmdletBinding()]param([string]$LiteralPath, [string]$PathType) $fixtureStandalone }
    function Get-Item { [CmdletBinding()]param([string]$LiteralPath) [pscustomobject]@{ VersionInfo = @{ ProductName = $fixtureProduct } } }
    function Package([string]$family, [string]$signature) {
        [pscustomobject]@{ PackageFamilyName=$family; Status='Ok'; IsDevelopmentMode=$false;
            SignatureKind=$signature; PackageFullName=$family + '_fixture'; InstallLocation=$testRoot; Version=[Version]'1.0' }
    }
    function Check([string]$app, [int]$code, [string]$status, [string]$id = '') {
        $json = (& $scriptFile -App $app -Check) | ConvertFrom-Json
        if ($LASTEXITCODE -ne $code -or $json.status -ne $status -or [string]$json.aumid -ne $id) {
            throw "Unexpected $app result $LASTEXITCODE $($json | ConvertTo-Json -Compress)"
        }
    }
    Check ChatGPT 2 'not-installed'
    $current = 'OpenAI.Codex_2p2nqsd0c76g0'
    $classic = 'OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0'
    $claude = 'Claude_pzs8sxrjxfjjc'
    $store = 'AnthropicPBC.Claude_fnn82j28hfe8t'
    $fixturePackages['OpenAI.Codex'] = Package $current 'Store'
    $fixtureApps = @(@{ Id='CodexChromeNativeHost' }, @{ Id='App' }, @{ Id='ChatGPT' })
    $fixtureNames = @(@{ AppID="$current!App"; Name='Codex' })
    Check ChatGPT 2 'not-installed'
    $fixturePackages['OpenAI.ChatGPT-Desktop'] = Package $classic 'Store'
    Check ChatGPT 0 'verified' "$classic!ChatGPT"
    $fixtureNames = @(@{ AppID="$current!App"; Name='ChatGPT' })
    Check ChatGPT 0 'verified' "$current!App"
    $fixturePackages['OpenAI.Codex'].PackageFamilyName = 'OpenAI.Codex_unrelated'
    Check ChatGPT 0 'verified' "$classic!ChatGPT"
    $fixturePackages.Clear()
    $fixturePackages.Claude = Package $claude 'Developer'
    $fixtureApps = @(@{ Id='SshProxy'; Executable='app\helper.exe' }, @{ Id='Claude'; Executable='app\Claude.exe' })
    Check Claude 0 'verified' "$claude!Claude"
    $validSignature=$false
    Check Claude 3 'unverified'
    $fixturePackages['AnthropicPBC.Claude'] = Package $store 'Store'
    Check Claude 0 'verified' "$store!Claude"
    $fixturePackages['AnthropicPBC.Claude'].IsDevelopmentMode = $true
    Check Claude 3 'unverified'
    $fixturePackages.Clear()
    $fixturePackages['AnthropicPBC.Claude'] = Package $store 'Developer'
    Check Claude 3 'unverified'
    $fixturePackages['AnthropicPBC.Claude'].SignatureKind = 'Store'
    $fixtureApps = @(@{ Id='OnlyAHelper' })
    Check Claude 3 'unverified'
    $fixturePackages.Clear()
    $fixtureStandalone=$true
    $validSignature=$true
    Check Claude 0 'verified'
    $fixtureSigner='Unrelated Publisher'
    Check Claude 3 'unverified'
    $fixtureProduct='Claude Code'
    $fixtureSigner='Anthropic, PBC'
    Check Claude 3 'unverified'

    # This fake activation distinguishes verification, submission and window observation.
    Add-Type -TypeDefinition 'namespace OfficialDesktop { public static class Activation { public static string Last; public static uint Request(string id) { Last=id; return 123; } } }'
    $fixtureStandalone=$false
    $fixturePackages['AnthropicPBC.Claude'] = Package $store 'Store'
    $fixtureApps = @(@{ Id='Claude' })
    Check Claude 0 'verified' "$store!Claude"
    if ([OfficialDesktop.Activation]::Last) { throw 'Check triggered activation' }
    $reply = (& $scriptFile -App Claude) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $reply.status -ne 'activation-requested' -or $reply.processId -ne 123 -or [OfficialDesktop.Activation]::Last -ne "$store!Claude") { throw 'Wrong activation' }
    '16 installed-identity and activation cases passed.'
} finally {
    [IO.Directory]::Delete($testRoot, $true)
}
