# 第二阶段：只读模型服务管理 Agent

提供独立的 `GET /management/snapshot` 和 `POST /management/report`。复用现有 HTTP 服务和网关认证，但不经过业务聊天的 `AgentService`，不自动修改路由、评分、配置或供应商状态。

## 启用

在 `config.yaml` 的 `settings.manager_agent` 配置：

```yaml
manager_agent:
  enabled: true
  provider: openrouter
  model: <上游实际模型ID>
  max_iterations: 4
  timeout_seconds: 60
  max_tokens: 1500
  auto_apply: false
```

重启后，使用现有网关 Key 认证。必须配置 `API_KEY` 或 `API_KEYS`。默认关闭，无周期任务；仅主动请求 report 时消耗模型额度。快照接口不调用模型。调用示例：

```sh
curl http://localhost:8000/management/snapshot -H "Authorization: Bearer <网关Key>"
curl -X POST http://localhost:8000/management/report -H "Authorization: Bearer <网关Key>"
```

分析供应商必须已在 providers 中配置 Key，模型须支持标准工具调用和 JSON 输出。未配置分析模型时 report 返回快照及 `manager_model_not_configured`，无需为了查看快照配置模型。

## 管理流程

采集一次快照 → 模型选择只读工具 → 返回供应商健康、调用统计、模型目录 → 模型返回 JSON 建议 → 结构校验 → 返回报告。

模型必须读取三类观察数据后才能完成报告。只有三个无参数的只读工具被允许。通用 shell/文件执行器不参与该流程，模型提出其他工具调用会终止分析并保留快照。

建议类型限制为 `observe`、`review_provider`、`review_routing`。没有应用建议的接口，`auto_apply` 只能为 false。建议是模型分析，仍需人工判断，不能视为已经执行的操作。

## 隔离与限制

- 模型分析使用独立连接和克隆的供应商状态，不推进业务 Key 轮询或修改业务冷却状态。但使用相同的上游账号，仍会消耗其真实额度。
- 单进程同时最多一份报告，其余返回 409。总耗时和模型轮数有限制；每轮适配器内部仍可能轮换多个 Key。
- 上游不可用、超时、非法 JSON、未经观察直接给结论、未知工具等均返回 `status: snapshot_only` 和错误码，不阻断业务聊天。
- 快照不包含 Key、消息正文、原始上游错误。数据来自当前进程；不做主动探测，不代表所有模型均可调用。
- 统计是进程内累计值，无时间窗口、价格、额度、模型质量数据。流式成功计数可能只代表首包成功。模型和统计各最多 100 条。
- 本阶段没有自动调度、审批执行、持久化报告、多进程状态汇总或独立 manager 启动模式。可由调用方保存返回报告。

## 验证

使用模拟模型验证工具循环、建议结构、未知工具拒绝、超时关闭连接、敏感信息排除、状态隔离和端点认证。没有真实模型调用或配置启用，实际模型工具调用兼容性需启用后验证。
