param(
    [Parameter(Mandatory = $true)]
    [string]$BundleArchive,
    [Parameter(Mandatory = $true)]
    [string]$BundleManifest,
    [Parameter(Mandatory = $true)]
    [string]$InstallRoot
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Write-ProgressLine {
    param([int]$Percent, [string]$Message)
    Write-Output "HUGAGENT_PROGRESS|$Percent|$Message"
}

function Invoke-Checked {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$FailureMessage
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)"
    }
}

function Move-DirectoryToCleanup {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    $Parent = Split-Path -Parent $Path
    $Leaf = Split-Path -Leaf $Path
    $Trash = Join-Path $Parent ".$Leaf.cleanup-$([Guid]::NewGuid().ToString('N'))"
    try {
        [System.IO.Directory]::Move($Path, $Trash)
    }
    catch {
        throw "Unable to detach the old runtime directory '$Path': $($_.Exception.Message)"
    }
    return $Trash
}

function Start-DetachedDirectoryCleanup {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Process = [System.Diagnostics.ProcessStartInfo]::new()
    $Process.FileName = $env:ComSpec
    $Process.Arguments = '/d /q /c rd /s /q "{0}"' -f $Path
    $Process.UseShellExecute = $false
    $Process.CreateNoWindow = $true
    $Process.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    [System.Diagnostics.Process]::Start($Process) | Out-Null
}

function Start-FastDirectoryCleanup {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Trash = Move-DirectoryToCleanup $Path
    if ($Trash) {
        Start-DetachedDirectoryCleanup $Trash
    }
}

function Test-PythonCandidate {
    param([string]$Executable, [string[]]$PrefixArguments)
    try {
        & $Executable @PrefixArguments -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Resolve-Python {
    $candidates = @()
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $candidates += [PSCustomObject]@{ Executable = $pyLauncher.Source; Prefix = @("-3.11") }
    }
    foreach ($name in @("python.exe", "python3.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $candidates += [PSCustomObject]@{ Executable = $command.Source; Prefix = @() }
        }
    }
    $localPrograms = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $localPrograms) {
        Get-ChildItem $localPrograms -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
            $candidates += [PSCustomObject]@{ Executable = $_.FullName; Prefix = @() }
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-PythonCandidate $candidate.Executable $candidate.Prefix) {
            return $candidate
        }
    }
    return $null
}

function Test-NodeCandidate {
    param([string]$Executable)
    try {
        $VersionCheck = & $Executable -p "Number(process.versions.node.split('.')[0]) >= 20 ? 'ok' : 'old'" 2>$null
        return $LASTEXITCODE -eq 0 -and $VersionCheck -contains "ok"
    }
    catch {
        return $false
    }
}

function Resolve-Node {
    $Candidates = @()
    $Command = Get-Command "node.exe" -ErrorAction SilentlyContinue
    if ($Command) {
        $Candidates += $Command.Source
    }
    foreach ($Path in @(
        (Join-Path $env:ProgramFiles "nodejs\node.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\nodejs\node.exe")
    )) {
        if (Test-Path $Path) {
            $Candidates += $Path
        }
    }
    $WinGetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $WinGetPackages) {
        Get-ChildItem $WinGetPackages -Filter "node.exe" -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
            $Candidates += $_.FullName
        }
    }
    foreach ($Candidate in $Candidates | Select-Object -Unique) {
        if (Test-NodeCandidate $Candidate) {
            return $Candidate
        }
    }
    return $null
}

function Resolve-Bash {
    $Candidates = @(
        (Join-Path $env:ProgramFiles "Git\bin\bash.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Git\bin\bash.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Git\bin\bash.exe")
    )
    $WinGetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $WinGetPackages) {
        Get-ChildItem $WinGetPackages -Filter "bash.exe" -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.FullName -like "*Git*\bin\bash.exe") {
                $Candidates += $_.FullName
            }
        }
    }
    foreach ($Candidate in $Candidates | Select-Object -Unique) {
        if ($Candidate -and (Test-Path $Candidate)) {
            try {
                & $Candidate --version 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    return $Candidate
                }
            }
            catch {
                # Try the next native Git Bash candidate.
            }
        }
    }
    return $null
}

