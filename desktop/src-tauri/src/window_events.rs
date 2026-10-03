//! Native window navigation policy is installed by the application composition root.
//! Window construction does not depend on the command/session orchestration layer.
use tauri::Manager;
pub(crate) struct WindowNavigation(pub fn(&tauri::AppHandle, &str, &url::Url) -> bool);

pub(crate) fn navigate(app: &tauri::AppHandle, label: &str, url: &url::Url) -> bool {
    // Fail closed if a window is accidentally built before policy installation.
    app.try_state::<WindowNavigation>()
        .is_some_and(|handler| (handler.0)(app, label, url))
}
