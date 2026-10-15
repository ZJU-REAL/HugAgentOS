# Windows checks of a frozen copy. TestPackage creates an internal package; neither action runs or installs it.
[CmdletBinding()]
param(
    [ValidateSet('Compile','TestPackage')][string]$Action = 'Compile',
    [Parameter(Mandatory=$true)][string]$SourceRoot,
    [Parameter(Mandatory=$true)][string]$SourceReceipt,
    [Parameter(Mandatory=$true)][string]$OutputRoot
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Windows x86_64 compiler required.' }
if ($env:TAURI_CONFIG) { throw 'Clear unrelated TAURI_CONFIG before this isolated check.' }
if ($Action -eq 'TestPackage' -and ($env:HUGAGENT_RELEASE_BUILD -eq '1' -or $env:CI)) {
    throw 'Internal unsigned test packaging cannot run in a formal release/CI context.'
}
function Assert-RegularPath([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $resolved -Force
    while ($item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Source/output links are prohibited.' }
        $parent = Split-Path -Parent $item.FullName
        if (!$parent -or $parent -eq $item.FullName) { break }
        $item = Get-Item -LiteralPath $parent -Force
    }
    return $resolved
}
function Hash([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
$source = (Assert-RegularPath $SourceRoot).TrimEnd('\')
$receiptPath = Assert-RegularPath $SourceReceipt
$receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($receipt.platform -ne 'windows-x86_64' -or $receipt.protocol -ne '1.0.0') {
    throw 'Wrong receipt platform/protocol.'
}
$output = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\')
$temp = (Assert-RegularPath ([IO.Path]::GetTempPath())).TrimEnd('\')
if ((Split-Path -Parent $output) -ne $temp -or (Split-Path -Leaf $output) -notmatch '^wb24-compile-[a-zA-Z0-9-]+$') {
    throw 'OutputRoot must be a new TEMP child named wb24-compile-*.'
}
if (Test-Path -LiteralPath $output) { throw 'Never overwrite an existing compile batch.' }
function Verify-Source {
    Push-Location $source
    try {
        $modes = @{}
        & git -c core.quotePath=false ls-files --stage | ForEach-Object {
            $parts = $_ -split "`t", 2
            if ($parts.Count -eq 2) { $modes[$parts[1]] = ($parts[0] -split ' ')[0] }
        }
        if ($LASTEXITCODE -ne 0) { throw 'Cannot verify native-copy Git modes.' }
    } finally { Pop-Location }
    foreach ($entry in $receipt.files) {
        if ([IO.Path]::IsPathRooted($entry.path) -or $entry.path -match '(^|[/\\])\.\.([/\\]|$)') {
            throw 'Receipt path escapes source.'
        }
        $candidate = Join-Path $source $entry.path
        if ($entry.PSObject.Properties.Name -contains 'deleted' -and $entry.deleted) {
            if (Test-Path -LiteralPath $candidate) { throw "Retired source resurrected: $($entry.path)" }
            continue
        }
        if ($entry.kind -ne 'file') { throw 'Native copy requires regular files; symlink receipts need explicit equivalent materialization.' }
        $path = Assert-RegularPath $candidate
        $gitMode = if ($modes.ContainsKey($entry.path)) { $modes[$entry.path] } else { '100644' }
        if ($gitMode -ne $entry.mode) { throw "Frozen source mode mismatch: $($entry.path)" }
        if (!$path.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase) -or (Hash $path) -ne $entry.sha256) {
            throw "Frozen source mismatch: $($entry.path)"
        }
    }
}
Verify-Source
New-Item -ItemType Directory -Path $output | Out-Null
Copy-Item -LiteralPath $receiptPath -Destination (Join-Path $output 'source.json')
$savedTarget = $env:CARGO_TARGET_DIR
$savedConfig = $env:TAURI_CONFIG
$exitCode = -1
$verifiedAfter = $false
$started = [DateTime]::UtcNow.ToString('o')
$command = 'cargo check --lib --locked --offline --target x86_64-pc-windows-msvc'
$level = 'native-compile-only'
try {
    $env:CARGO_TARGET_DIR = Join-Path $output 'target'
    if ($Action -eq 'Compile') {
        # Canonical documented compile-only override. No private runtime/app assets are claimed.
        $env:TAURI_CONFIG = '{"bundle":{"resources":[]}}'
        Push-Location (Join-Path $source 'desktop\src-tauri')
        try {
            $log = Join-Path $output 'cargo-check.log'
            # Redirect native stderr; PowerShell 5 otherwise treats compiler progress as terminating errors.
            & $env:ComSpec /d /s /c "$command > `"$log`" 2>&1"
            $exitCode = $LASTEXITCODE
        } finally { Pop-Location }
    } else {
        $level = 'native-internal-test-package'
        $override = Join-Path $output 'test-only.json'
        [IO.File]::WriteAllText($override, '{"bundle":{"createUpdaterArtifacts":false}}',
            [Text.UTF8Encoding]::new($false))
        $command = 'npm.cmd run build -- --config <batch>/test-only.json --no-sign -- --locked'
        Push-Location (Join-Path $source 'desktop')
        try {
            $log = Join-Path $output 'test-package.log'
            # Keep the existing full bundle pipeline. Only updater signing artifacts are disabled for this test package.
            & $env:ComSpec /d /s /c "npm.cmd run build -- --config `"$override`" --no-sign -- --locked > `"$log`" 2>&1"
            $exitCode = $LASTEXITCODE
        } finally { Pop-Location }
    }
    Verify-Source
    $verifiedAfter = $true
} finally {
    $env:CARGO_TARGET_DIR = $savedTarget
    $env:TAURI_CONFIG = $savedConfig
    $result = [ordered]@{
        level = $level
        command = $command
        baseline = $receipt.baseline
        commit = $receipt.commit
        sourceTreeHash = $receipt.sourceSha256
        sourceReceiptSha256 = Hash (Join-Path $output 'source.json')
        startedUtc = $started
        completedUtc = [DateTime]::UtcNow.ToString('o')
        exitCode = $exitCode
        sourceVerifiedAfter = $verifiedAfter
        acceptance = 'not-run'
        distribution = 'internal-test-only; do not publish or install into the daily user profile'
        scope = 'Build/compile evidence only; no Windows application behavior verified.'
    }
    [IO.File]::WriteAllText((Join-Path $output 'compile-result.json'),
        ($result | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
}
if ($exitCode -ne 0) { throw "Native $Action failed ($exitCode). Preserve logs; do not loosen checks." }
Write-Output "Windows native $Action passed. A13 remains not-run. Evidence: $output"
