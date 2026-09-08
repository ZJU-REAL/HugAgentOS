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
