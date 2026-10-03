use super::*;

fn headers(pairs: &[(&str, &str)]) -> HeaderMap {
    let mut map = HeaderMap::new();
    for (name, value) in pairs {
        map.insert(
            header::HeaderName::from_bytes(name.as_bytes()).unwrap(),
            value.parse().unwrap(),
        );
    }
    map
}

#[test]
fn only_this_windows_own_pages_may_use_the_injected_credentials() {
    // 本页面发起的请求：两种标注任取其一都认。
    assert!(is_same_origin(
        &headers(&[("origin", "http://127.0.0.1:5173")]),
        5173
    ));
    assert!(is_same_origin(
        &headers(&[("origin", "http://localhost:5173")]),
        5173
    ));
    assert!(is_same_origin(
        &headers(&[("sec-fetch-site", "same-origin")]),
        5173
    ));
    // 用户直接导航（壳的跳转哨兵走的就是这条）。
    assert!(is_same_origin(
        &headers(&[("sec-fetch-site", "none")]),
        5173
    ));

    // 网页跨站打过来。
    assert!(!is_same_origin(
        &headers(&[("origin", "https://evil.example")]),
        5173
    ));
    // 端口对不上——另一个实例的页面也不行。
    assert!(!is_same_origin(
        &headers(&[("origin", "http://127.0.0.1:5174")]),
        5173
    ));
    assert!(!is_same_origin(
        &headers(&[("sec-fetch-site", "cross-site")]),
        5173
    ));
    // 同机的其它程序（curl 之类）两种标注都给不出。
    assert!(!is_same_origin(&headers(&[]), 5173));
}

/// 每张壳页面都必须成对具备：`<head>` 里的主题引导脚本 + `:root[data-theme="dark"]` 覆盖块。
/// 少了脚本 → 深色偏好读不到，页面恒亮；少了覆盖块 → 属性打上了也没有对应样式。
#[test]
fn every_shell_page_boots_and_defines_both_themes() {
    for (name, html) in [
        ("login", LOGIN_HTML),
        ("init", INIT_HTML),
        ("init-fixed", INIT_FIXED_HTML),
        ("setup", SETUP_HTML),
        ("close-confirm", CLOSE_CONFIRM_HTML),
        ("server-config", SERVER_CONFIG_HTML),
    ] {
        assert!(
            html.contains(":root[data-theme=\"dark\"]"),
            "{name} 页缺少深色覆盖块"
        );
        let booted = with_theme_boot(html);
        assert!(
            booted.contains(brand::THEME_STORAGE_KEY),
            "{name} 页没注入主题引导"
        );
        // 引导必须早于 <style>，否则深色用户会先闪一帧白底
        assert!(
            booted.find(brand::THEME_STORAGE_KEY) < booted.find("<style>"),
            "{name} 页的主题引导排在样式之后，会闪白"
        );
        // 壳页面不许用媒体查询判深浅：那只认系统外观，会和手动三档打架
        assert!(
            !html.contains("prefers-color-scheme"),
            "{name} 页用了 prefers-color-scheme，应改用 data-theme"
        );
    }
}

#[test]
fn setup_install_starts_in_place_before_switching_modes() {
    assert!(SETUP_HTML.contains("fetch('/__desktop/setup/install'"));
    assert!(SETUP_HTML.contains("从零开始安装"));
    assert!(!SETUP_HTML.contains("if(!activeLocal){ activateLocal(); return; }"));
}

#[test]
fn setup_dual_mode_never_switches_login_target_to_local() {
    // 双模式云端为主：安装完成后回云端应用，不 activate-local。
    assert!(SETUP_HTML.contains("var dual = __HYBRID_DUAL__;"));
    assert!(SETUP_HTML.contains("if(activeLocal||dual){location.replace('/');}"));
    assert!(SETUP_HTML.contains("本机服务已就绪，正在返回…"));
}

#[test]
fn setup_mac_copy_and_single_primary_action_are_present() {
    assert!(SETUP_HTML.contains("在这台 Mac 上开始使用"));
    assert_eq!(SETUP_HTML.matches("id=\"install\"").count(), 1);
    assert!(!SETUP_HTML.contains("class=\"choices\""));
    assert!(!SETUP_HTML.contains("border-top:1px solid"));
    assert!(SETUP_HTML.contains("transform:scale(.97)"));
    assert!(SETUP_HTML.contains("prefers-reduced-motion:reduce"));
    assert!(SETUP_HTML.contains("prefers-reduced-transparency:reduce"));
    assert!(SETUP_HTML.contains("prefers-contrast:more"));
}

