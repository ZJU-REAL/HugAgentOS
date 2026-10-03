//! The spawned service owns its process group/job until explicit stop or drop.
#[cfg(windows)]
use crate::child_process::job::KillOnCloseJob;
use std::process::{Child, ChildStderr, ChildStdout, Command, ExitStatus};

pub(super) struct ManagedProcess {
    child: Child,
    stopped: bool,
    #[cfg(windows)]
    job: KillOnCloseJob,
}

impl ManagedProcess {
    pub fn spawn(command: &mut Command) -> Result<Self, String> {
        #[cfg(windows)]
        let job = KillOnCloseJob::new()?;
        let mut child = command
            .spawn()
            .map_err(|e| format!("启动本机服务失败：{e}"))?;
        #[cfg(windows)]
        if let Err(error) = job.assign(&child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
        // Mutable only on Windows, where assignment failure must reap the child.
        #[cfg(not(windows))]
        let _ = &mut child;
        Ok(Self {
            child,
            stopped: false,
            #[cfg(windows)]
            job,
        })
    }
    pub fn id(&self) -> u32 {
        self.child.id()
    }
    pub fn take_stdout(&mut self) -> Option<ChildStdout> {
        self.child.stdout.take()
    }
    pub fn take_stderr(&mut self) -> Option<ChildStderr> {
        self.child.stderr.take()
    }
    pub fn try_wait(&mut self) -> std::io::Result<Option<ExitStatus>> {
        if self.stopped {
            return self.child.try_wait();
        }
        #[cfg(unix)]
        let exited = super::process::exited_unreaped(&self.child)?;
        #[cfg(windows)]
        let exited = self.child.try_wait()?.is_some();
        if !exited {
            return Ok(None);
        }
        // Health monitoring owns cleanup too, not just explicit shutdown/restart.
        self.stop().map_err(std::io::Error::other)?;
        self.child.try_wait()
    }
    pub fn stop(&mut self) -> Result<(), String> {
        if self.stopped {
            return Ok(());
        }
        #[cfg(unix)]
        super::process::stop_live_process_group(&mut self.child)?;
        #[cfg(windows)]
        self.job.terminate()?;
        self.child
            .wait()
            .map_err(|e| format!("回收本机服务失败：{e}"))?;
        self.stopped = true;
        Ok(())
    }
}
impl Drop for ManagedProcess {
    fn drop(&mut self) {
        if let Err(error) = self.stop() {
            eprintln!("[local-server] {error}");
        }
    }
}
