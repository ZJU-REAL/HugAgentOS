//! 子进程的平台化启动参数。
//!
//! Windows 上壳子以 GUI 子系统运行、自身没有控制台，拉起控制台程序
//! （python.exe / powershell.exe / taskkill.exe）时系统会为它新开一个控制台
//! 窗口——用户看到的就是黑色 cmd 框。所有子进程都要经这里加 CREATE_NO_WINDOW。

use std::process::Command;

#[cfg(target_os = "windows")]
pub(crate) fn hide_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(target_os = "windows"))]
pub(crate) fn hide_console(_command: &mut Command) {}

#[cfg(target_os = "windows")]
pub(crate) mod job;

/// Drain diagnostic output into a bounded tail and kill the owned tree on exit.
pub(crate) fn run_diagnostic(
    command: &mut Command,
    cancelled: &std::sync::atomic::AtomicBool,
    timeout: std::time::Duration,
) -> Result<(), String> {
    use std::io::Read;
    use std::process::Stdio;
    use std::sync::atomic::Ordering;
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    hide_console(command);
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    #[cfg(target_os = "windows")]
    let job = job::KillOnCloseJob::new()?;
    let mut child = command.spawn().map_err(|e| e.to_string())?;
    #[cfg(target_os = "windows")]
    if let Err(error) = job.assign(&child) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    let mut stderr = child.stderr.take().ok_or("缺少自检输出管道")?;
    let reader = std::thread::spawn(move || {
        let mut tail = std::collections::VecDeque::with_capacity(8192);
        let mut chunk = [0u8; 4096];
        while let Ok(count) = stderr.read(&mut chunk) {
            if count == 0 {
                break;
            }
            for byte in &chunk[..count] {
                if tail.len() == 8192 {
                    tail.pop_front();
                }
                tail.push_back(*byte);
            }
        }
        tail.into_iter().collect::<Vec<u8>>()
    });
    let started = std::time::Instant::now();
    let outcome = loop {
        if cancelled.load(Ordering::SeqCst) {
            break Err("自检已取消".to_string());
        }
        if started.elapsed() >= timeout {
            break Err("运行环境自检超时".to_string());
        }
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(50)),
            Err(error) => break Err(error.to_string()),
        }
    };
    #[cfg(unix)]
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    #[cfg(target_os = "windows")]
    drop(job);
    let _ = child.kill();
    let _ = child.wait();
    let tail = reader.join().unwrap_or_default();
    if outcome?.success() {
        Ok(())
    } else {
        Err(format!(
            "离线 Python 自检失败：{}",
            String::from_utf8_lossy(&tail).trim()
        ))
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn diagnostic_deadline_and_output_limit() {
        let cancelled = std::sync::atomic::AtomicBool::new(false);
        let start = std::time::Instant::now();
        let result = run_diagnostic(
            Command::new("python3").args(["-c", "import time; time.sleep(60)"]),
            &cancelled,
            std::time::Duration::from_millis(80),
        );
        assert!(result.unwrap_err().contains("超时"));
        assert!(start.elapsed() < std::time::Duration::from_secs(3));
        let result = run_diagnostic(
            Command::new("python3").args([
                "-c",
                "import sys; sys.stderr.write('x'*1000000); sys.exit(1)",
            ]),
            &cancelled,
            std::time::Duration::from_secs(3),
        );
        assert!(result.unwrap_err().len() < 8300);
    }
}

#[cfg(all(test, target_os = "windows"))]
mod windows_tests {
    use super::*;
    #[test]
    fn diagnostic_job_deadline_and_output_limit() {
        let cancelled = std::sync::atomic::AtomicBool::new(false);
        let result = run_diagnostic(
            Command::new("powershell.exe").args([
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Start-Sleep -Seconds 60",
            ]),
            &cancelled,
            std::time::Duration::from_millis(300),
        );
        assert!(result.unwrap_err().contains("超时"));
        let result = run_diagnostic(
            Command::new("powershell.exe").args([
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::Error.Write(('x' * 1000000)); exit 1",
            ]),
            &cancelled,
            std::time::Duration::from_secs(15),
        );
        assert!(result.unwrap_err().len() < 8300);
    }
}
