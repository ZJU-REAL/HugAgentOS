use crate::windows::show_main_window;
use crate::{actions, brand, prefs, Shared};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::Manager;
pub(crate) fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let show_i = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
    let new_chat_i = MenuItem::with_id(app, "tray_new_chat", "新建对话", true, None::<&str>)?;
    let update_i = MenuItem::with_id(app, "tray_check_update", "检查更新…", true, None::<&str>)?;
    let ask_i = MenuItem::with_id(app, "ask_close", "关闭时重新询问", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_i, &new_chat_i, &update_i, &ask_i, &quit_i])?;

    let mut builder = TrayIconBuilder::new()
        .tooltip(brand::NAME)
        .menu(&menu)
        .show_menu_on_left_click(false)
        // 托盘项用 `tray_*` 前缀 id，避开主菜单全局事件处理器（actions::handle）的动作 id，
        // 防止同一事件被托盘 + 全局两处重复触发；这里再映射回统一的 actions::dispatch。
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main_window(app),
            "tray_new_chat" => actions::dispatch(app, "new_chat"),
            "tray_check_update" => actions::dispatch(app, "check_update"),
            // 清除记住的关闭行为 → 下次点关闭又会弹「最小化 / 退出」框。
            "ask_close" => {
                let dir = app.state::<Shared>().config_dir.clone();
                prefs::clear_close_action(&dir);
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}
