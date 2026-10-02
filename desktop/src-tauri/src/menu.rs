//! 顶部菜单：macOS 用系统菜单栏（`build`），Windows/Linux 用 `proxy.rs` 注入的窗口内
//! 标题栏菜单。动作由 actions 模块统一执行，本模块只构建菜单。
//!
//! 菜单动作由 Rust 侧处理、**不经 WebView IPC**——反代这种远程源下自定义命令会被
//! Tauri 的 ACL 拒绝，所以壳层能力（新建对话 / 设置服务器 / 检查更新）都不依赖 IPC。
//! 编辑、全屏等用系统预定义项（`PredefinedMenuItem`），撤销/复制/粘贴由系统直接作用于
//! 焦点输入框，无需自己接线。

// 菜单构建相关的类型只有 macOS 的 `build` 用得到；其它平台只走下面的动作分发。
#[cfg(target_os = "macos")]
use tauri::menu::{AboutMetadataBuilder, Menu, MenuItem, SubmenuBuilder};
#[cfg(target_os = "macos")]
use tauri::Runtime;
#[cfg(target_os = "macos")]
use tauri::{AppHandle, Manager};

#[cfg(target_os = "macos")]
use crate::brand;
#[cfg(target_os = "macos")]
use crate::Shared;

/// macOS 专用的系统应用菜单。Windows/Linux 不挂原生菜单——那两个平台显示的是
/// `proxy.rs` 注入的窗口内标题栏菜单（`TB_MENU`），菜单项只在那边定义一份。
#[cfg(target_os = "macos")]
pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let config = crate::config::load(&app.state::<Shared>().config_dir);
    let hybrid =
        brand::HYBRID_ONLY || config.provision_mode() == crate::config::ProvisionMode::Dual;
    let local_capable = config.provision_mode() != crate::config::ProvisionMode::CloudOnly;
    let about = AboutMetadataBuilder::new()
        .name(Some(brand::NAME.to_string()))
        .version(Some(app.package_info().version.to_string()))
        .build();
    let application = SubmenuBuilder::new(app, brand::NAME)
        .about(Some(about))
        .separator()
        .text("server_config", "设置…")
        .text("check_update", "检查更新…")
        .separator()
        .services()
        .separator()
        .hide()
        .hide_others()
        .show_all()
        .separator()
        .quit()
        .build()?;

    let mut file = SubmenuBuilder::new(app, "文件")
        .item(&MenuItem::with_id(
            app,
            "new_window",
            "新建窗口",
            true,
            Some("CmdOrCtrl+Shift+N"),
        )?)
        .text("new_chat", "新建对话");
    // 仅交付混合模式的包没有别的形态可切，不摆一个点了也没意义的入口。
    if !hybrid {
        file = file.text("run_mode", "运行模式…");
    }
    if local_capable {
        file = file.text("open_folder", "打开文件夹…");
    }
    if !hybrid {
        file = file.text("local_server", "本机服务…");
    }
    let file = file.separator().quit().build()?;

    let edit = SubmenuBuilder::new(app, "编辑")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .build()?;

    let view = SubmenuBuilder::new(app, "显示")
        .text("reload", "重新加载")
        .separator()
        .item(&MenuItem::with_id(
            app,
            "zoom_in",
            "放大",
            true,
            Some("CmdOrCtrl+Plus"),
        )?)
        .item(&MenuItem::with_id(
            app,
            "zoom_out",
            "缩小",
            true,
            Some("CmdOrCtrl+-"),
        )?)
        .item(&MenuItem::with_id(
            app,
            "zoom_reset",
            "实际大小",
            true,
            Some("CmdOrCtrl+0"),
        )?)
        .separator()
        .fullscreen()
        .build()?;

    let window = SubmenuBuilder::new(app, "窗口")
        .minimize()
        .maximize()
        .build()?;

    let help = SubmenuBuilder::new(app, "帮助")
        .text("website", "访问官网")
        .build()?;

    Menu::with_items(app, &[&application, &file, &edit, &view, &window, &help])
}