function Resolve-PythonPackageIndex {
    $Configured = [string]$env:HUGAGENT_PYPI_INDEX_URL
    if ($Configured) {
        return [PSCustomObject]@{
            Url = $Configured.TrimEnd('/')
            Name = "configured mirror"
            AllowOfficialFallback = $false
        }
    }

    $DomesticMirror = "https://mirrors.aliyun.com/pypi/simple"
    try {
        Invoke-WebRequest -Uri "$DomesticMirror/uv/" -Method Head -TimeoutSec 6 -UseBasicParsing | Out-Null
        return [PSCustomObject]@{
            Url = $DomesticMirror
            Name = "Alibaba Cloud mirror"
            AllowOfficialFallback = $true
        }
    }
    catch {
        return [PSCustomObject]@{
            Url = "https://pypi.org/simple"
            Name = "official PyPI"
            AllowOfficialFallback = $false
        }
    }
}

if (-not (Test-Path -LiteralPath $BundleArchive -PathType Leaf)) {
    throw "The desktop package doesn't contain the CE server archive."
}
if (-not (Test-Path -LiteralPath $BundleManifest -PathType Leaf)) {
    throw "The desktop package doesn't contain the CE server manifest."
}

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
$RuntimeRoot = Join-Path $InstallRoot "runtime"
$SourceDir = Join-Path $RuntimeRoot "source"
$VenvDir = Join-Path $RuntimeRoot "venv"
$NodeDataDir = Join-Path $RuntimeRoot "node"
$InstalledManifest = Join-Path $RuntimeRoot "installed-bundle.json"
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null

function Read-DependencyFingerprint {
    param([string]$ManifestPath)
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        return ""
    }
    try {
        $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
        return [string]$Manifest.dependency_fingerprint
    }
    catch {
        return ""
    }
}

$BundledDependencyFingerprint = Read-DependencyFingerprint $BundleManifest
$InstalledDependencyFingerprint = Read-DependencyFingerprint $InstalledManifest

Write-ProgressLine 5 "正在解压同版本服务端资源…"
$StagedSource = Join-Path $RuntimeRoot "source.next-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $StagedSource -Force | Out-Null
$PreviousSource = $null
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($BundleArchive, $StagedSource)
    if (-not (Test-Path -LiteralPath (Join-Path $StagedSource "pyproject.toml") -PathType Leaf)) {
        throw "The extracted CE server payload is invalid."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $StagedSource "src\frontend\dist\index.html") -PathType Leaf)) {
        throw "The bundled CE web application is missing."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $StagedSource "requirements-desktop.txt") -PathType Leaf)) {
        throw "The desktop package doesn't contain its local-server dependency profile."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $StagedSource "requirements-desktop-windows-py311.lock") -PathType Leaf)) {
        throw "The desktop package doesn't contain its Windows Python 3.11 dependency lock."
    }
    if (Test-Path -LiteralPath $SourceDir) {
        $PreviousSource = Move-DirectoryToCleanup $SourceDir
    }
    [System.IO.Directory]::Move($StagedSource, $SourceDir)
    if ($PreviousSource) {
        Start-DetachedDirectoryCleanup $PreviousSource
    }
}
catch {
    if ($PreviousSource -and -not (Test-Path -LiteralPath $SourceDir)) {
        [System.IO.Directory]::Move($PreviousSource, $SourceDir)
        $PreviousSource = $null
    }
    if (Test-Path -LiteralPath $StagedSource) {
        Start-FastDirectoryCleanup $StagedSource
    }
    throw
}

