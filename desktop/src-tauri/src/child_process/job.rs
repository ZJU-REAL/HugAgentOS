use std::os::windows::io::AsRawHandle;
use std::process::Child;
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

/// Every managed process joins this job. The kernel ends the whole tree when
/// the last handle closes, which includes the shell crashing: no orphaned
/// server or sidecar can survive the desktop process on Windows.
pub struct KillOnCloseJob(HANDLE);
unsafe impl Send for KillOnCloseJob {}
unsafe impl Sync for KillOnCloseJob {}

impl KillOnCloseJob {
    pub fn new() -> Result<Self, String> {
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return Err(format!(
                    "创建进程作业对象失败：{}",
                    std::io::Error::last_os_error()
                ));
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let ok = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if ok == 0 {
                let error = std::io::Error::last_os_error();
                CloseHandle(handle);
                return Err(format!("配置进程作业对象失败：{error}"));
            }
            Ok(Self(handle))
        }
    }

    pub fn terminate(&self) -> Result<(), String> {
        if unsafe { TerminateJobObject(self.0, 1) } == 0 {
            return Err(format!(
                "结束进程作业失败：{}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(())
    }

    pub fn assign(&self, child: &Child) -> Result<(), String> {
        let ok = unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) };
        if ok == 0 {
            return Err(format!(
                "本机服务进程加入作业对象失败：{}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(())
    }
}

impl Drop for KillOnCloseJob {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
