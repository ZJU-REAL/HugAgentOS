param(
    [Parameter(Mandatory = $true)]
    [string]$BundleDir,
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

if (-not (Test-Path (Join-Path $BundleDir "pyproject.toml"))) {
    throw "The desktop package doesn't contain a valid CE server payload."
}
if (-not (Test-Path (Join-Path $BundleDir "src\frontend\dist\index.html"))) {
    throw "The bundled CE web application is missing."
}

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
$SourceDir = Join-Path $InstallRoot "source"
$VenvDir = Join-Path $InstallRoot "venv"
$InstalledManifest = Join-Path $InstallRoot "installed-bundle.json"

Write-ProgressLine 5 "正在复制同版本服务端资源…"
New-Item -ItemType Directory -Path $SourceDir -Force | Out-Null
& robocopy.exe $BundleDir $SourceDir /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS /NP
$RobocopyCode = $LASTEXITCODE
if ($RobocopyCode -gt 7) {
    throw "Unable to copy the server payload (robocopy exit code $RobocopyCode)."
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
        Remove-Item -Path $VenvDir -Recurse -Force
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

Write-ProgressLine 70 "正在安装本机脚本与文档处理能力…"
Invoke-Checked $VenvPython @(
    "-m", "pip", "install", "--disable-pip-version-check", "--prefer-binary",
    "-r", (Join-Path $SourceDir "docker\requirements-script-runner.txt")
) "Unable to install the local tool dependencies"

Write-ProgressLine 77 "正在准备可选的 Node.js 文档能力…"
$NodeExecutableFile = Join-Path $InstallRoot "node-executable.txt"
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
            $NodeDataDir = Join-Path $InstallRoot "data\node"
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
Copy-Item (Join-Path $BundleDir "desktop-bundle.json") $InstalledManifest -Force
Write-ProgressLine 90 "本机服务安装完成，正在启动…"
Write-Output "Local server installed at $InstallRoot"
