use super::LOCAL_SERVER_PORT;
#[cfg(target_os = "windows")]
use crate::child_process::hide_console;
use std::path::Path;
#[cfg(target_os = "linux")]
use std::path::PathBuf;
#[cfg(unix)]
use std::process::Child;
use std::process::Command;
#[cfg(unix)]
use std::time::Duration;
#[cfg(unix)]
pub(super) fn configure_process_group(command: &mut Command) {
    use std::os::unix::process::CommandExt;
    command.process_group(0);
}

#[cfg(not(unix))]
pub(super) fn configure_process_group(_command: &mut Command) {}

#[cfg(unix)]
pub(super) fn signal_process_group(pid: u32, signal: i32) -> Result<(), String> {
    let result = unsafe { libc::kill(-(pid as i32), signal) };
    if result == 0 {
        return Ok(());
    }
    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(())
    } else {
        Err(format!("发送进程组信号失败：{error}"))
    }
}

/// Observe exit without reaping: the leader PID must remain reserved until its
/// group has been signalled, otherwise a recycled PID could identify another job.
#[cfg(unix)]
pub(super) fn exited_unreaped(child: &Child) -> std::io::Result<bool> {
    let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
    let result = unsafe {
        libc::waitid(
            libc::P_PID,
            child.id() as libc::id_t,
            &mut info,
            libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
        )
    };
    if result != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(unsafe { info.si_pid() } != 0)
}

#[cfg(unix)]
pub(super) fn stop_live_process_group(child: &mut Child) -> Result<(), String> {
    signal_process_group(child.id(), libc::SIGTERM)?;
    for _ in 0..30 {
        if exited_unreaped(child).map_err(|error| format!("等待本机服务退出失败：{error}"))?
        {
            // The leader exiting does not imply its children exited. Sidecars
            // may ignore TERM and retain the group after Python exits. Keep its zombie reserved until group cleanup.
            return signal_process_group(child.id(), libc::SIGKILL);
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    signal_process_group(child.id(), libc::SIGKILL)
}

#[cfg(target_os = "windows")]
pub(super) fn stop_recorded_server(
    pid_path: &Path,
    _expected_executable: &Path,
    install_root: &Path,
) -> Result<(), String> {
    let Ok(raw_pid) = std::fs::read_to_string(pid_path) else {
        return Ok(());
    };
    let pid = raw_pid
        .trim()
        .parse::<u32>()
        .map_err(|_| "本机服务 PID 文件已损坏".to_string())?;
    // PID 文件可能来自上次异常退出；若该 PID 已被别的程序复用，只清理陈旧
    // 记录，不结束无关进程，也不阻断本次重新安装/启动。
    stop_recorded_process_tree(pid, install_root)
}

#[cfg(target_os = "windows")]
pub(super) fn stop_process_tree(pid: u32) -> Result<(), String> {
    let mut command = Command::new("taskkill.exe");
    command.args(["/PID", &pid.to_string(), "/T", "/F"]);
    hide_console(&mut command);
    let status = command
        .status()
        .map_err(|error| format!("无法回收本机服务进程树：{error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!(
            "回收本机服务进程树失败（退出码 {:?}）",
            status.code()
        ))
    }
}

#[cfg(target_os = "windows")]
pub(super) fn stop_recorded_process_tree(pid: u32, install_root: &Path) -> Result<(), String> {
    let script = format!(
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; \
         if (-not $p) {{ exit 0 }}; \
         $root=[IO.Path]::GetFullPath($env:HUGAGENT_INSTALL_ROOT).TrimEnd('\\'); \
         if (-not $p.ExecutablePath -or -not [IO.Path]::GetFullPath($p.ExecutablePath).StartsWith($root + '\\',[StringComparison]::OrdinalIgnoreCase)) {{ exit 3 }}; \
         & taskkill.exe /PID {pid} /T /F | Out-Null; exit $LASTEXITCODE"
    );
    let mut command = Command::new("powershell.exe");
    command
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            &script,
        ])
        .env("HUGAGENT_INSTALL_ROOT", install_root);
    hide_console(&mut command);
    let status = command
        .status()
        .map_err(|error| format!("无法回收上次本机服务进程：{error}"))?;
    match status.code() {
        Some(0) => Ok(()),
        Some(3) => Ok(()),
        code => Err(format!("回收上次本机服务进程失败（退出码 {code:?}）")),
    }
}

#[cfg(target_os = "windows")]
pub(super) fn windows_local_server_pids(
    install_root: &Path,
    required_source: Option<&Path>,
) -> Result<Vec<u32>, String> {
    let script = "$ErrorActionPreference='Stop'; \
        $root=[IO.Path]::GetFullPath($env:HUGAGENT_INSTALL_ROOT).TrimEnd('\\'); \
        $source=[string]$env:HUGAGENT_SOURCE_DIR; \
        $port='--port ' + $env:HUGAGENT_LOCAL_PORT; \
        Get-CimInstance Win32_Process | ForEach-Object { \
          $cmd=[string]$_.CommandLine; \
          $exe=[string]$_.ExecutablePath; \
          if ($cmd -and $exe -and \
              [IO.Path]::GetFullPath($exe).StartsWith($root + '\\',[StringComparison]::OrdinalIgnoreCase) -and \
              $cmd.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and \
              $cmd.IndexOf('cli.py',[StringComparison]::OrdinalIgnoreCase) -ge 0 -and \
              $cmd.IndexOf(' serve',[StringComparison]::OrdinalIgnoreCase) -ge 0 -and \
              $cmd.IndexOf($port,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and \
              (-not $source -or $cmd.IndexOf($source,[StringComparison]::OrdinalIgnoreCase) -ge 0)) { \
            Write-Output $_.ProcessId \
          } \
        }";
    let mut command = Command::new("powershell.exe");
    command
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ])
        .env("HUGAGENT_INSTALL_ROOT", install_root)
        .env(
            "HUGAGENT_SOURCE_DIR",
            required_source.map(Path::as_os_str).unwrap_or_default(),
        )
        .env("HUGAGENT_LOCAL_PORT", LOCAL_SERVER_PORT.to_string());
    hide_console(&mut command);
    let output = command
        .output()
        .map_err(|error| format!("无法检查已运行的本机服务：{error}"))?;
    if !output.status.success() {
        return Err(format!(
            "检查已运行的本机服务失败（退出码 {:?}）",
            output.status.code()
        ));
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| {
            line.trim()
                .parse::<u32>()
                .map_err(|_| format!("无法解析本机服务进程号：{line}"))
        })
        .collect()
}

