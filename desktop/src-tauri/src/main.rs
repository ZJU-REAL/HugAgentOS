// Windows 发布构建隐藏控制台窗口。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    hugagent_agent_desktop_lib::run();
}
