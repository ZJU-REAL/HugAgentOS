use super::*;

#[test]
fn hybrid_file_menu_only_has_chat_folder_and_exit() {
    let menu = titlebar_menu_for(true);
    let file = menu.split("data-menu=\"edit\"").next().unwrap();
    assert!(file.contains("data-act=\"open_folder\""));
    assert!(file.contains("data-act=\"new_chat\""));
    assert!(file.contains("data-win=\"quit\""));
    for removed in ["run_mode", "server_config", "local_server"] {
        assert!(!file.contains(removed));
    }
}

#[test]
fn windows_titlebar_has_compact_localized_menus_without_a_context_tab() {
    assert!(TB_MENU.contains("data-act=\"new_window\""));
    assert!(TB_JS.contains("new_window:'新建窗口'"));
    assert!(TB_JS.contains("new_window:'New Window'"));
    assert!(TB_JS.contains("event.shiftKey?'new_window':'new_chat'"));
    let block = titlebar_block(TB_OFFSET_SPA);
    assert!(!block.contains("tb-logo"));
    assert!(!block.contains(brand::LOGIN_LOGO_URL));
    assert!(!block.contains("tb-name"));
    assert!(!block.contains(&format!(">{}<", brand::NAME)));
    for label in [">文件<", ">编辑<", ">视图<", ">帮助<"] {
        assert!(block.contains(label));
    }
    for key in ["file", "edit", "view", "help"] {
        assert!(block.contains(&format!("data-i18n=\"{key}\"")));
    }
    assert!(block.contains("localStorage.getItem('jx_lang')"));
    assert!(block.contains("file:'File',edit:'Edit',view:'View',help:'Help'"));
    for action in [
        "new_chat",
        "run_mode",
        "server_config",
        "local_server",
        "reload",
        "check_update",
        "website",
        "about",
    ] {
        assert!(block.contains(&format!("data-act=\"{action}\"")));
    }
    for edit_action in ["undo", "redo", "cut", "copy", "paste", "selectAll"] {
        assert!(block.contains(&format!("data-edit=\"{edit_action}\"")));
    }
    assert_eq!(TB_MENU.matches("class=\"tb-menuGroup\"").count(), 4);
    assert!(!block.contains("data-nav=\"back\""));
    assert!(!block.contains("data-nav=\"forward\""));
    assert!(!block.contains("history.back()"));
    assert!(!block.contains("history.forward()"));
    assert!(block.contains("aria-haspopup=\"menu\""));
    assert!(block.contains("aria-expanded=\"false\""));
    assert!(block.contains("role=\"menuitem\""));
    assert!(block.contains("ResizeObserver"));
    assert!(block.contains("event.ctrlKey&&!event.altKey&&key==='n'"));
    assert!(block.contains("event.key==='F11'"));
    assert!(block.contains("event.key==='ArrowDown'"));
    assert!(block.contains("inset:0 0 auto 0"));
    assert!(block.contains("background-color:var(--module-rail-bg,var(--color-bg-layout))"));
    assert!(block.contains("class=\"tb-sidebarZone\""));
    assert!(block.contains("class=\"tb-mainChrome\""));
    assert!(!block.contains("class=\"tb-currentTab\""));
    assert!(!block.contains("class=\"tb-tabText\""));
    assert!(!block.contains(".jx-historyItem.active .jx-historyTitle"));
    assert!(!block.contains("resolveTabLabel"));
    assert!(block.contains("body{box-sizing:border-box!important;padding-top:0!important}"));
    assert!(block.contains("padding-top:var(--hugagent-desktop-titlebar-height)"));
    assert!(block.contains(".jx-moduleRail"));
    assert!(block.contains("background-attachment:fixed"));
    assert!(!block.contains("sampleBg("));
    assert!(!block.contains("--hugagent-desktop-main-chrome"));
    assert!(block.contains("font-family:inherit;font-size:12px;line-height:1;"));
    assert!(!block.contains("border-bottom:1px"));
    assert!(block.find("tb-sidebarZone") < block.find("<nav class=\"tb-menu\""));
    assert!(block.contains("data-win=\"minimize\""));
    assert!(block.contains("data-win=\"close\""));
}