#[test]
fn init_page_offers_three_modes_and_conditional_cloud_field() {
    // 三种运行模式都在下拉里。
    assert!(INIT_HTML.contains("value=\"local\""));
    assert!(INIT_HTML.contains("value=\"cloud\""));
    assert!(INIT_HTML.contains("value=\"dual\""));
    // 云端地址输入 + 仅在含云端形态时展开。
    assert!(INIT_HTML.contains("id=\"base\""));
    assert!(INIT_HTML.contains("needsCloud"));
    // 提交走初始化哨兵。
    assert!(INIT_HTML.contains("/__desktop/provision?mode="));
    // 占位符齐全，渲染时都会被替换。
    assert!(INIT_HTML.contains("__CURRENT_MODE__"));
    assert!(INIT_HTML.contains("__CLOUD_BASE__"));
    assert!(INIT_HTML.contains("__LOCAL_SUPPORTED__"));
}

/// 仅交付混合模式的构建：初始化页不问模式也不问地址，只有一个确认动作。
#[test]
fn fixed_init_page_asks_nothing_and_only_confirms() {
    assert!(!INIT_FIXED_HTML.contains("<select"));
    assert!(!INIT_FIXED_HTML.contains("id=\"base\""));
    assert!(!INIT_FIXED_HTML.contains("value=\"cloud\""));
    // 提交的哨兵不带任何模式 / 地址参数——形态由 Rust 端固定。
    assert!(INIT_FIXED_HTML.contains("'/__desktop/provision'"));
    assert!(!INIT_FIXED_HTML.contains("/__desktop/provision?"));
    assert!(INIT_FIXED_HTML.contains("__LOCAL_SUPPORTED__"));
}

/// 初始化的动画视觉：首启页与安装进度页共用同一套（光晕 + 轨道 + 浮动核心），
/// 并且都尊重「减少动态效果」的系统偏好。
#[test]
fn initialization_pages_share_the_same_animated_visual() {
    for (name, html) in [("init-fixed", INIT_FIXED_HTML), ("setup", SETUP_HTML)] {
        assert!(html.contains("class=\"orbit\""), "{name} 页缺少轨道动画");
        assert!(html.contains("class=\"halo\""), "{name} 页缺少光晕");
        assert!(html.contains("@keyframes spin"), "{name} 页缺少旋转关键帧");
        assert!(html.contains("@keyframes float"), "{name} 页缺少浮动关键帧");
        assert!(html.contains("@keyframes halo"), "{name} 页缺少光晕关键帧");
        assert!(
            html.contains("prefers-reduced-motion"),
            "{name} 页没有为减少动态效果的用户关掉动画"
        );
    }
    // 进度条的流光只在进度页上。
    assert!(SETUP_HTML.contains("@keyframes sweep"));
}

/// 首启确认后进度页自己开装，用户不用在两张页面上各点一次；但纯云端形态落到
/// 本页是「云端不可达」，那种情况绝不能顺手装本机服务。
#[test]
fn setup_page_auto_starts_only_for_local_or_dual() {
    assert!(SETUP_HTML.contains("autoStartAttempted"));
    assert!(SETUP_HTML.contains("!autoStartAttempted&&(activeLocal||dual)"));
    assert!(SETUP_HTML.contains("await installLocal();return;"));
}

#[test]
fn titlebar_is_injected_before_page_content() {
    let html = "<html><body><main>content</main></body></html>";
    let output = inject_after_body(html, "<header>titlebar</header>");
    assert_eq!(
        output,
        "<html><body><header>titlebar</header><main>content</main></body></html>"
    );
}

#[test]
fn titlebar_is_injected_inside_body_with_attributes() {
    let html =
        "<!doctype html><html><body class=\"platform-macos\"><main>content</main></body></html>";
    let output = inject_after_body(html, "<header>titlebar</header>");
    assert_eq!(
            output,
            "<!doctype html><html><body class=\"platform-macos\"><header>titlebar</header><main>content</main></body></html>"
        );
}

#[test]
fn site_paths_always_route_to_cloud() {
    for path in [
        "/site",
        "/site/",
        "/site/my-report/",
        "/site/my-report/assets/app.js",
        "/api/v1/sites",
        "/api/v1/sites/abc/submissions",
    ] {
        assert!(is_cloud_site_path(path), "{path} 必须走云端");
    }
}

#[test]
fn non_site_paths_stay_routable_to_local() {
    for path in [
        "/",
        "/api/v1/chats",
        "/api/v1/sitemap",
        "/sites",
        "/website/index.html",
    ] {
        assert!(!is_cloud_site_path(path), "{path} 不应被当作站点路径");
    }
}
