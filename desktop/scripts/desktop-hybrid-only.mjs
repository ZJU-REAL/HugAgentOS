/**
 * 「仅交付混合模式」构建选项。
 *
 * 打开后这个安装包只交付「本机 + 云端」双模式：首启不再让用户三选一，也不再问服务器
 * 地址，直接进带动画的初始化页，确认后在同一窗口装本机执行面。因为不再问地址，云端
 * 地址必须在构建时用 `JX_DEFAULT_SERVER_BASE` 烤进去 —— 缺了就硬失败，不留一个指向
 * 开发默认地址（localhost:3000）的包流到用户手里。
 */

/** 构建期布尔值：与 `brand.rs::env_flag` 认的真值保持一致。 */
export function isTruthy(value) {
  return ["1", "true", "TRUE", "yes"].includes(value);
}

export function resolveHybridOnly(args, env) {
  const enabled =
    args.includes("--hybrid-only") || isTruthy(env.JX_DESKTOP_HYBRID_ONLY);
  if (!enabled) return false;

  const base = (env.JX_DEFAULT_SERVER_BASE || "").trim();
  if (!base) {
    throw new Error(
      "仅混合模式的安装包不再向用户询问服务器地址，必须在构建时提供 " +
        "JX_DEFAULT_SERVER_BASE（云端入口地址）",
    );
  }
  if (!/^https?:\/\//.test(base)) {
    throw new Error(
      `JX_DEFAULT_SERVER_BASE 必须是 http(s) 地址，当前为 ${JSON.stringify(base)}`,
    );
  }
  return true;
}
