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
        & $Executable @PrefixArguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
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
        $candidates += [PSCustomObject]@{ Executable = $pyLauncher.Source; Prefix = @("-3") }
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
    if (-not (Test-Path -LiteralPath (Join-Path $StagedSource "requirements-mem0.txt") -PathType Leaf)) {
        throw "The desktop package doesn't contain the persistent-memory dependencies."
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
        throw "Python 3.11+ isn't installed, and Windows Package Manager (winget) isn't available. Install Python 3.11 and retry."
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
    & $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
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

Write-ProgressLine 32 "正在更新 Python 安装工具…"
Invoke-Checked $VenvPython @("-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip", "setuptools", "wheel") "Unable to prepare pip"

Write-ProgressLine 42 "正在安装服务端依赖，首次安装需要数分钟…"
Invoke-Checked $VenvPython @(
    "-m", "pip", "install", "--disable-pip-version-check", "--prefer-binary",
    "-r", (Join-Path $SourceDir "requirements.txt")
) "Unable to install the server dependencies"

Write-ProgressLine 58 "正在安装永久记忆运行环境…"
Invoke-Checked $VenvPython @(
    "-m", "pip", "install", "--disable-pip-version-check", "--prefer-binary", "--upgrade",
    "-r", (Join-Path $SourceDir "requirements-mem0.txt"),
    "protobuf<7", "pymilvus==2.5.18", "milvus-lite==3.1.0"
) "Unable to install the persistent-memory dependencies"

Write-ProgressLine 70 "正在安装本机脚本与文档处理能力…"
Invoke-Checked $VenvPython @(
    "-m", "pip", "install", "--disable-pip-version-check", "--prefer-binary",
    "-r", (Join-Path $SourceDir "docker\requirements-script-runner.txt")
) "Unable to install the local tool dependencies"

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
        try {
            $env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1"
            Invoke-Checked $Npm @(
                "install", "--silent", "--no-audit", "--no-fund", "--no-package-lock",
                "--prefix", $NodeDataDir, "pptxgenjs", "playwright"
            ) "Unable to install the optional Node.js tool dependencies"
            $Playwright = Join-Path $NodeDataDir "node_modules\.bin\playwright.cmd"
            if (Test-Path $Playwright) {
                $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $NodeDataDir "browsers"
                & $Playwright install chromium
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning "Chromium download failed. Word, Excel, and PPT generation still work; advanced PDF rendering will use its fallback."
                }
            }
        }
        catch {
            Write-Warning "Optional Node.js tools couldn't be prepared. The core service will still start. $($_.Exception.Message)"
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
Invoke-Checked $VenvPython @(
    "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--editable", $SourceDir
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
