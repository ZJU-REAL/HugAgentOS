param(
    [Parameter(Mandatory=$true)][string]$FixtureRoot,
    [Parameter(Mandatory=$true)][string]$HelperPath
)
$ErrorActionPreference = 'Stop'
$fixture = [IO.Path]::GetFullPath($FixtureRoot).TrimEnd('\')
if (-not ([IO.Path]::GetFileName($fixture)).StartsWith('codex-cap-uninstall-')) { throw 'Expected isolated fixture root' }
if ([IO.Path]::GetFileName([IO.Path]::GetDirectoryName($fixture)) -ne 'Temp') { throw 'Fixture must be directly inside TEMP' }
. $HelperPath
$passed = 0
function Check([bool]$Condition, [string]$Label) {
    if (-not $Condition) { throw $Label }
}
function Write-Fixture([string]$Path, [string]$Content) {
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path)) | Out-Null
    [IO.File]::WriteAllText($Path, $Content)
}
function Make-Install([string]$Name) {
    $app = Join-Path $fixture "$Name\com.hugagent.desktop"
    foreach ($entry in @('skills\local\skill\r1\SKILL.md','plugins\local\plugin\r1\plugin.json','agents\local\agent\r1\AGENT.md','mcp.json','.capabilities\index.json','local-server\data\data.db','local-server\data\workspace\notes.txt','local-server\runtime\python.exe')) {
        Write-Fixture (Join-Path $app $entry) "sentinel:$entry"
    }
    return $app
}
function Detach-Runtime([string]$App, [bool]$Preserve) {
    # Same-directory moves used by the NSIS hooks, using one runtime end-to-end.
    $runtime = Join-Path $App 'local-server'
    $held = Join-Path $App 'held-data'
    $detached = Join-Path $App 'remove-test.tmp'
    Assert-HugAgentOSCleanupRoot $App
    if ($Preserve) { [IO.Directory]::Move((Join-Path $runtime 'data'), $held) }
    [IO.Directory]::Move($runtime, $detached)
    if ($Preserve) {
        [IO.Directory]::CreateDirectory($runtime) | Out-Null
        [IO.Directory]::Move($held, (Join-Path $runtime 'data'))
    }
    return $detached
}
function Pass([string]$Name) { $script:passed++; Write-Output "PASS $Name" }

$app = Make-Install 'preserve'
$target = Join-Path $app 'skills\local\skill\r1'
New-Item -ItemType Junction -Path (Join-Path $app 'local-server\data\workspace\skill-view') -Target $target | Out-Null
$detached = Detach-Runtime $app $true
Invoke-HugAgentOSCleanup -AppRoot $app -DetachedRuntime $detached -DeleteUserData $false
foreach ($entry in @('skills\local\skill\r1\SKILL.md','plugins\local\plugin\r1\plugin.json','agents\local\agent\r1\AGENT.md','mcp.json','.capabilities\index.json','local-server\data\data.db','local-server\data\workspace\notes.txt','local-server\data\workspace\skill-view\SKILL.md')) {
    Check ([IO.File]::ReadAllText((Join-Path $app $entry)).StartsWith('sentinel:')) "Preserve failed: $entry"
}
Check (-not [IO.Directory]::Exists($detached)) 'Detached runtime remains'
Pass 'default/silent-update preservation keeps all four kinds, index, database and workspace junction'

$app = Make-Install 'delete'
$outside = Join-Path $fixture 'external-user-data'
Write-Fixture (Join-Path $outside 'do-not-delete.txt') 'external-sentinel'
New-Item -ItemType Junction -Path (Join-Path $app 'local-server\runtime\external-link') -Target $outside | Out-Null
New-Item -ItemType Junction -Path (Join-Path $app 'plugins\outside-link') -Target $outside | Out-Null
$detached = Detach-Runtime $app $false
Invoke-HugAgentOSCleanup -AppRoot $app -DetachedRuntime $detached -DeleteUserData $true
foreach ($entry in @('skills','plugins','agents','mcp.json','.capabilities','local-server')) {
    Check (-not (Test-Path -LiteralPath (Join-Path $app $entry))) "Explicit delete left $entry"
}
Check ([IO.File]::ReadAllText((Join-Path $outside 'do-not-delete.txt')) -eq 'external-sentinel') 'Cleanup followed junction into external data'
Pass 'explicit delete clears four kinds and business data while preserving external junction targets'