Write-ProgressLine 12 "正在检查 Python 运行环境…"
$Python = Resolve-Python
if (-not $Python) {
    $Winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "Python 3.11 isn't installed, and Windows Package Manager (winget) isn't available. Install Python 3.11 and retry."
    }
    Write-ProgressLine 16 "正在为当前用户安装 Python 3.11…"
    Invoke-Checked $Winget.Source @(
        "install", "--id", "Python.Python.3.11", "--exact", "--scope", "user", "--silent",
        "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"
    ) "Unable to install Python 3.11 with winget"
    $Python = Resolve-Python
    if (-not $Python) {
        throw "Python 3.11 was installed but couldn't be located. Restart Windows, then retry from the desktop app."
    }
}
Write-Output "Using Python: $($Python.Executable)"

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$RebuildVenv = $true
if (Test-Path $VenvPython) {
    & $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" 2>$null
    $RebuildVenv = $LASTEXITCODE -ne 0
    if (-not $RebuildVenv) {
        & $VenvPython -m pip --version 2>$null | Out-Null
        $RebuildVenv = $LASTEXITCODE -ne 0
    }
}
if ($RebuildVenv) {
    Write-ProgressLine 24 "正在创建独立 Python 环境…"
    if (Test-Path $VenvDir) {
        Start-FastDirectoryCleanup $VenvDir
    }
    $VenvArguments = @($Python.Prefix) + @("-m", "venv", $VenvDir)
    Invoke-Checked $Python.Executable $VenvArguments "Unable to create the Python virtual environment"
}

$WindowsRequirementsLock = Join-Path $SourceDir "requirements-desktop-windows-py311.lock"
$VenvUv = Join-Path $VenvDir "Scripts\uv.exe"
$DependenciesChanged = $RebuildVenv -or `
    -not (Test-Path -LiteralPath $VenvUv -PathType Leaf) -or `
    -not $BundledDependencyFingerprint -or `
    $BundledDependencyFingerprint -ne $InstalledDependencyFingerprint

if ($DependenciesChanged) {
    Write-ProgressLine 32 "正在准备锁定的 Python 3.11 运行环境…"
    $PythonPackageIndex = Resolve-PythonPackageIndex
    Write-Output "Using Python package index: $($PythonPackageIndex.Name)"
    $UvRequirement = Get-Content -LiteralPath $WindowsRequirementsLock | Where-Object {
        $_ -match '^uv==[0-9]+\.[0-9]+\.[0-9]+$'
    } | Select-Object -First 1
    if (-not $UvRequirement) {
        throw "The Windows dependency lock doesn't contain an exact uv version."
    }
    $UvPackage = $UvRequirement.Trim()
    & $VenvPython -m pip install --disable-pip-version-check `
        --index-url $PythonPackageIndex.Url $UvPackage
    if ($LASTEXITCODE -ne 0 -and $PythonPackageIndex.AllowOfficialFallback) {
        Write-Warning "The domestic Python mirror couldn't provide uv; retrying official PyPI."
        Invoke-Checked $VenvPython @(
            "-m", "pip", "install", "--disable-pip-version-check",
            "--index-url", "https://pypi.org/simple", $UvPackage
        ) "Unable to prepare the locked dependency installer"
    }
    elseif ($LASTEXITCODE -ne 0) {
        throw "Unable to prepare the locked dependency installer (exit code $LASTEXITCODE)"
    }

    # Keep the build/cache prefix short. Some source distributions contain
    # deeply nested paths and otherwise exceed MAX_PATH before wheel creation.
    $env:UV_CACHE_DIR = Join-Path $env:LOCALAPPDATA "desktop-uv"
    $env:UV_HTTP_RETRIES = "5"

    Write-ProgressLine 42 "正在同步锁定的桌面运行依赖，首次安装需要数分钟…"
    $SyncArguments = @(
        "pip", "sync", "--python", $VenvPython,
        "--only-binary", ":all:",
        "--default-index", $PythonPackageIndex.Url,
        $WindowsRequirementsLock
    )
    & $VenvUv @SyncArguments
    if ($LASTEXITCODE -ne 0 -and $PythonPackageIndex.AllowOfficialFallback) {
        Write-Warning "The domestic Python mirror is incomplete or unavailable; retrying the same locked sync with official PyPI."
        Invoke-Checked $VenvUv @(
            "pip", "sync", "--python", $VenvPython,
            "--only-binary", ":all:",
            "--default-index", "https://pypi.org/simple",
            $WindowsRequirementsLock
        ) "Unable to synchronize the locked desktop dependencies"
    }
    elseif ($LASTEXITCODE -ne 0) {
        throw "Unable to synchronize the locked desktop dependencies (exit code $LASTEXITCODE)"
    }
}
else {
    Write-ProgressLine 70 "Python 依赖未变化，复用现有运行环境…"
}

