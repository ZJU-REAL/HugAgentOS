//! Platform window chrome, shared frontend surfaces, menus and safe areas.
use super::brand;

// ── 一体化桌面标题栏 ───────────────────────────────────────────────────────
//
// 主窗口关闭系统 decorations，避免「系统标题栏 + 原生菜单栏」占两行。Windows/Linux
// 保留一行紧凑菜单和窗口控制，整条背景延续最左侧模块导航底色，不重复品牌 Logo。
// 菜单靠左排列，侧边栏自己的品牌区从标题栏下方开始。
// 中间空白仍承担窗口拖动。壳动作走导航哨兵，由
// lib.rs 拦截执行，不依赖远程源下不稳定的 Tauri IPC。

const TITLEBAR_HEIGHT: u8 = 34;
const TB_OFFSET_SPA: &str =
    ":root{--hugagent-desktop-titlebar-height:34px;--hugagent-desktop-sidebar-width:344px}body{box-sizing:border-box!important;padding-top:0!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";
const TB_OFFSET_PAGE: &str =
    ":root{--hugagent-desktop-titlebar-height:34px;--hugagent-desktop-sidebar-width:280px}body{box-sizing:border-box!important;padding-top:34px!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";

// Tao's traffic-light y inset controls the native titlebar container height,
// not the button's top edge. The 21px inset keeps the controls inside this
// 28px drag region. The workspace begins below that region.
const MAC_TITLEBAR_HEIGHT: u8 = 28;
// Reserve one shared top region so native controls and module navigation never overlap.
const MAC_OFFSET_SPA: &str =
    ":root{--hugagent-desktop-titlebar-height:28px}body{box-sizing:border-box!important;padding-top:0!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";
const MAC_OFFSET_PAGE: &str =
    ":root{--hugagent-desktop-titlebar-height:28px}body{box-sizing:border-box!important;padding-top:28px!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";

// 这条标题栏是**注进 SPA 自己那份文档**的（见 inject_after_body），所以 `<html>` 上的
// data-theme 对它同样生效，直接引用应用令牌即可两档自动跟随 —— 不需要再写一套深色覆盖，
// 也不需要 prefers-color-scheme（那会和手动 light/dark/system 三档打架）。
const TB_CSS: &str = include_str!("../../shared/chrome/menu.css");

const TB_MENU: &str = include_str!("../../shared/chrome/menu.html");

const TB_CONTROLS: &str = include_str!("../../shared/chrome/controls.html");

const TB_JS: &str = include_str!("../../shared/chrome/menu.js");

// macOS keeps application actions in the native system menu. Inside the window
// we only reserve a compact draggable title region for the traffic lights; a
// second branded toolbar would duplicate the native chrome and waste space.
const MAC_TB_CSS: &str = include_str!("../../shared/chrome/mac.css");

const MAC_TB_JS: &str = include_str!("../../shared/chrome/mac.js");

/// 仅交付混合模式的包没有别的运行形态可切，菜单里不摆一个点了也没意义的入口。
fn titlebar_menu_for(hybrid_only: bool) -> String {
    if !hybrid_only {
        return TB_MENU.to_string();
    }
    TB_MENU
        .lines()
        .filter(|line| {
            !["run_mode", "server_config", "local_server"]
                .iter()
                .any(|action| line.contains(&format!("data-act=\"{action}\"")))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn titlebar_block(offset_css: &str) -> String {
    format!(
        "<style id=\"hugagent-titlebar-style\">{css}{offset}</style>\
<header id=\"hugagent-titlebar\" data-height=\"{height}\">\
<div class=\"tb-sidebarZone\">{menu}</div><div class=\"tb-mainChrome\">\
<div class=\"tb-spacer\"></div>{controls}</div></header><script>{script}</script>",
        css = format!(
            "{TB_CSS}{}",
            if offset_css == TB_OFFSET_SPA {
                SPA_CSS
            } else {
                ""
            }
        ),
        offset = offset_css,
        height = TITLEBAR_HEIGHT,
        menu = titlebar_menu_for(brand::HYBRID_ONLY),
        controls = TB_CONTROLS,
        script = TB_JS,
    )
}

fn mac_titlebar_block(offset_css: &str) -> String {
    format!(
        "<style id=\"hugagent-titlebar-style\">{css}{offset}</style>\
<header id=\"hugagent-mac-titlebar\" data-height=\"{height}\" aria-hidden=\"true\"></header><script>{script}</script>",
        css = format!("{MAC_TB_CSS}{}", if offset_css == MAC_OFFSET_SPA { SPA_CSS } else { "" }),
        offset = offset_css,
        height = MAC_TITLEBAR_HEIGHT,
        script = MAC_TB_JS,
    )
}

pub(super) fn platform_titlebar_block(spa: bool) -> String {
    if cfg!(target_os = "macos") {
        mac_titlebar_block(if spa { MAC_OFFSET_SPA } else { MAC_OFFSET_PAGE })
    } else {
        titlebar_block(if spa { TB_OFFSET_SPA } else { TB_OFFSET_PAGE })
    }
}

const SPA_CSS: &str = include_str!("../../shared/chrome/workspace.css");

#[cfg(test)]
#[path = "window_chrome_tests.rs"]
mod tests;