$app = Make-Install 'root-escape'
$badRoot = Join-Path $fixture 'redirected-app'
New-Item -ItemType Junction -Path $badRoot -Target $app | Out-Null
$rejected = $false
try { Assert-HugAgentOSCleanupRoot $badRoot } catch { $rejected=$true }
Check $rejected 'Redirected application root accepted'
Check (Test-Path -LiteralPath (Join-Path $app 'local-server\data\data.db')) 'Root validation changed target data'
Pass 'redirected application root rejected before detach'

$app = Make-Install 'runtime-escape'
$runtime = Join-Path $app 'local-server'
[IO.Directory]::Move($runtime, (Join-Path $app 'runtime-original'))
New-Item -ItemType Junction -Path $runtime -Target $outside | Out-Null
$rejected = $false
try { Assert-HugAgentOSCleanupRoot $app } catch { $rejected=$true }
Check $rejected 'Redirected runtime accepted'
Check ([IO.File]::ReadAllText((Join-Path $outside 'do-not-delete.txt')) -eq 'external-sentinel') 'Runtime validation changed external data'
Pass 'redirected local-server rejected before data access'

$app = Make-Install 'detached-escape'
$rejected = $false
try { Invoke-HugAgentOSCleanup -AppRoot $app -DetachedRuntime $outside -DeleteUserData $false } catch { $rejected=$true }
Check $rejected 'Outside detached path accepted'
Check ([IO.File]::ReadAllText((Join-Path $outside 'do-not-delete.txt')) -eq 'external-sentinel') 'Outside path changed'
Pass 'outside detached-runtime path rejected'

$app = Make-Install 'nested-parent-escape'
$parent = Join-Path $fixture 'redirected-parent'
New-Item -ItemType Junction -Path $parent -Target ([IO.Path]::GetDirectoryName($app)) | Out-Null
$rejected = $false
try { Assert-HugAgentOSCleanupRoot (Join-Path $parent 'com.hugagent.desktop') } catch { $rejected=$true }
Check $rejected 'Intermediate reparse parent accepted'
Pass 'intermediate reparse ancestor rejected'

$app = Make-Install 'readonly-and-dangling'
$detached = Detach-Runtime $app $true
$file = Join-Path $detached 'readonly.txt'
[IO.File]::WriteAllText($file,'read-only')
[IO.File]::SetAttributes($file,[IO.FileAttributes]::ReadOnly)
$gone = Join-Path $fixture 'empty-target'
[IO.Directory]::CreateDirectory($gone) | Out-Null
New-Item -ItemType Junction -Path (Join-Path $detached 'dangling') -Target $gone | Out-Null
[IO.Directory]::Delete($gone,$false)
Invoke-HugAgentOSCleanup -AppRoot $app -DetachedRuntime $detached -DeleteUserData $false
Check (-not [IO.Directory]::Exists($detached)) 'Read-only or dangling files blocked cleanup'
Pass 'read-only files and dangling junctions removed without following'


$app = Make-Install 'self-cleaning-helper'
$detached = Detach-Runtime $app $true
$selfScript = Join-Path $detached 'hugagent-uninstall-cleanup.ps1'
[IO.File]::Copy($HelperPath,$selfScript)
& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $selfScript -AppRoot $app -DetachedRuntime $detached -DeleteData 0
Check ($LASTEXITCODE -eq 0) 'Standalone cleanup helper failed'
Check (-not [IO.Directory]::Exists($detached)) 'Standalone helper did not delete itself'
Check (Test-Path -LiteralPath (Join-Path $app 'local-server\data\data.db')) 'Standalone helper removed preserved data'
Pass 'standalone helper safely deletes its detached runtime and own script'