Write-ProgressLine 75 "正在准备本机 Bash 脚本能力…"
$BashExecutableFile = Join-Path $RuntimeRoot "bash-executable.txt"
$Bash = Resolve-Bash
if (-not $Bash) {
    $Winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if ($Winget) {
        try {
            Invoke-Checked $Winget.Source @(
                "install", "--id", "Git.Git", "--exact", "--scope", "user", "--silent",
                "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"
            ) "Unable to install Git Bash with winget"
            $Bash = Resolve-Bash
        }
        catch {
            Write-Warning "Git Bash couldn't be installed automatically. Python and JavaScript still work; Bash scripts remain unavailable. $($_.Exception.Message)"
        }
    }
}
if ($Bash) {
    [System.IO.File]::WriteAllText(
        $BashExecutableFile,
        [string]$Bash,
        [System.Text.UTF8Encoding]::new($false)
    )
}
elseif (Test-Path $BashExecutableFile) {
    Remove-Item $BashExecutableFile -Force
}

Write-ProgressLine 78 "正在准备可选的 Node.js 文档能力…"
$NodeExecutableFile = Join-Path $RuntimeRoot "node-executable.txt"
$Node = Resolve-Node
if (-not $Node) {
    $Winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if ($Winget) {
        try {
            Invoke-Checked $Winget.Source @(
                "install", "--id", "OpenJS.NodeJS.LTS", "--exact", "--installer-type", "zip", "--silent",
                "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"
            ) "Unable to install Node.js with winget"
            $Node = Resolve-Node
        }
        catch {
            Write-Warning "Node.js wasn't installed automatically. The core service will still work; React site building and advanced PDF rendering remain unavailable. $($_.Exception.Message)"
        }
    }
    else {
        Write-Warning "Node.js 20+ and winget aren't available. The core service will still work; React site building and advanced PDF rendering remain unavailable."
    }
}
if ($Node) {
    [System.IO.File]::WriteAllText(
        $NodeExecutableFile,
        [string]$Node,
        [System.Text.UTF8Encoding]::new($false)
    )
    $NodeDir = Split-Path $Node -Parent
    $env:PATH = "$NodeDir;$env:PATH"
    $Npm = Join-Path $NodeDir "npm.cmd"
    if (-not (Test-Path $Npm)) {
        $NpmCommand = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
        if ($NpmCommand) {
            $Npm = $NpmCommand.Source
        }
    }
    if (Test-Path $Npm) {
        # Playwright/Chromium 体量大（Chromium ~130MB），转入后台安装：核心服务不等它，
        # 先装完先启动。后台任务自测网络——npm 官方源慢或不可达时切 npmmirror 镜像
        # （含 Playwright 浏览器二进制）。进度与失败原因见 runtime\node-tools-install.log。
        $NodeToolsLog = Join-Path $RuntimeRoot "node-tools-install.log"
        $NodeToolsScript = Join-Path $RuntimeRoot "install-node-tools.ps1"
        $NodeToolsWorker = @'
param([string]$Npm, [string]$NodeDataDir, [string]$LogFile)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

function Log([string]$Message) {
    $Line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $Line -Encoding UTF8
}

$LockFile = "$LogFile.lock"
if (Test-Path $LockFile) {
    $OtherProcessId = Get-Content $LockFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($OtherProcessId -and (Get-Process -Id $OtherProcessId -ErrorAction SilentlyContinue)) {
        exit 0
    }
}
Set-Content -Path $LockFile -Value $PID -Encoding ASCII

try {
    Log "Preparing optional Node.js document tools (pptxgenjs + Playwright Chromium)."
    $UseMirror = $false
    $Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        Invoke-WebRequest -Uri "https://registry.npmjs.org/" -Method Head -TimeoutSec 6 -UseBasicParsing | Out-Null
        if ($Stopwatch.ElapsedMilliseconds -gt 3000) {
            $UseMirror = $true
        }
    }
    catch {
        $UseMirror = $true
    }
    if ($UseMirror) {
        Log "registry.npmjs.org is slow or unreachable; switching to npmmirror.com mirrors."
    }

    $env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1"
    $NpmArguments = @(
        "install", "--silent", "--no-audit", "--no-fund", "--no-package-lock",
        "--prefix", $NodeDataDir, "pptxgenjs", "playwright"
    )
    if ($UseMirror) {
        $NpmArguments += @("--registry", "https://registry.npmmirror.com")
    }
    & $Npm @NpmArguments 2>&1 | ForEach-Object { Log "$_" }
    if ($LASTEXITCODE -ne 0) {
        Log "npm install failed (exit code $LASTEXITCODE); optional document tools stay unavailable for now."
        exit 1
    }

    $Playwright = Join-Path $NodeDataDir "node_modules\.bin\playwright.cmd"
    if (-not (Test-Path $Playwright)) {
        Log "The playwright CLI wasn't found after npm install."
        exit 1
    }
    Remove-Item Env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD -ErrorAction SilentlyContinue
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $NodeDataDir "browsers"
    if ($UseMirror) {
        $env:PLAYWRIGHT_DOWNLOAD_HOST = "https://cdn.npmmirror.com/binaries/playwright"
    }
    & $Playwright install chromium 2>&1 | ForEach-Object { Log "$_" }
    if ($LASTEXITCODE -ne 0 -and -not $UseMirror) {
        Log "Chromium download from the official host failed; retrying via npmmirror."
        $env:PLAYWRIGHT_DOWNLOAD_HOST = "https://cdn.npmmirror.com/binaries/playwright"
        & $Playwright install chromium 2>&1 | ForEach-Object { Log "$_" }
    }
    if ($LASTEXITCODE -ne 0) {
        Log "Chromium download failed. Word, Excel, and PPT generation still work; advanced PDF rendering will use its fallback."
        exit 1
    }
    Log "Optional Node.js document tools are ready."
}
finally {
    Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
}
'@
        try {
            [System.IO.File]::WriteAllText(
                $NodeToolsScript,
                $NodeToolsWorker,
                [System.Text.UTF8Encoding]::new($true)
            )
            $NodeToolsArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Npm "{1}" -NodeDataDir "{2}" -LogFile "{3}"' -f `
                $NodeToolsScript, $Npm, $NodeDataDir, $NodeToolsLog
            Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList $NodeToolsArguments | Out-Null
            Write-Output "Optional document tools (pptxgenjs + Playwright Chromium) are installing in the background; see $NodeToolsLog"
        }
        catch {
            Write-Warning "Optional Node.js tools couldn't be scheduled. The core service will still start. $($_.Exception.Message)"
        }
    }
    else {
        Write-Warning "Node.js is available but npm.cmd wasn't found. Optional document tools weren't installed."
    }
}
elseif (Test-Path $NodeExecutableFile) {
    Remove-Item $NodeExecutableFile -Force
}

