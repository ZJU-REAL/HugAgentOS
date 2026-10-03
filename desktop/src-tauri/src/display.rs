use crate::window_labels::is_session_window;
use crate::{prefs, Shared};
use tauri::{webview::PageLoadEvent, Manager};
/// 「视图 → 放大 / 缩小」的缩放档位，与主流浏览器一致。
///
/// 系统 DPI 缩放由 WebView 自己按平台原生处理，壳层不再干预（历史上曾用
/// `--force-device-scale-factor=1` 关掉系统缩放再自行补偿，那会让 150% 等常见档位整体缩水，
/// 已移除）。因此这里的值就是用户自己的放大/缩小意愿，1.0 表示「实际大小」。
pub(crate) const ZOOM_STEPS: [f64; 13] = [
    0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0,
];

/// 默认缩放：跟随系统，不额外放大。
pub(crate) const DEFAULT_ZOOM: f64 = 1.0;
const ZOOM_MIN: f64 = ZOOM_STEPS[0];
const ZOOM_MAX: f64 = ZOOM_STEPS[ZOOM_STEPS.len() - 1];

/// 把外部来源的缩放值归一到合法范围：未设置过或不是有限数都回到「实际大小」。
/// prefs.json 可能被手工改过，归一化只放在这一处，读写两侧都经由它。
pub(crate) fn sanitize_zoom(value: Option<f64>) -> f64 {
    match value {
        Some(zoom) if zoom.is_finite() => zoom.clamp(ZOOM_MIN, ZOOM_MAX),
        _ => DEFAULT_ZOOM,
    }
}

/// 从当前缩放跳到相邻档位：`delta > 0` 放大、`delta < 0` 缩小，到头停在端点。
/// 当前值不在档位表里（历史配置）时先就近归位再走一步。
pub(crate) fn stepped_zoom(current: f64, delta: i32) -> f64 {
    let index = ZOOM_STEPS
        .iter()
        .enumerate()
        .min_by(|(_, a), (_, b)| (*a - current).abs().total_cmp(&(*b - current).abs()))
        .map_or(0, |(index, _)| index as i32);
    let next = (index + delta).clamp(0, ZOOM_STEPS.len() as i32 - 1);
    ZOOM_STEPS[next as usize]
}

/// 主窗口初始尺寸按显示器的逻辑分辨率计算，避免远程会话中 1280×860 逻辑像素
/// 被高 DPI 放大后超出可用桌面。
pub(crate) fn adaptive_window_dimensions(
    physical_width: u32,
    physical_height: u32,
    scale_factor: f64,
) -> (f64, f64, f64, f64) {
    let scale = if scale_factor.is_finite() {
        scale_factor.max(1.0)
    } else {
        1.0
    };
    let logical_width = physical_width as f64 / scale;
    let logical_height = physical_height as f64 / scale;
    let available_width = (logical_width - 32.0).max(760.0);
    let available_height = (logical_height - 48.0).max(520.0);
    let width = (logical_width * 0.86)
        .clamp(960.0, 1280.0)
        .min(available_width);
    let height = (logical_height * 0.86)
        .clamp(640.0, 860.0)
        .min(available_height);
    (width, height, width.min(960.0), height.min(640.0))
}

pub(crate) fn main_window_dimensions(app: &tauri::AppHandle) -> (f64, f64, f64, f64) {
    app.primary_monitor()
        .ok()
        .flatten()
        .map(|monitor| {
            let size = monitor.size();
            adaptive_window_dimensions(size.width, size.height, monitor.scale_factor())
        })
        .unwrap_or((1280.0, 860.0, 960.0, 640.0))
}

/// 当前生效的缩放档位。真值是 prefs.json，`Shared` 里存一份运行时副本：
/// 页面每次加载完成、以及 Ctrl+滚轮连续缩放时都要取它，不该每次都去读盘。
pub(crate) fn current_zoom(app: &tauri::AppHandle) -> f64 {
    f64::from_bits(
        app.state::<Shared>()
            .ui_zoom
            .load(std::sync::atomic::Ordering::Relaxed),
    )
}