$app = Make-Install 'long-paths'
$detached = Detach-Runtime $app $true
$deep = Join-Path $detached (('a' * 100) + '\' + ('b' * 100) + '\' + ('c' * 100))
Check ($deep.Length -gt 260) 'Long-path fixture is too short'
[IO.Directory]::CreateDirectory((Get-HugAgentOSNativePath $deep)) | Out-Null
[IO.File]::WriteAllText((Get-HugAgentOSNativePath (Join-Path $deep 'sentinel.txt')), 'long-path')
Invoke-HugAgentOSCleanup -AppRoot $app -DetachedRuntime $detached -DeleteUserData $false
Check (-not [IO.Directory]::Exists($detached)) 'Long-path runtime cleanup incomplete'
Pass 'runtime files over 260 characters are removed using extended paths'

$app = Make-Install 'restore-failed'
$runtime = Join-Path $app 'local-server'
$held = Join-Path $app 'held-data'
$detached = Join-Path $app 'remove-restore-failed.tmp'
Assert-HugAgentOSCleanupRoot $app
[IO.Directory]::Move((Join-Path $runtime 'data'),$held)
[IO.Directory]::Move($runtime,$detached)
# The NSIS restore-failed branch keeps the held data and only starts runtime cleanup.
Invoke-HugAgentOSCleanup -AppRoot $app -DetachedRuntime $detached -DeleteUserData $false
Check ([IO.File]::ReadAllText((Join-Path $held 'data.db')).StartsWith('sentinel:')) 'Restore failure lost held data'
Check (Test-Path -LiteralPath (Join-Path $app 'skills\local\skill\r1\SKILL.md')) 'Restore failure lost skill'
Pass 'restore-failure cleanup preserves separately held business data and capabilities'

$app = Make-Install 'top-level-junction'
$detached = Detach-Runtime $app $true
# Move the fixture out using a validated direct-child path, then link to external data.
$held = Join-Path $app 'held-detached'
[IO.Directory]::Move($detached,$held)
New-Item -ItemType Junction -Path $detached -Target $outside | Out-Null
Invoke-HugAgentOSCleanup -AppRoot $app -DetachedRuntime $detached -DeleteUserData $false
Check (-not (Test-Path -LiteralPath $detached)) 'Detached top-level junction not removed'
Check ([IO.File]::ReadAllText((Join-Path $outside 'do-not-delete.txt')) -eq 'external-sentinel') 'Detached root followed external target'
Pass 'detached runtime replaced by a junction removes only the link'

$app = Make-Install 'locked-file-retry'
$detached = Detach-Runtime $app $true
$locked = Join-Path $detached 'runtime\python.exe'
$handle = [IO.File]::Open($locked, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
$selfScript = Join-Path $detached 'hugagent-uninstall-cleanup.ps1'
[IO.File]::Copy($HelperPath,$selfScript)
$helper = Start-Process -FilePath powershell.exe -ArgumentList @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$selfScript,'-AppRoot',$app,'-DetachedRuntime',$detached,'-DeleteData','0') -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 7
Check (Test-Path -LiteralPath $locked) 'Locked file was deleted while still open'
$handle.Dispose()
$helper.WaitForExit()
Check ($helper.ExitCode -eq 0) 'Cleanup helper gave up on a transiently locked file'
Check (-not [IO.Directory]::Exists($detached)) 'Locked file blocked the rest of the cleanup'
Check (Test-Path -LiteralPath (Join-Path $app 'local-server\data\data.db')) 'Retry path removed preserved data'
Pass 'transiently locked runtime files are retried instead of aborting the cleanup'

Write-Output "RESULT passed=$passed failed=0"
