# 多模型接口的工具边界

`POST /v1/chat/completions/multi`（及无 `/v1` 的路径）仅返回模型答案，不执行或接受上游返回的标准工具调用。

- 请求中非空 `tools` / `functions`、开启工具调用的 `tool_choice` / `function_call` 或 `parallel_tool_calls: true` 返回 HTTP 400，错误码 `multi_tools_not_supported`。
- 空工具列表、`tool_choice: none`、`parallel_tool_calls: false` 可兼容接收，但不会向上游发送工具配置。
- 任一 choice 带非空 `message.tool_calls`，或 `finish_reason` 为 `tool_calls` / `function_call`，该模型项返回 `status: error`、`error_code: multi_tool_calls_not_supported`。不返回其工具参数或混合正文，也不会执行工具。
- 其他模型正常完成时，HTTP 200、总状态 `partial_success`；全部失败时按原规则返回 HTTP 502（全部整体超时为 504）。调用方需检查每项状态。
- 被拒绝的工具响应仍保留已消耗的 Token 用量；记录该项失败，但不因此熔断供应商或停用 Key。
- 文本中讨论工具、JSON 或代码本身不等于协议工具调用，不做关键词过滤。已有对话历史不被改写。

普通 `/v1/chat/completions` 保持原有语义。接入 OpenClaw、Hermes 等外部 Agent 时，使用 `tools.mode: passthrough`，由调用方完成工具执行和多轮循环。现有 `local_agent` 模式和管理 Agent 功能不在本次调整范围内。

## 请求任务取消清理

并行与串行调用均在 finally 中取消未完成子任务，并等待其退出及释放连接。重复取消不会中断已经进行的子任务清理。串行任务取消后不再启动下一模型；整体超时继续保留已完成结果，未完成项仍标记 `multi_timeout`。取消不转换为供应商失败或触发重试。

这里处理的是服务器请求任务收到取消信号的情况。客户端单纯断开 TCP 连接不一定让 ASGI 服务器取消非流式请求；本轮没有增加主动断连监测。关闭本地上游连接也不保证供应商停止已接受的推理或退还用量。