Write-ProgressLine 86 "正在注册 HugAgentOS 本机服务…"
Invoke-Checked $VenvUv @(
    "pip", "install", "--python", $VenvPython,
    "--no-deps", "--editable", $SourceDir
) "Unable to install the HugAgentOS command"

$HugAgentOSCommand = Join-Path $VenvDir "Scripts\hugagent.exe"
if (-not (Test-Path $HugAgentOSCommand)) {
    throw "The HugAgentOS service command wasn't installed correctly."
}
Copy-Item $BundleManifest $InstalledManifest -Force

# Version 0.2.2 and earlier mixed disposable runtime files into InstallRoot and
# put optional Node packages under data. The new runtime is live now, so detach
# those legacy trees and let native rd remove them without blocking startup.
foreach ($LegacyDirectory in @(
    (Join-Path $InstallRoot "source"),
    (Join-Path $InstallRoot "venv"),
    (Join-Path $InstallRoot "data\node")
)) {
    if (Test-Path -LiteralPath $LegacyDirectory) {
        Start-FastDirectoryCleanup $LegacyDirectory
    }
}
foreach ($LegacyFile in @(
    (Join-Path $InstallRoot "installed-bundle.json"),
    (Join-Path $InstallRoot "node-executable.txt"),
    (Join-Path $InstallRoot "bash-executable.txt")
)) {
    Remove-Item -LiteralPath $LegacyFile -Force -ErrorAction SilentlyContinue
}
Write-ProgressLine 90 "本机服务安装完成，正在启动…"
Write-Output "Local server installed at $InstallRoot"
