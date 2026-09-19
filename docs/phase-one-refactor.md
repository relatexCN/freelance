# 第一阶段：共享接口和工具归属整理

采用渐进迁移：先完成结构整理，再按用户确认补齐工具参数描述和结构化日志。现有工具模式、HTTP 接口和重试规则保持不变，尚未新增模型服务管理 Agent。

## 已完成

- Provider 的既有抽象接口迁入 `app/core/providers/base.py`，保留方法名称、参数和返回约定。适配器直接引用共享接口；`app/providers/base.py` 保留兼容导入，引用同一个类。
- 本地工具执行器迁入 `app/manager_agent/tools/executor.py`。旧路径保留模块别名，使原有导入、全局变量和单例注册表保持一致。
- 工具执行器迁移后仍使用原来的 `app/` 文件访问范围。没有因目录加深而缩窄或扩大允许范围，也没有修改原有路径校验算法。
- 当前 `AgentService` 改为从 Agent 工具目录导入执行器。它依然承担现有聊天入口的编排；本轮目录迁移不等同于完成独立网关运行模式。
- 新增离线 Provider 合约测试，覆盖普通响应、实际请求 Key 的轮询、429 冷却、401 失效、404 模型停用、工具调用保留、SSE 工具事件与结束标志、模型发现元数据。
- 新增兼容性测试，确认原有导入身份、工具单例和文件允许范围不变。

## 当前代码核对结果

当前版本已具有多轮 Agent 调用、工具模式配置及独立路由/流式服务。旧讨论中的单次工具调用描述已不适用于该版本。

当前路由和 Agent 服务没有为了记录日志额外调用 `get_next_key()`。本轮用模拟 HTTP 请求验证连续普通请求按第一、第二、第一把 Key 发出，因此无需实施旧建议中的重复取 Key 修复。

## 已确认并完成的行为调整

1. 工具参数 Schema：`get_tools()` 根据函数签名生成参数类型、必填字段及默认值；不增加执行时参数校验，不覆盖客户端的 `tools`。
2. 结构化日志：默认 JSON Lines，记录请求 ID、路由、Key 索引、耗时、错误分类、工具生命周期和流式结束事件。消息正文和 Key 片段默认不记录。ASGI 中间件直接转发消息，不缓存完整响应；请求耗时包含整个流式传输。

配置入口为 `config.yaml` 的 `settings.logging`：

```yaml
logging:
  format: json
  level: INFO
  log_request_payload: false
  include_diagnostics: false
  max_field_chars: 4096
```

- `format: text` 提供可读文本事件，不恢复旧字符串日志格式。
- `log_request_payload: true` 临时开启单模型及多模型请求正文；不记录响应正文。
- `include_diagnostics: true` 临时开启旧自由文本诊断。默认旧日志仅保留级别和来源位置，以避免上游错误回显私人内容。主要调用链已有命名事件。
- 已配置的网关及供应商 Key、结构化敏感字段会脱敏；自由文本中的任意私人信息不能保证自动识别，调试选项用完应关闭。
- `error_models.log` 保留原格式，但默认用请求 ID 和错误码代替原始错误文本。既有日志不删除，`freelance.log` 继续追加；旧文本和新 JSON 可能共存，应更新采集规则。`latest_models.log` 排名报告不变。
- `request_finished.outcome=completed` 仅表示 HTTP 传输完整；流内错误结合 `upstream_failed` / `stream_failed` 判断，HTTP 200 不代表推理成功。
- `/stats` 的既有错误信息与 API 错误响应不在日志改造范围内，行为保持不变。
- 配置修改需重启服务；本轮未重启或部署任何服务。

第一阶段已完成。未来如需把网关请求从 `AgentService` 分离、延迟加载执行器、增加模型能力筛选或改变工具权限，均应另行说明行为差异并确认。

## 验证

运行 `python -m unittest discover -s tests -q`：37 项通过。新增测试使用模拟上游，覆盖接口兼容、轮询、Schema、日志隐私、并发请求关联、流式即时转发、异常、断连及取消，无真实模型请求。`git diff --check` 通过。

保留工作区原有的 README、请求模型及既有测试改动；本轮没有改写这些内容。