#[test]
fn mac_titlebar_is_a_compact_drag_region_without_duplicate_actions() {
    let block = mac_titlebar_block(MAC_OFFSET_SPA);
    assert!(block.contains("hugagent-mac-titlebar"));
    assert!(block.contains("height:28px"));
    assert!(block.contains("background-color:var(--module-rail-bg,var(--color-bg-layout))"));
    assert!(!block.contains("border-bottom"));
    assert!(!block.contains("backdrop-filter"));
    assert!(!block.contains("data-act="));
    assert!(!block.contains("mac-toolButton"));
    assert!(!block.contains("data-win=\"minimize\""));
    assert!(!block.contains("data-win=\"close\""));
    assert!(!block.contains("tb-menuLabel"));
    assert!(block.contains("background-attachment:fixed"));
    // The sidebar supplies the background; only its controls need an inset.
    assert!(!block.contains("linear-gradient(90deg"));
    assert!(block.contains("padding-top:0!important"));
    assert!(block.contains(".jx-moduleRail{width:88px;}"));
    assert!(
        block.contains("width:88px!important;min-width:88px!important;max-width:88px!important")
    );
    assert!(block.contains("dataset.desktopPlatform='macos'"));
    assert!(block.contains("if(event.button!==0||!isDragSurface(event))return"));
    assert!(!block.contains("ResizeObserver"));
}

#[test]
fn injected_shell_styles_carry_no_hardcoded_colors() {
    for css in [
        MAC_OFFSET_SPA,
        MAC_OFFSET_PAGE,
        TB_OFFSET_SPA,
        TB_OFFSET_PAGE,
        SPA_CSS,
    ] {
        assert!(
            !css.contains('#'),
            "注入 SPA 的偏移样式里出现了写死的颜色：{css}"
        );
    }
    let offenders: Vec<&str> = TB_CSS
        .lines()
        .filter(|line| line.contains('#') && !line.starts_with("#hugagent-titlebar"))
        .filter(|line| !line.contains("dark-ok"))
        .collect();
    // 关闭键那行紧跟在 dark-ok 注释之后，单独放行
    let offenders: Vec<&str> = offenders
        .into_iter()
        .filter(|line| !line.contains(".tb-windowButton.close:hover"))
        .collect();
    assert!(
        offenders.is_empty(),
        "标题栏样式里有写死的颜色：{offenders:?}"
    );
}

#[test]
fn desktop_titlebar_offsets_fixed_feedback_overlays() {
    for block in [
        titlebar_block(TB_OFFSET_SPA),
        titlebar_block(TB_OFFSET_PAGE),
        mac_titlebar_block(MAC_OFFSET_SPA),
        mac_titlebar_block(MAC_OFFSET_PAGE),
    ] {
        assert!(block.contains(
            ".ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}"
        ));
        assert!(block.contains(
            ".ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight"
        ));
    }
    assert!(titlebar_block(TB_OFFSET_SPA).contains("--hugagent-desktop-titlebar-height:34px"));
    assert!(mac_titlebar_block(MAC_OFFSET_SPA).contains("--hugagent-desktop-titlebar-height:28px"));
}

#[test]
fn hybrid_only_titlebar_drops_the_run_mode_entry() {
    let normal = titlebar_menu_for(false);
    let hybrid_only = titlebar_menu_for(true);
    assert!(normal.contains("data-act=\"run_mode\""));
    assert!(!hybrid_only.contains("data-act=\"run_mode\""));
    for act in ["new_chat", "open_folder"] {
        assert!(
            hybrid_only.contains(&format!("data-act=\"{act}\"")),
            "仅混合模式菜单丢了 {act}"
        );
    }
}
