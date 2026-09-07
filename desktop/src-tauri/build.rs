fn main() {
    register_brand_env_inputs();
    tauri_build::build()
}

/// `brand.rs` 里每个 `option_env!` 都是构建期输入，但 cargo 并不知道——只改环境变量
/// 重新打包，它会认为没东西变而直接复用上次的产物，打出一个「看起来换了品牌、其实
/// 没换」的包。这里从源码里把变量名抠出来逐个登记，新增开关自动生效，不必再改本文件。
fn register_brand_env_inputs() {
    const PATH: &str = "src/brand.rs";
    const MARKER: &str = "option_env!(\"";
    println!("cargo:rerun-if-changed={PATH}");
    let Ok(source) = std::fs::read_to_string(PATH) else {
        return;
    };
    let mut rest = source.as_str();
    while let Some(at) = rest.find(MARKER) {
        rest = &rest[at + MARKER.len()..];
        let Some(end) = rest.find('"') else { break };
        println!("cargo:rerun-if-env-changed={}", &rest[..end]);
        rest = &rest[end..];
    }
}
