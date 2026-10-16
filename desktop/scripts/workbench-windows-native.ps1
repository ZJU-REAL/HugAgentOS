# Read-only native observation; never builds, installs, launches, stops or publishes.
[CmdletBinding()]
param(
    [ValidateSet('Preflight','Begin','Observe')][string]$Action = 'Preflight',
    [string]$BatchRoot,
    [string]$SourceBatch,
    [string]$Installer,
    [string]$ApplicationExecutable,
    [ValidateSet('local','cloud','dual')][string]$Mode,
    [int]$NativeProcessId,
    [switch]$IsolatedTestEnvironment
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Hash-File([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Assert-RegularPath([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $resolved -Force
    while ($item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Reparse paths are not permitted for native evidence.'
        }
        $parent = Split-Path -Parent $item.FullName
        if (!$parent -or $parent -eq $item.FullName) { break }
        $item = Get-Item -LiteralPath $parent -Force
    }
    return $resolved
}
function Write-NewJson([string]$Path, $Value) {
    if (Test-Path -LiteralPath $Path) { throw "Evidence already exists: $Path" }
    $json = $Value | ConvertTo-Json -Depth 12
    [IO.File]::WriteAllText($Path, $json, [Text.UTF8Encoding]::new($false))
}
function Get-Inventory {
    $tools = [ordered]@{}
    foreach ($name in @('node','npm','rustc','cargo','uv','ssh')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        $tools[$name] = if ($command) { $command.Source } else { $null }
    }
    $webview = @()
    foreach ($root in @('HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients',
                        'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients')) {
        if (Test-Path -LiteralPath $root) {
            foreach ($key in Get-ChildItem -LiteralPath $root) {
                $properties = Get-ItemProperty -LiteralPath $key.PSPath
                if ($properties.PSObject.Properties['name'] -and $properties.name -match 'WebView2') {
                    $webview += @{name=$properties.name; version=$properties.pv}
                }
            }
        }
    }
    $os = Get-CimInstance Win32_OperatingSystem
    $cpu = @(Get-CimInstance Win32_Processor | Select-Object Name)
    return [ordered]@{
        platform = 'windows-x86_64'
        os = $os.Caption
        build = $os.BuildNumber
        architecture = $env:PROCESSOR_ARCHITECTURE
        memoryBytes = [long]$os.TotalVisibleMemorySize * 1024
        cpu = $cpu
        interactive = [Environment]::UserInteractive
        tools = $tools
        webview2 = $webview
        recordedUtc = [DateTime]::UtcNow.ToString('o')
        status = 'inventory-only'
    }
}
if ($Action -eq 'Preflight') {
    Get-Inventory | ConvertTo-Json -Depth 8
    exit 0
}
if (!$IsolatedTestEnvironment) {
    throw 'Run only in a dedicated test user/VM. Environment overrides do not isolate Tauri known folders or credentials.'
}
if (!$BatchRoot) { throw 'BatchRoot is required.' }
$batch = [IO.Path]::GetFullPath($BatchRoot).TrimEnd('\')
$temp = (Assert-RegularPath ([IO.Path]::GetTempPath())).TrimEnd('\')
if ((Split-Path -Parent $batch) -ne $temp -or (Split-Path -Leaf $batch) -notmatch '^wb24-native-[a-zA-Z0-9-]+$') {
    throw 'BatchRoot must be a new wb24-native-* directory directly beneath this test user TEMP.'
}
if ($Action -eq 'Begin') {
    if (Test-Path -LiteralPath $batch) { throw 'BatchRoot already exists; preserve the previous batch.' }
    if (!$SourceBatch -or !$Installer -or !$ApplicationExecutable) { throw 'SourceBatch, Installer and ApplicationExecutable are required.' }
    $sourceDir = Assert-RegularPath $SourceBatch
    $installerPath = Assert-RegularPath $Installer
    $exePath = Assert-RegularPath $ApplicationExecutable
    if ([IO.Path]::GetExtension($installerPath) -ne '.exe' -or [IO.Path]::GetExtension($exePath) -ne '.exe') {
        throw 'Use the actual NSIS installer and installed Tauri executable.'
    }
    $source = Get-Content -LiteralPath (Join-Path $sourceDir 'source.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($source.platform -ne 'windows-x86_64' -or $source.protocol -ne '1.0.0') { throw 'Wrong source receipt platform/protocol.' }
    $fileVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo($exePath)
    if ($fileVersion.ProductVersion -notmatch ('^' + [regex]::Escape($source.version) + '(\.|$)')) {
        throw 'Installed executable version differs from source receipt.'
    }
    New-Item -ItemType Directory -Path $batch | Out-Null
    Copy-Item -LiteralPath (Join-Path $sourceDir 'source.json') -Destination $batch
    Copy-Item -LiteralPath (Join-Path $sourceDir 'results.json') -Destination $batch
    $artifact = [ordered]@{
        schemaVersion = 1
        level = 'native'
        isolatedTestEnvironment = $true
        sourceReceiptSha256 = Hash-File (Join-Path $batch 'source.json')
        installerName = [IO.Path]::GetFileName($installerPath)
        installerSha256 = Hash-File $installerPath
        installerBytes = (Get-Item -LiteralPath $installerPath).Length
        applicationPath = $exePath
        applicationSha256 = Hash-File $exePath
        applicationVersion = $fileVersion.ProductVersion
        authenticode = (Get-AuthenticodeSignature -LiteralPath $exePath).Status.ToString()
        recordedUtc = [DateTime]::UtcNow.ToString('o')
        acceptance = 'not-run'
    }
    Write-NewJson (Join-Path $batch 'artifact.json') $artifact
    Write-NewJson (Join-Path $batch 'inventory.json') (Get-Inventory)
    Write-Output "Native evidence batch prepared: $batch"
    exit 0
}
Assert-RegularPath $batch | Out-Null
if (!$Mode -or $NativeProcessId -le 0) { throw 'Observe requires Mode and the running installed Tauri process ID.' }
$artifact = Get-Content -LiteralPath (Join-Path $batch 'artifact.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$processes = @(Get-CimInstance Win32_Process)
$app = $processes | Where-Object ProcessId -eq $NativeProcessId | Select-Object -First 1
if (!$app -or !$app.ExecutablePath -or $app.ExecutablePath -ne $artifact.applicationPath) {
    throw 'Process ID does not match the recorded installed application.'
}
if ((Hash-File (Assert-RegularPath $app.ExecutablePath)) -ne $artifact.applicationSha256) {
    throw 'Installed application changed; create a separate batch.'
}
# Descendants tie actual WebView2 to this application without dumping command lines.
$ids = @([uint32]$NativeProcessId)
do {
    $new = @($processes | Where-Object { $_.ParentProcessId -in $ids -and $_.ProcessId -notin $ids })
    $ids += @($new | ForEach-Object { [uint32]$_.ProcessId })
} while ($new.Count -gt 0)
$webviews = @($processes | Where-Object { $_.ProcessId -in $ids -and $_.Name -eq 'msedgewebview2.exe' })
if ($webviews.Count -eq 0) { throw 'No actual WebView2 child found. A browser tab is not native evidence.' }
$details = @($webviews | ForEach-Object {
    if (!$_.ExecutablePath) { throw 'WebView2 image path cannot be read.' }
    $version = [Diagnostics.FileVersionInfo]::GetVersionInfo($_.ExecutablePath)
    @{processId=$_.ProcessId; parentProcessId=$_.ParentProcessId; version=$version.ProductVersion}
})
Write-NewJson (Join-Path $batch ("native-" + $Mode + ".json")) ([ordered]@{
    mode = $Mode
    level = 'native'
    processId = $NativeProcessId
    applicationSha256 = $artifact.applicationSha256
    sourceReceiptSha256 = Hash-File (Join-Path $batch 'source.json')
    webview2 = $details
    recordedUtc = [DateTime]::UtcNow.ToString('o')
    acceptance = 'observation-only'
})
Write-Output "Recorded actual Tauri/WebView2 process for $Mode. Behavior cases remain pending."