#[cfg(target_os = "macos")]
pub(super) fn stop_recorded_server(
    pid_path: &Path,
    _expected_executable: &Path,
    install_root: &Path,
) -> Result<(), String> {
    let Ok(raw_pid) = std::fs::read_to_string(pid_path) else {
        return Ok(());
    };
    let Ok(pid) = raw_pid.trim().parse::<u32>() else {
        return Ok(());
    };
    let output = Command::new("/bin/ps")
        .args(["-p", &pid.to_string(), "-o", "command="])
        .output()
        .map_err(|error| format!("无法检查上次本机服务进程：{error}"))?;
    if !output.status.success() {
        return Ok(());
    }
    let command_line = String::from_utf8_lossy(&output.stdout);
    if !mac_server_command_matches(&command_line, install_root) {
        return Ok(());
    }

    let pid_text = pid.to_string();
    let _ = signal_process_group(pid, libc::SIGTERM);
    for _ in 0..20 {
        if !mac_process_exists(&pid_text) {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let _ = signal_process_group(pid, libc::SIGKILL);
    if mac_process_exists(&pid_text) {
        return Err("无法结束上次遗留的本机服务进程".to_string());
    }
    Ok(())
}

#[cfg(target_os = "macos")]
pub(super) fn mac_process_exists(pid: &str) -> bool {
    Command::new("/bin/kill")
        .args(["-0", pid])
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

#[cfg(any(target_os = "macos", test))]
pub(super) fn mac_server_command_matches(command_line: &str, install_root: &Path) -> bool {
    let root = install_root.to_string_lossy();
    command_line.contains(root.as_ref())
        && command_line.contains("cli.py")
        && command_line.contains(" serve")
        && command_line.contains(&format!("--port {LOCAL_SERVER_PORT}"))
}

#[cfg(target_os = "linux")]
pub(super) fn stop_recorded_server(
    pid_path: &Path,
    _expected_executable: &Path,
    install_root: &Path,
) -> Result<(), String> {
    let Ok(raw_pid) = std::fs::read_to_string(pid_path) else {
        return Ok(());
    };
    let Ok(pid) = raw_pid.trim().parse::<u32>() else {
        return Ok(());
    };
    let proc_root = PathBuf::from(format!("/proc/{pid}"));
    if !proc_root.exists() {
        return Ok(());
    }
    let executable = match std::fs::read_link(proc_root.join("exe")) {
        Ok(path) => path,
        Err(_) => return Ok(()),
    };
    let command_line = std::fs::read(proc_root.join("cmdline"))
        .map(|bytes| String::from_utf8_lossy(&bytes).replace('\0', " "))
        .unwrap_or_default();
    if !executable.starts_with(install_root)
        || !linux_server_command_matches(&command_line, install_root)
    {
        return Ok(());
    }
    signal_process_group(pid, libc::SIGTERM)?;
    for _ in 0..20 {
        if !proc_root.exists() {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    signal_process_group(pid, libc::SIGKILL)?;
    Ok(())
}

#[cfg(any(target_os = "linux", test))]
pub(super) fn linux_server_command_matches(command_line: &str, install_root: &Path) -> bool {
    let root = install_root.to_string_lossy();
    command_line.contains(root.as_ref())
        && command_line.contains("cli.py")
        && command_line.contains(" serve")
        && command_line.contains(&format!("--port {LOCAL_SERVER_PORT}"))
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    #[test]
    fn shutdown_reaps_descendants_when_leader_exits_first() {
        let dir = tempfile::tempdir().unwrap();
        let marker = dir.path().join("child.pid");
        let script = r#"import subprocess,sys,time; code='import signal,os,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open(sys.argv[1],"w").write(str(os.getpid())); time.sleep(60)'; subprocess.Popen([sys.executable,'-c',code,sys.argv[1]]); time.sleep(60)"#;
        let mut command = Command::new("python3");
        command.args(["-c", script, marker.to_str().unwrap()]);
        configure_process_group(&mut command);
        let mut leader = command.spawn().unwrap();
        let group = leader.id();
        for _ in 0..100 {
            if marker.exists() {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let pid = std::fs::read_to_string(&marker)
            .expect("descendant ready")
            .parse::<u32>()
            .unwrap();
        let result = stop_live_process_group(&mut leader);
        let alive = (0..30).all(|_| {
            let running = std::fs::read_to_string(format!("/proc/{pid}/stat"))
                .ok()
                .and_then(|s| s.rsplit_once(") ").map(|(_, rest)| !rest.starts_with('Z')))
                .unwrap_or(false);
            if running {
                std::thread::sleep(Duration::from_millis(10));
            }
            running
        });
        // Always clean this test's process group, including on a regression.
        let _ = signal_process_group(group, libc::SIGKILL);
        let _ = leader.wait();
        assert!(result.is_ok());
        assert!(
            !alive,
            "TERM-resistant descendant survived after its leader exited"
        );
    }
}
