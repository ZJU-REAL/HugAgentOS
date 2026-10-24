# 通用智能体执行

[English](README.md)

公开入口为 runner.astream_chat_workflow。它选择主智能体或直接智能体路径，
并在调用方退出时关闭整条生成器链。

- runner / direct：运行生命周期、输出评审、最终元数据和响应后的工作。
- prepare_main / prepare_direct：创建智能体并组装输入。
- stream_main / stream_direct：把模型和工具事件转换成既有事件协议。
- main_state / direct_state：单轮持有的有类型状态，不跨任务共享。
- targets、skills、budget、events：目标选择、技能、预算与事件辅助逻辑。
- integrations：记忆、压缩、本体证据/修复与演化集成。
- complete：保留原有非流式执行入口和结果结构。

运行所有权与数据库事务在这些阶段之外管理。core.chat.runtime 中的聊天 worker
消费事件，在 core.runs 租约约束下持久化输出。Plan 与自主 Loop 的流程组合
属于外层 orchestration。
