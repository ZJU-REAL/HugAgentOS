//! Window roles are shared policy, independent of window creation and display.
pub(crate) fn is_desktop_window(label: &str) -> bool {
    label == "main"
        || label
            .strip_prefix("main-")
            .is_some_and(|suffix| !suffix.is_empty() && suffix.bytes().all(|c| c.is_ascii_digit()))
}

/// 承载应用内容、需要跟随会话导航与用户缩放的窗口：主窗口及其副本 + 快速问答窗。
/// 确认弹窗、更新进度这类固定尺寸小窗不算在内。
pub(crate) fn is_session_window(label: &str) -> bool {
    is_desktop_window(label) || label == "quickask"
}

/// 构建系统托盘：左键单击恢复主窗口；右键菜单「显示主窗口 / 退出」。
/// 配合「关闭即最小化到托盘」，让应用关窗后仍在后台运行（自动化任务等）。
#[cfg(test)]
mod multiwindow_tests {
    #[test]
    fn regular_window_labels_exclude_dialogs_and_quick_ask() {
        for label in ["main", "main-1", "main-42"] {
            assert!(super::is_desktop_window(label), "{label}");
        }
        for label in ["quickask", "close-confirm", "main-", "main-dialog", "other"] {
            assert!(!super::is_desktop_window(label), "{label}");
        }
    }
}
