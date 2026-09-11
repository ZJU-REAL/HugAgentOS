# 第三方来源说明

本目录下的 `*.sbpl` 是 macOS Seatbelt（`sandbox-exec`）策略片段，取自
[openai/codex](https://github.com/openai/codex) 的 `codex-rs/sandboxing/src/`，
按 Apache License 2.0 使用并保留原文：

| 本仓库文件 | 上游文件 |
|---|---|
| `base.sbpl` | `seatbelt_base_policy.sbpl` |
| `network.sbpl` | `seatbelt_network_policy.sbpl` |
| `platform_defaults.sbpl` | `seatbelt_read_only_platform_defaults.sbpl` |
| `preferences.sbpl` | `seatbelt_preferences_policy.sbpl` |

这些片段本身又源自 Chromium 的 macOS sandbox 策略（BSD-3-Clause），上游文件头部
保留了对应的引用链接。

**不要手工改写这些文件。** 它们是「默认全关（`deny default`）+ 逐条放行」这套模型
的基座：每一条 `allow` 都对应一个真实的系统调用/服务依赖，删掉任意一条都可能让沙箱
里的进程在某个 macOS 版本上莫名其妙地起不来。需要放行新路径时，走
`core.sandbox.oslayer.policy` 的策略条目，由 `seatbelt.py` 生成动态规则，而不是往
基座里加规则。同步上游更新时整文件替换。
