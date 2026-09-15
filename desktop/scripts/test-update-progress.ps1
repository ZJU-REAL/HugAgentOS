# Tauri's application manifest is not linked into Cargo's library-test harness.
# Copy it from the built application so Common Controls v6/TaskDialogIndirect resolves.
param(
    [string]$TestExecutable,
    [string]$ApplicationExecutable
)
$ErrorActionPreference = 'Stop'
$rustRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\src-tauri'))
if (!$ApplicationExecutable) {
    $ApplicationExecutable = Join-Path $rustRoot 'target\release\hugagent-desktop.exe'
}
if (!(Test-Path -LiteralPath $ApplicationExecutable)) { throw 'Build the desktop application first.' }
if (!$TestExecutable) {
    Push-Location $rustRoot
    try {
        $messages = & cargo test --release --lib --no-run --message-format=json
        if ($LASTEXITCODE -ne 0) { throw 'Native test compilation failed.' }
        $artifacts = @($messages | ForEach-Object {
            try { $row = $_ | ConvertFrom-Json } catch { return }
            if ($row.reason -eq 'compiler-artifact' -and $row.profile.test -and $row.executable) {
                $row.executable
            }
        })
        if ($artifacts.Count -ne 1) { throw 'Expected one native library-test executable.' }
        $TestExecutable = $artifacts[0]
    } finally { Pop-Location }
}
$mt = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\mt.exe" |
    Sort-Object FullName -Descending | Select-Object -First 1
if (!$mt) { throw 'Windows SDK Manifest Tool (mt.exe) is required.' }
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('updater-progress-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixture | Out-Null
$manifest = Join-Path $fixture 'application.manifest'
$testCopy = Join-Path $fixture 'progress-test.exe'
Copy-Item -LiteralPath $TestExecutable -Destination $testCopy
& $mt.FullName "-inputresource:$ApplicationExecutable;#1" "-out:$manifest"
if ($LASTEXITCODE -ne 0) { throw 'Application manifest extraction failed.' }
& $mt.FullName -manifest $manifest "-outputresource:$testCopy;#1"
if ($LASTEXITCODE -ne 0) { throw 'Test manifest embedding failed.' }
& $testCopy native_update_progress_window_loads_and_renders --ignored --nocapture
$testExit = $LASTEXITCODE
# Only known files created above; no recursive deletion or link traversal.
Remove-Item -LiteralPath $testCopy,$manifest
Remove-Item -LiteralPath $fixture
if ($testExit -ne 0) { throw "Native update progress regression failed: $testExit" }