/// 导航 / 刷新会让 WebView2 丢掉页面缩放，加载完成后按用户档位重新施加。
/// 主窗口和快速问答窗都会被 `navigate_session_windows` 整页导航，两边都要挂。
pub(crate) fn restore_zoom_on_load(
    window: tauri::WebviewWindow,
    payload: tauri::webview::PageLoadPayload<'_>,
) {
    if matches!(payload.event(), PageLoadEvent::Finished) {
        apply_user_zoom(&window);
    }
}

/// 施加用户在「视图」菜单里选择的缩放档位。系统 DPI 由 WebView 原生处理，这里不参与，
/// 所以未设置过时就是 1.0（= 完全跟随系统显示设置）。
pub(crate) fn apply_user_zoom(window: &tauri::WebviewWindow) {
    let _ = window.set_zoom(current_zoom(window.app_handle()));
}

/// 改变缩放档位并落盘，让所有承载内容的窗口与下次启动保持一致。
/// `delta` 为 0 表示回到「实际大小」。
pub(crate) fn adjust_user_zoom(app: &tauri::AppHandle, delta: i32) {
    let zoom = if delta == 0 {
        DEFAULT_ZOOM
    } else {
        stepped_zoom(current_zoom(app), delta)
    };
    let config_dir = {
        let shared = app.state::<Shared>();
        shared
            .ui_zoom
            .store(zoom.to_bits(), std::sync::atomic::Ordering::Relaxed);
        shared.config_dir.clone()
    };
    // 落盘交给阻塞线程池：Ctrl+滚轮会连续触发，主线程不该等磁盘。
    tauri::async_runtime::spawn_blocking(move || prefs::save_ui_zoom(&config_dir, zoom));
    for window in app.webview_windows().values() {
        if is_session_window(window.label()) {
            let _ = window.set_zoom(zoom);
        }
    }
}

#[cfg(test)]
mod display_tests {
    use super::*;

    #[test]
    fn zoom_steps_walk_the_preset_ladder() {
        assert_eq!(stepped_zoom(1.0, 1), 1.1);
        assert_eq!(stepped_zoom(1.1, 1), 1.25);
        assert_eq!(stepped_zoom(1.0, -1), 0.9);
        assert_eq!(stepped_zoom(0.9, -1), 0.8);
    }

    #[test]
    fn zoom_steps_stop_at_both_ends() {
        assert_eq!(stepped_zoom(3.0, 1), 3.0);
        assert_eq!(stepped_zoom(0.5, -1), 0.5);
    }

    /// 历史 prefs.json 里可能留着不在档位表上的值（旧版本按 DPI 算出来的补偿值），
    /// 放大 / 缩小要能从那里就近接上，而不是卡住不动。
    #[test]
    fn off_ladder_zoom_snaps_to_the_nearest_step() {
        assert_eq!(stepped_zoom(1.45, 1), 1.75);
        assert_eq!(stepped_zoom(1.45, -1), 1.25);
    }

    /// prefs.json 是可以被手工改坏的，归一化统一挡在读取这一层。
    #[test]
    fn sanitize_zoom_falls_back_and_clamps() {
        assert_eq!(sanitize_zoom(None), 1.0);
        assert_eq!(sanitize_zoom(Some(f64::NAN)), 1.0);
        assert_eq!(sanitize_zoom(Some(f64::INFINITY)), 1.0);
        assert_eq!(sanitize_zoom(Some(-4.0)), 0.5);
        assert_eq!(sanitize_zoom(Some(99.0)), 3.0);
        assert_eq!(sanitize_zoom(Some(1.25)), 1.25);
    }

    #[test]
    fn session_windows_cover_main_copies_and_quickask() {
        for label in ["main", "main-3", "quickask"] {
            assert!(is_session_window(label), "{label}");
        }
        for label in ["close-confirm", "update-progress", "server-config"] {
            assert!(!is_session_window(label), "{label}");
        }
    }

    #[test]
    fn remote_window_stays_inside_logical_desktop() {
        assert_eq!(
            adaptive_window_dimensions(1920, 1080, 2.0),
            (928.0, 520.0, 928.0, 520.0)
        );
        assert_eq!(
            adaptive_window_dimensions(3840, 2160, 2.0),
            (1280.0, 860.0, 960.0, 640.0)
        );
    }
}
