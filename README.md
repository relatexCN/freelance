# Freelance API Gateway

Freelance API Gateway 是一个 OpenAI 兼容的多供应商 LLM 网关。客户端只需要调用统一的 `/v1/chat/completions`、`/v1/models` 等接口，网关会根据 `config.yaml` 中的供应商、模型别名、默认模型和错误策略，把请求转发到实际上游。

当前项目重点能力：

- 统一 OpenAI Chat Completions API。
- 多供应商接入，包括 Ollama Cloud、OpenRouter、Groq、Cerebras、SambaNova、NVIDIA、Mistral、Hyperbolic、Novita、Nebius、Google Gemini 等 OpenAI-compatible 上游。
- 自动模型发现、评分和缓存。
- `auto` 模式下按分数和供应商默认模型策略选择候选模型。
- 同一供应商内支持多个 API key 轮询和冷却。
- 按 HTTP 状态码统一分类错误，并控制 key、model、provider 的冷却策略。
- 支持 provider 级别的 body、header、参数透传配置。
- 支持 OpenAI 原生工具调用透传，也可切换为本地 Agent 工具执行模式。
- 多模型独立回答：跟随各厂商默认模型，支持并行/串行、部分成功、总超时和请求任务取消清理。
- 独立 stdio MCP 工具 `compare_models`：让 OpenClaw、Hermes 等客户端使用多模型接口，自行比较和汇总结果。
- 可选只读管理 Agent：读取供应商状态、模型缓存和调用统计，生成健康分析及建议，不自动改配置。
- 请求级结构化日志：关联请求、路由、Key 索引、耗时及错误，默认隐藏消息正文和 Key 片段。

## 架构与职责

项目以个人使用的模型网关为核心，提供三条独立调用路径：

| 入口 | 处理流程 | 输出 |
| --- | --- | --- |
| 单模型聊天 | HTTP → `AgentService` → 路由/Provider → 上游 | 一次聊天回答或 SSE；可按候选故障切换 |
| 多模型/MCP | 外部 Agent → MCP（可选）→ 多模型 HTTP → `MultiChatService` → 各上游 | 多份独立答案及各自状态；由外部 Agent 汇总 |
| 管理报告 | 管理 HTTP → `manager_agent` → 只读快照/分析模型 | 运行状态和管理建议，不执行配置修改 |

`core` 提供共享 Provider 契约和日志能力，`providers` 实现供应商适配。路由、熔断、Key 轮询仍由确定性代码控制。MCP 是单独进程，只调用已运行网关的 HTTP 接口，不维护另一份供应商配置。

当前 `services/agent.py` 仍承担普通聊天编排，并保留可选的本地工具循环。`manager_agent` 是只读模型运维分析模块，不是多 Agent 管理平台，也不是通用任务规划执行器。尚未实现独立 `gateway/manager/combined` 启动模式；管理状态、规划和策略暂集中在 `manager_agent/service.py`，未拆成 `state.py/planner.py/policy.py/controller.py`。

外部 Agent 接入推荐边界：网关负责模型调用；OpenClaw/Hermes 负责工具执行、多轮任务和答案汇总。普通聊天选 `tools.mode: passthrough`；多模型接口不接受工具调用。

## 项目结构

```text
app/
  main.py                         FastAPI 入口、鉴权、中间件、模型缓存刷新、健康检查
  config.py                       读取 .env/config.yaml，维护供应商、模型路由、错误策略
  scoring.py                      模型评分、免费/付费过滤、排序
  cli.py                          freelance-api 命令入口
  mcp_server.py                   freelance-mcp 入口，独立 stdio → HTTP 桥接
  core/
    providers/base.py             共享 Provider 抽象接口
    observability.py              结构化事件、请求 ID、完整响应生命周期记录
  models/
    request.py                    ChatCompletion 请求模型
    response.py                   OpenAI 兼容响应模型
  providers/
    base.py                       旧 Provider 接口路径的兼容导入
    openai_compatible.py          OpenAI-compatible 上游实现，请求透传、key 轮询、错误分类
  routers/
    chat.py                       /chat/completions、/chat/completions/multi、/models
    stats.py                      统计接口
  services/
    agent.py                      请求编排、多模型尝试、工具调用模式
    multi_chat.py                 多模型独立调用、结果聚合、超时和取消清理
    router.py                     模型候选生成、provider/model 可用性判断、错误日志
    gateway_errors.py             统一错误对象和状态码规则
    streamer.py                   SSE 流式心跳和首包超时策略
    stats_service.py              请求统计
  utils/
    responses.py                  OpenAI 风格错误响应
    tool_converter.py             XML 工具调用解析
    tool_executor.py              本地工具执行器旧路径兼容别名
  manager_agent/
    api.py                        /management/snapshot、/management/report
    monitor.py                    只读采集供应商、模型和统计快照
    service.py                    有限轮分析、只读工具白名单、建议校验、资源清理
    tools/executor.py             历史本地执行器；管理报告不使用它

config.yaml                       供应商、模型别名、超时、冷却、透传、工具模式配置
.env                              网关 key 和供应商 API key
latest_models.log                 最近一次模型发现和实际路由候选说明
error_models.log                  按天追加的模型调用错误日志
freelance.log                     网关运行日志
docs/                             MCP、管理 Agent、工具边界和阶段记录
tests/                            当前工作区的自动化测试
```

## 使用前必读

强烈建议在启动前，为每个启用的供应商配置 `default_model`。

```yaml
providers:
  openrouter:
    default_model: nvidia/nemotron-3-super-120b-a12b:free

  nvidia:
    default_model: minimaxai/minimax-m2.7
```

原因：`auto/free/best-free` 会走自动路由。如果某个供应商没有配置 `default_model`，该供应商从 `/models` 返回的所有模型都会按评分进入候选列表，实际调用模型可能随上游模型列表和评分变化而变化。配置 `default_model` 后，该供应商在自动路由中只尝试你指定的默认模型，调用行为更稳定、更可控，也更方便排查供应商兼容性和成本问题。

## 安装与启动

建议使用 Python 3.11（当前已验证环境）；MCP 要求 Python 3.10+。`pyproject.toml` 与启动脚本仍保留旧的 3.8 声明，不能视为当前依赖及代码在 3.8 上已经验证。仓库内置脚本会检查 Python，在首次创建 `.venv` 时安装依赖，并把命令转发给 `app.cli`；已有环境不会自动更新依赖，升级后需手动执行 `python -m pip install -e .`。

### 1. 准备配置文件

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

Linux / macOS：

```bash
cp .env.example .env
```

然后在 `.env` 中配置网关访问 key 和上游供应商 key。

```env
API_KEY=sk-freelance-key
OPENROUTER_API_KEY=sk-or-xxx
NVIDIA_API_KEY=nvapi-xxx
```

同一个供应商可以配置多个 key，用英文逗号分隔：

```env
OPENROUTER_API_KEY=sk-or-key1,sk-or-key2,sk-or-key3
```

### 2. Windows 使用方式

PowerShell 或 CMD：

```powershell
.\freelance.bat start
```

指定端口：

```powershell
.\freelance.bat start -p 8001
```

指定监听地址和端口：

```powershell
.\freelance.bat start --host 127.0.0.1 --port 8000
```

后台运行：

```powershell
.\freelance.bat start -d -p 8000
```

停止服务：

```powershell
.\freelance.bat stop
```

重启服务：

```powershell
.\freelance.bat restart -d -p 8000
```

生成并写入新的网关 API key：

```powershell
.\freelance.bat keygen
```

也可以在已安装环境中直接调用 CLI：

```powershell
freelance-api start -p 8000
freelance-api stop
freelance-api restart -d
freelance-api keygen
```

### 3. Linux 使用方式

首次使用先赋予脚本执行权限：

```bash
chmod +x freelance.sh
```

前台启动：

```bash
./freelance.sh start
```

指定端口：

```bash
./freelance.sh start -p 8001
```

指定监听地址和端口：

```bash
./freelance.sh start --host 0.0.0.0 --port 8000
```

后台运行：

```bash
./freelance.sh start -d -p 8000
```

停止服务：

```bash
./freelance.sh stop
```

重启服务：

```bash
./freelance.sh restart -d -p 8000
```

生成并写入新的网关 API key：

```bash
./freelance.sh keygen
```

也可以直接调用 CLI：

```bash
freelance-api start -p 8000
freelance-api stop
freelance-api restart -d
freelance-api keygen
```

### 4. macOS 使用方式

macOS 与 Linux 基本一致。首次使用先赋予执行权限：

```bash
chmod +x freelance.sh
```

前台启动：

```bash
./freelance.sh start
```

后台运行：

```bash
./freelance.sh start -d -p 8000
```

停止服务：

```bash
./freelance.sh stop
```

重启服务：

```bash
./freelance.sh restart -d -p 8000
```

生成并写入新的网关 API key：

```bash
./freelance.sh keygen
```

如果系统没有 `python3`，请先安装 Python 3.11 或经过验证的更新版本。使用 Homebrew 的示例：

```bash
brew install python
```

### 5. 常用参数

| 参数 | 说明 |
| --- | --- |
| `start` | 启动服务 |
| `stop` | 停止当前服务，依赖 `freelance.pid` |
| `restart` | 停止后重新启动 |
| `keygen` | 生成新的 `API_KEY` 并写入 `.env` |
| `-p, --port` | 指定端口，默认 `8000` |
| `--host` | 指定监听地址，默认 `0.0.0.0` |
| `--reload` | 启用开发热重载 |
| `-d, --daemon` | 后台运行，日志写入 `freelance.log` |

默认访问地址：

```text
http://localhost:8000
```

客户端请求需要携带：

```text
Authorization: Bearer <API_KEY>
```

如果 `.env` 中没有配置 `API_KEY` 或 `API_KEYS`，网关会跳过鉴权。

这是直接运行应用时的行为；仓库启动脚本还会执行 `scripts/check_gateway_key.py` 做启动前检查。个人本机使用可显式传 `--host 127.0.0.1`。CLI 的监听参数以 `--host/--port` 为准，不要仅修改 `.env` 的 `HOST/PORT` 就假定 CLI 已采用它们。

后台运行时，主日志写入：

```text
freelance.log
```

## API

### Chat Completions

```http
POST /v1/chat/completions
POST /chat/completions
```

示例：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-freelance-key" \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "user", "content": "你好，介绍一下你自己"}
    ],
    "stream": false
  }'
```

### Multi-model Chat Completions

同一个问题可并行或串行发送给多个已配置模型。该接口不会改变原有 Chat Completions 的自动回退行为。

```http
POST /v1/chat/completions/multi
POST /chat/completions/multi
```

```bash
curl http://localhost:8000/v1/chat/completions/multi \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-freelance-key" \
  -d '{
    "messages": [
      {"role": "user", "content": "比较这两种技术方案的优缺点"}
    ],
    "parallel": true
  }'
```

- 日常及 MCP 接入省略 `models`，使调用组合跟随配置。显式 `models` 参数保留兼容：传入时仅请求指定目标，支持 `provider:model-id` 和配置中的别名；MCP 不开放该参数。
- 省略 `models` 时，请求所有已配置 API Key 且设置了 `default_model` 的供应商默认模型；没有 `default_model` 或没有 API Key 的供应商会跳过。
- `parallel=true` 并行调用；`false` 按目标顺序串行调用。两者都是独立回答，串行不会把上一模型的答案传给下一模型，也不会自动投票、复核或汇总。
- 多模型接口固定返回非流式聚合 JSON。为兼容 OpenAI 客户端，允许省略 `stream`，也接受 `stream:false` 或 `stream:null` 并按非流式处理；`stream:true` 返回 422。需要 SSE 时使用单模型 `/chat/completions` 接口。
- 单个模型失败不会取消其他调用，整体状态为 `success`、`partial_success` 或 `failed`。
- 每项包含 `provider`、`model`、`status`、`latency_ms`、`content`、完整 `response`、`usage` 或 `error`。
- `settings.multi_model_max_models` 同时限制显式目标和默认模型数量，默认最多 5 个；重复目标返回 400。若默认组合超过限制，应调整厂商配置或提高上限，不会静默截断。
- `settings.multi_model_timeout` 控制整次多模型聚合的总等待时间，默认 90 秒，包含所有模型及其内部 Key 重试。超时后取消未完成调用并保留已完成结果；部分成功返回 200，全部超时返回 504。
- 当所有上游模型均失败时，接口返回 HTTP 502，并在响应体中保留每个模型的错误详情；部分成功仍返回 HTTP 200。
- 多模型接口不执行本地 Agent 工具循环；当 `tools.mode=local_agent` 时返回 400。需要本地工具执行时请使用单模型接口，或将工具模式设为 `passthrough` 由客户端处理。
- 非空 `tools/functions`、启用工具的 `tool_choice/function_call`、`parallel_tool_calls:true` 返回 400（`multi_tools_not_supported`）。空列表和明确关闭的参数兼容接收，但不传给上游。
- 上游任一 choice 返回 `tool_calls` 或工具调用结束标记时，该模型项失败（`multi_tool_calls_not_supported`），不返回其正文或工具指令；仍保留用量并计入失败统计，不因此熔断 Provider。
- 外层请求任务取消时，会取消并等待子任务清理；串行不再启动后续模型。重复取消不会打断清理。客户端仅断开 TCP 不一定触发服务器任务取消，目前没有主动断连监测，不能保证上游推理立即停止或退还额度。

完整边界见 [多模型工具与取消说明](docs/multi-model-tools.md)。

流式单模型请求也会检查 HTTP 200 SSE 数据中的结构化 `error`：首块即报错时会先尝试下一个 Key或备用模型；流已经输出后才报错时，会转发错误事件并补发 `data: [DONE]`，避免客户端因异常断链而无法结束请求。

### Models

```http
GET /v1/models
GET /models
GET /v1/models/{model_id}
GET /models/{model_id}
```

`/models` 从自动路由缓存中返回模型列表，并带上评分。

查询参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `mode` | `all` | `all` 返回全部缓存模型，`free` 只展示免费模型，`paid` 当前接口未单独过滤付费模型 |

注意：`/models?mode=free` 是展示层过滤；当前聊天请求中的 `model=free`、`model=best-free` 和 `model=auto` 都进入同一套自动候选生成逻辑。

### Health

```http
GET /health
GET /health/providers
```

`/health/providers` 会返回每个供应商的熔断状态、可用 key 数量、连续失败次数和 `default_model`。

`/health` 仅表示 Web 进程可响应，不代表上游模型可用。`GET /stats` 提供当前进程累计请求、成功/失败次数、延迟等统计；流式成功可能仅表示首包收到，不能直接当作完整推理成功率。

### 管理 Agent（可选，只读）

```http
GET /management/snapshot
POST /management/report
```

```yaml
settings:
  manager_agent:
    enabled: false
    provider: ""         # 分析报告使用的已配置供应商
    model: ""            # 上游实际模型 ID，需支持工具调用和 JSON 输出
    max_iterations: 4
    timeout_seconds: 60
    max_tokens: 1500
    auto_apply: false     # 目前只允许 false
```

- 启用并重启后，使用网关 Key 访问；管理接口要求配置 Key，即使普通网关允许无 Key 运行。
- `snapshot` 不调用模型；`report` 先采集快照，再让模型通过三个只读工具查看 Provider 状态、调用统计和模型缓存，最后返回中文分析及建议。
- 建议类型仅为观察、检查供应商、检查路由；不存在自动应用、审批执行或写配置接口。
- 同进程同时最多一份报告，忙时返回 409。缺少分析模型配置、超时或分析失败时返回 `snapshot_only` 与错误码，保留快照。
- 分析使用独立连接和克隆的 Key 状态，不改变业务轮询/冷却；仍共享上游账号额度。没有定时任务，只在请求报告时调用模型。
- 数据是单进程累计快照，模型和统计各最多 100 项；没有历史时间窗、主动健康探测、剩余额度、价格或质量测量。不会执行 `tools/executor.py` 中的本地工具。

详见 [管理 Agent 使用说明](docs/manager-agent.md)。

## MCP 与 OpenClaw / Hermes 接入

普通模型 Provider 配置只会调用单模型聊天接口。需要多模型比较时，使用独立 stdio MCP 工具 `compare_models`，由外部 Agent 负责比较和汇总。

安装可选依赖（Python 3.10+）：

```bash
python -m pip install -e ".[mcp]"
```

客户端通过已安装环境的 Python 启动 `python -m app.mcp_server`，或使用命令 `freelance-mcp`。网关须先独立启动；stdio MCP 不开放新的监听端口。

| MCP 环境变量 | 说明 |
| --- | --- |
| `FREELANCE_GATEWAY_URL` | 默认 `http://127.0.0.1:8000/v1`；填写基础地址，不含完整多模型路径 |
| `FREELANCE_GATEWAY_KEY` | 必填，网关 Key，不是供应商 Key |
| `FREELANCE_MCP_TIMEOUT` | 默认 120 秒，应大于网关多模型总超时；客户端工具超时应更长，例如 150 秒 |

MCP 进程不会自动读取项目 `.env`，需要在客户端环境中显式传入上述变量。工具参数：

```json
{"question": "比较两个方案的优缺点", "context": "可选背景资料", "parallel": true}
```

工具不接受模型列表或工具配置。返回文本 JSON 和 `structuredContent`；部分成功保留有效答案且 `isError=false`，全部失败或请求错误为 `isError=true`。模型调用会消耗供应商额度。取消 MCP 会关闭本地 HTTP 等待，但不会保证远端网关任务立即取消。

[MCP 安装和客户端配置示例](docs/mcp.md)提供 OpenClaw 与 Hermes 的配置方法。已通过标准 MCP 客户端到模拟 HTTP 网关的 stdio 协议测试；尚未完成两款宿主的真实模型联调，也未自动修改用户的客户端配置。

## 配置入口

项目有两个主要配置来源：

- `.env`：放网关访问 key 和供应商 API key。
- `config.yaml`：放供应商定义、模型别名、默认模型、错误策略、透传策略、工具模式。

`config.yaml.settings` 还控制多模型数量/总超时、结构化日志和可选管理 Agent。修改 `.env` 或 YAML 后需重启加载；模型列表每 6 小时刷新不等于配置热加载。建议从仓库根目录运行；配置查找优先当前工作目录，其次包目录。

### 供应商配置

供应商定义在 `config.yaml` 的 `providers` 下：

```yaml
providers:
  openrouter:
    type: openai
    base_url: https://openrouter.ai/api/v1
    api_key: ${OPENROUTER_API_KEY:}
    timeout: 120
    default_model: nvidia/nemotron-3-super-120b-a12b:free

  nvidia:
    type: openai
    base_url: https://integrate.api.nvidia.com/v1
    api_key: ${NVIDIA_API_KEY:}
    timeout: 250
    default_model: minimaxai/minimax-m2.7
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `type` | 当前主要支持 `openai`，表示上游兼容 OpenAI API |
| `base_url` | 上游 API 根地址，不需要带 `/chat/completions` |
| `api_key` | 支持 `${ENV_NAME:default}` 从 `.env` 或系统环境变量读取 |
| `timeout` | 该供应商默认超时时间 |
| `score_offset` | 给该供应商模型评分加权，可正可负 |
| `default_model` | 自动路由时该供应商强制使用的默认模型 |
| `pass_through_body` | 是否尽量透传客户端原始 JSON body |
| `header_passthrough` | 允许从客户端透传到上游的 header 白名单 |
| `header_overrides` | 写入或覆盖发给上游的 header |
| `param_overrides` | 发给上游前 set/delete 请求参数 |
| `disabled_fields` | 发给上游前删除指定字段 |

建议：实际使用时，给每个启用的供应商都配置 `default_model`。这样 `auto/free/best-free` 不会在该供应商返回的所有模型之间漂移，而是稳定调用你确认可用、成本和能力都符合预期的模型；当 `/models` 没有返回该模型时，网关也会把它作为兜底候选追加到实际路由中。

只有配置了可用 `api_key` 的供应商才会在启动时创建 provider 实例，也才会参与模型发现和自动路由。

### 增加、删除或替换供应商

增加供应商：

1. 在 `.env` 中增加供应商 API key。
2. 在 `config.yaml.providers` 中增加一个 provider。
3. 如果该供应商兼容 OpenAI `/chat/completions` 和 `/models`，`type` 保持 `openai`。
4. 重启服务，让启动预热重新拉取模型列表。

示例：

```yaml
providers:
  my-provider:
    type: openai
    base_url: https://api.example.com/v1
    api_key: ${MY_PROVIDER_API_KEY:}
    timeout: 120
    default_model: example/default-model
```

删除供应商：

1. 从 `providers` 删除该供应商，或清空它对应的 `.env` key。
2. 删除或调整 `models` 中指向该供应商的别名。
3. 重启服务。

替换供应商地址：

```yaml
providers:
  openrouter:
    base_url: https://new-compatible-endpoint.example.com/v1
```

如果只是替换 `base_url` 或 key，不需要改客户端请求；客户端仍然调用网关统一地址。

## 模型更换逻辑

模型有三种来源：客户端显式模型名、`models` 别名表、自动路由缓存。

### 1. 显式模型别名

`config.yaml.models` 可以把客户端传入的模型名映射到指定供应商和真实模型 ID：

```yaml
models:
  "gpt-4o": { provider: "openrouter", model: "openai/gpt-4o" }
  "deepseek-chat": { provider: "openrouter", model: "deepseek/deepseek-chat" }
```

客户端请求：

```json
{
  "model": "gpt-4o",
  "messages": [{"role": "user", "content": "hello"}]
}
```

实际调用：

```text
openrouter -> openai/gpt-4o
```

更换别名指向时，只需要改 `models` 表：

```yaml
models:
  "gpt-4o": { provider: "nvidia", model: "openai/gpt-4o" }
```

客户端仍然传 `model: "gpt-4o"`，网关会改走 `nvidia`。

### 2. 未命中别名的模型名

当客户端传入的 `model` 不在 `models` 表中时，网关按以下规则解析：

1. 如果模型名包含 `/`，并且配置了 `openrouter`，默认交给 `openrouter`，例如 `meta-llama/llama-3.3-70b-instruct`。
2. 否则交给 `default_provider`。
3. 如果没有可用 `default_provider`，使用配置中的第一个 provider。

示例：

```yaml
default_provider: "ollama"
```

客户端请求：

```json
{"model": "llama3"}
```

如果 `llama3` 没有在 `models` 表里配置，实际会走：

```text
ollama -> llama3
```

### 3. 自动模型选择

客户端传以下模型名时，会进入自动路由：

```text
auto
free
best-free
```

当前实现中，这三个值都会调用同一套候选生成逻辑。

自动路由依赖启动时和后台定时刷新的模型缓存：

1. 启动时只初始化配置了 API key 的供应商。
2. 对所有已初始化供应商并发请求 `/models`。
3. 给每个模型打上 `_gateway_provider`。
4. 使用 `scoring.py` 计算 `_score` 并降序排序。
5. 保存到 `config.auto_models_cache`。
6. 每 6 小时后台刷新一次。
7. 每次成功刷新会重写 `latest_models.log`。

评分权重：

| 维度 | 权重 | 说明 |
| --- | --- | --- |
| `capabilities` | 40% | 按工具调用、推理、结构化输出、Token 控制等能力组计分；同组参数再多也不会重复加分 |
| `context` | 20% | 8K 到 1M 之间采用对数曲线，长上下文仍有优势，但收益递减 |
| `recency` | 15% | 默认按 730 天半衰期平滑衰减；缺失或异常时间使用中性分 |
| `model_tier` | 15% | 根据 `pro/ultra/max/opus/sonnet`、参数规模及 `flash/lite/mini` 等模型档位标识评分 |
| `stability` | 10% | 稳定版本优先，`preview/beta/experimental/latest` 等版本适度降权 |

默认不再根据模型组织或供应商名称提供信任加分，避免供应商身份主导模型排名。每个模型会保存 `_score_components` 分项明细，`/models` 通过 `score_breakdown` 返回，`latest_models.log` 也会逐项展示。

供应商仍可通过 `score_offset` 做小幅运维修正，但最终修正会被 `max_provider_adjustment` 限制，默认最多 ±0.03：

```yaml
providers:
  groq:
    score_offset: 0.02

settings:
  scoring:
    capabilities_weight: 0.40
    context_weight: 0.20
    recency_weight: 0.15
    model_tier_weight: 0.15
    stability_weight: 0.10
    recency_half_life_days: 730
    max_provider_adjustment: 0.03
```

权重会自动归一化，因此允许按需调整而不必严格相加为 1。

## 自动路由中的供应商和模型切换

这是当前项目最核心的切换规则。

### 对话内切换命令

- `/switch`：将自动路由轮转到下一个优先级模型，并保留其后的候选模型作为故障回退。
- `/switch:openrouter:nvidia/chatgpt5.6`：固定调用 `openrouter` 供应商的 `nvidia/chatgpt5.6` 模型。
- `/switch:auto`：清除当前会话的固定选择，恢复自动路由第一优先级。
- 只有内容完全匹配上述格式的用户消息才会作为命令处理；普通正文中提到 `/switch` 不会触发切换。
- 网关依次从 `X-Freelance-Session-ID`、`X-OpenClaw-Session-ID`、`X-Conversation-ID`、`X-Session-ID`、`X-Chat-ID` 请求头及 OpenAI `user` 字段识别会话；切换状态默认保留 24 小时。
- 没有稳定会话标识时，网关仍根据请求携带的消息历史重建切换状态，作为兼容降级。

### 自定义模型排名（可选）

可以用 `auto_routing.custom_ranking` 显式指定 `auto/free/best-free` 的优先调用顺序：

```yaml
auto_routing:
  custom_ranking:
    - { provider: "agnes-ai", model: "agnes-2.0-flash" }
    - { provider: "agnes-ai", model: "agnes-image-2.0-flash" }
    - { provider: "openrouter", model: "nvidia/nemotron-3-ultra-550b-a55b:free" }
```

- 列表从上到下依次尝试，重复项会自动去重。
- 自定义项不依赖 `/models` 缓存；只要 provider 已配置 API key，即使模型发现没有返回该模型，它仍可参与调用。
- 自定义列表之后会追加原有评分和 `default_model` 规则生成的候选，作为回退。
- 未配置 `auto_routing`、删除 `custom_ranking`，或将它设为 `[]` 时，完全使用原有默认排名。
- 推荐使用上面的 `{ provider, model }` 写法；也支持 `models` 中已有的别名，以及 `"provider:model-id"` 简写。

### 候选列表生成规则

当请求 `model=auto/free/best-free` 时，`RouterService.get_models_to_try()` 会生成候选列表：

备注：推荐为生产环境中的每个供应商设置 `default_model`。未设置时，该供应商的所有缓存模型都会按评分进入自动候选，模型可能随上游 `/models` 返回结果和评分变化而变化；设置后，该供应商在自动路由中只尝试指定默认模型，行为更可控。

1. 如果配置了 `custom_ranking`，先按配置顺序加入这些模型。
2. 读取已经按 `_score` 排好序的 `auto_models_cache`。
3. 如果某个供应商没有配置 `default_model`，该供应商缓存中的所有模型都可以进入候选列表，顺序按评分。
4. 如果某个供应商配置了 `default_model`，默认排名中只有这个 `default_model` 可以进入候选列表；自定义排名仍可显式加入该供应商的其他模型。
5. 如果配置了 `default_model`，但该模型没有出现在 `/models` 缓存中，只要该供应商配置了 API key，网关仍会把这个 `default_model` 作为兜底候选追加进去。
6. 合并候选并去重；自定义排名在前，默认排名在后。

示例：

```yaml
providers:
  openrouter:
    default_model: nvidia/nemotron-3-super-120b-a12b:free

  nvidia:
    default_model: minimaxai/minimax-m2.7

  cerebras:
    # 没有 default_model
```

假设缓存评分结果为：

```text
1. openrouter/openai/gpt-4o-mini                    score=0.90
2. cerebras/llama-4-scout                           score=0.88
3. openrouter/nvidia/nemotron-3-super-120b-a12b:free score=0.82
4. nvidia/minimaxai/minimax-m2.7                    score=0.75
```

实际候选会变成：

```text
1. cerebras -> llama-4-scout
2. openrouter -> nvidia/nemotron-3-super-120b-a12b:free
3. nvidia -> minimaxai/minimax-m2.7
```

原因：

- `openrouter` 配了 `default_model`，所以 `openai/gpt-4o-mini` 被跳过。
- `cerebras` 没有配 `default_model`，所以它的高分模型可以直接进入候选。
- `nvidia` 配了 `default_model`，只允许 `minimaxai/minimax-m2.7` 进入候选。

### 调用时切换规则

一次聊天请求进入 `AgentService.run()` 后，网关按候选列表顺序尝试：

1. 检查 provider 是否处于平台级冷却。
2. 检查目标 model 是否处于模型级冷却。
3. 获取 provider 实例。
4. 在该 provider 内选择一个可用 API key。
5. 请求上游。
6. 成功则直接返回。
7. 失败则记录错误，根据错误策略冷却 key、model 或 provider，然后继续尝试下一个候选。

简化流程：

```text
client
  -> gateway /v1/chat/completions
  -> 解析 model
  -> 生成候选 [(provider, actual_model), ...]
  -> 候选 1
       -> provider 内轮询 key
       -> 成功：返回
       -> 失败：记录冷却和错误日志
  -> 候选 2
  -> 候选 3
  -> 全部失败：返回 OpenAI 风格错误响应
```

### 同一供应商内的 API key 切换

每个 provider 可以有多个 key：

```env
OPENROUTER_API_KEY=key1,key2,key3
```

调用时 `ProviderConfig.get_next_key()` 会：

1. 优先选择不在冷却期、非 fatal 的 key。
2. 如果没有完全健康的 key，会选择非 fatal 的冷却中 key 作为临时兜底。
3. 如果所有 key 都 fatal，会触发 provider 级平台冷却。

HTTP 失败后的 key 处理由 `error_policy` 和错误分类共同决定：

| 错误 | 默认处理 |
| --- | --- |
| `429` | 标记当前 key 冷却，默认 30 分钟，不视为 fatal |
| `401` | 标记当前 key 冷却并 fatal，默认 3 小时 |
| `403` | 标记当前 key 冷却并 fatal，默认 3 小时 |
| quota/余额不足 | 标记当前 key 冷却并 fatal，默认 1 小时 |
| timeout/network | 标记当前 key 短冷却，并可推进 provider 失败统计 |
| `404` | 默认标记 model 冷却 |

### 模型级切换

模型级冷却由 `disable_model_status_codes` 控制，默认：

```yaml
settings:
  error_policy:
    disable_model_status_codes: "404"
```

如果某个候选模型返回 404，网关会认为这个模型路径不可用，记录模型冷却。后续自动路由遇到同一个 `provider/model` 时会跳过，直到冷却结束。

### 供应商级切换和熔断

供应商级冷却由两部分触发：

1. `GatewayError.disable_provider=true`，由 `error_policy.disable_provider_status_codes` 或 timeout/network 等异常分类决定。
2. 旧异常路径或所有 key fatal 时，调用 `record_platform_failure()`。

provider 不会一失败就立刻熔断。它有连续失败阈值：

```yaml
settings:
  max_retries: 2
```

当某供应商连续失败次数达到 `max_retries` 后，才进入平台级冷却。冷却期间：

- `RouterService.is_provider_available()` 返回不可用。
- 自动路由会跳过该供应商下的候选模型。
- `/health/providers` 可以看到 `melted=true` 和剩余秒数。

成功请求会重置该供应商的连续失败计数。

### 状态码策略配置

默认配置：

```yaml
settings:
  error_policy:
    retry_status_codes: "401-407,409-499,500-503,505-599"
    skip_retry_status_codes: "400,408,422,504,524"
    disable_key_status_codes: "401,403"
    disable_model_status_codes: "404"
    disable_provider_status_codes: ""
```

含义：

| 配置 | 作用 |
| --- | --- |
| `retry_status_codes` | 这些状态码允许继续尝试其他 key 或候选 |
| `skip_retry_status_codes` | 这些状态码直接返回给客户端，不再继续重试 |
| `disable_key_status_codes` | 这些状态码会冷却当前 key |
| `disable_model_status_codes` | 这些状态码会冷却当前 provider/model |
| `disable_provider_status_codes` | 这些状态码会推进 provider 熔断 |

例如希望上游 503 直接推进供应商熔断：

```yaml
settings:
  error_policy:
    disable_provider_status_codes: "503"
```

## 请求透传和上游兼容

不同 OpenAI-compatible 上游支持的参数不完全一致。可以在 provider 级别配置 body、header 和参数规则。

示例：

```yaml
providers:
  my-provider:
    type: openai
    base_url: https://api.example.com/v1
    api_key: ${MY_PROVIDER_API_KEY:}

    pass_through_body: true

    header_passthrough:
      - x-request-id
      - re:^x-custom-

    header_overrides:
      X-Provider-Feature: "enabled"
      X-Client-Trace: "{client_header:x-trace-id}"

    disabled_fields:
      - stream_options.include_obfuscation
      - service_tier

    param_overrides:
      - { op: "set", path: "reasoning.enabled", value: true }
      - { op: "delete", path: "temperature" }
```

规则说明：

- `pass_through_body: false` 时，网关使用标准化后的请求模型生成上游 payload。
- `pass_through_body: true` 时，网关尽量保留客户端原始 JSON body，但仍会强制覆盖 `model` 和 `stream`。
- `header_passthrough` 只透传白名单 header，默认不会透传 `authorization`、`cookie`、`host`、`content-length` 等敏感或 hop-by-hop header。
- `header_overrides` 可以写死 header，也可以从客户端 header 取值。
- `disabled_fields` 用点路径删除字段。
- `param_overrides` 支持 `set` 和 `delete`。

## 工具调用模式

工具调用由 `settings.tools.mode` 控制：

```yaml
settings:
  tools:
    mode: passthrough
```

可选值：

| 模式 | 行为 |
| --- | --- |
| `passthrough` | 默认模式。OpenAI 原生 `tools`、`tool_choice`、`tool_calls` 透传给上游和客户端，网关不执行本地工具 |
| `local_agent` | 网关执行支持的本地工具调用，并把工具结果加入下一轮请求 |
| `disabled` | 发给上游前删除 `tools`、`tool_choice`、`parallel_tool_calls` |

建议：

- 如果客户端自己处理 OpenAI 原生工具调用，使用 `passthrough`。
- 如果希望网关内置工具执行，使用 `local_agent`。
- 如果某些上游不兼容工具参数，使用 `disabled` 或 provider 的 `disabled_fields`。

本地工具描述根据函数签名生成参数 Schema，包含类型、必填项和默认值；不会替换客户端的工具定义，也未额外增加执行时参数校验。历史本地命令执行仍使用 shell，文件访问校验也不是沙箱隔离；接入 OpenClaw/Hermes 保持 `passthrough`，无需开启本地执行模式。管理报告中的三个只读工具与这里的通用执行器相互独立。

## 日志文件

### 故障处理与项目边界（2026-09）

网关负责：请求追踪、日志脱敏、上游 Key 重试、路由冷却、HTTP 请求预算、SSE 错误传递与响应结果摘要。
Hermes/OpenClaw 负责：识别流内错误而不是按“空回复”结束、工具执行检查点与恢复、命令展示、业务文件修改后的验收。微信脚本的 URL 改写、Token 保存及敏感日志属于远端业务项目，不由网关自动修复。

```yaml
settings:
  reliability:
    request_timeout_seconds: 180
    model_failure_threshold: 3
    model_cooldown_seconds: 60
```

- `request_timeout_seconds` 是单模型 `/chat/completions`（含 `/v1`）的整次 HTTP 请求预算，覆盖请求解析、路由、Key 重试、降级及流输出；多模型接口仍使用 `multi_model_timeout`。超时取消工作并等待清理，清理耗时可能使实际返回晚于预算。响应头发出前返回 HTTP 504；SSE 已开始后发送 `request_deadline_exceeded` 错误及结束标记，不更改已经发出的 HTTP 200。
- 同一次 Provider 调用不再重复尝试已经用过的 Key，即使其他并发请求改变了轮询游标。
- 同一 Provider/模型连续出现 503、504、网络/首包超时或非正常 EOF，达到阈值后施加短期模型冷却。阈值按上游尝试计数，不是按用户消息计数。完整成功响应重置失败计数；已经开启的冷却不会被旧的在途成功请求提前清除。后续路由跳过冷却模型；并发在途请求不会被强制终止。状态仅存在于当前进程。
- `upstream_failed.cooldown` 为错误策略参数；`cooldown_applied` 记录实际施加的 Key/模型冷却，`model_circuit_opened` 记录短期模型熔断。平台级熔断仍沿用原策略。
- 上游无 `[DONE]` 即 EOF 视为不完整流。输出前可重试，输出后报错结束，禁止拼接重放。
- 默认输出 `response_summary`，无需开启正文日志：包含 `model_outcome`、`finish_reasons`、`tool_call_count`、`content_chars`、`stream_error`、`done_received`。它逐行检查聊天 SSE，限制单行缓冲；超限或解析失败明确标为检查不完整，不保存正文。`completed` 仅代表模型协议层正常结束，不代表 Agent 业务任务通过验收；`length/content_filter` 标为 `truncated`。
- `request_finished.http_outcome` 表示 HTTP 传输结果；原 `outcome` 字段保留兼容。HTTP completed 与模型 error 可以同时出现，这是流内错误的正常表达。
- JSON 字符串按结构递归脱敏，不再对重新序列化的 JSON 执行第二轮正则替换。截断仅作用于叶子文本，外层工具参数 JSON 保持可解析；代码模板字符串边界不会被查询参数脱敏吞掉。脱敏后的日志仍不是可执行代码副本，也不能保证识别所有自由文本秘密。

以上配置修改后需要重启。当前仓库已有的正文日志启用状态予以保留；排查结束建议关闭正文记录。本次改动不自动重放历史失败任务、不修改外部业务文件，也不执行发布或创建草稿操作。

### latest_models.log

每次模型缓存刷新成功后重写，包含两部分：

```text
--- A: Theoretical Ranking (by score, grouped by provider) ---
--- B: Actual Routing (custom ranking + default fallbacks) ---
```

用途：

- 查看每个供应商拉到了哪些模型。
- 查看模型理论评分。
- 查看自动路由的真实候选顺序，以及哪些候选来自自定义排名。
- 发现某个 provider 的 `/models` 是否返回为空。

### error_models.log

模型调用失败时按天追加：

```text
=== 2026-07-02 ===
12:00:01 | RateLimit | status=429 | retryable | cooldown=key | ... | openrouter:model-id
```

字段包括：

- 时间。
- 错误类型。
- HTTP 状态码。
- 是否继续重试。
- 冷却对象：`key`、`model`、`provider`。
- 默认记录请求 ID 与错误码；开启自由文本诊断时才写入经 Key 脱敏的原始错误内容。
- 实际 `provider:model`。

### freelance.log

默认使用 JSON Lines，包含请求、Provider 选择、Key 索引、上游调用、首包、工具生命周期及响应结束等事件。相关事件携带 `request_id`，不会为日志先缓存完整 SSE 回答。

```yaml
settings:
  logging:
    format: json                 # json / text，text 也使用新的事件字段
    level: INFO
    log_request_payload: false   # 请求正文写入独立内容日志
    log_response_payload: false  # 响应正文写入独立内容日志
    content_log_path: freelance-content.log
    max_body_bytes: 262144       # 单次内容上限；响应超限/不完整/解析失败则省略正文
    max_log_bytes: 10485760      # 每个文件约 10 MiB 后轮转
    backup_count: 3             # 保留 3 个备份，不是保留 3 天
    include_diagnostics: false   # 旧自由文本日志，可能包含上游回显内容
    max_field_chars: 4096
```

默认不输出消息正文、Key 首尾片段或原始上游错误文本。已配置 Key 和结构化敏感字段会脱敏，但不能保证识别命令、调试文本或 SSE 分片中的所有私人信息。旧自由文本 INFO/DEBUG 在默认模式被过滤；WARNING 及以上保留级别与来源位置，主要调用链使用具名事件。

正文开关默认关闭，开启后需要重启。内容日志不输出到控制台或主运行日志；响应以解析后的 JSON 或 SSE 事件数组记录，不是拼接后的最终回答。只对聊天接口采集响应，采集内存受 `max_body_bytes` 限制，不改变转发内容。超限、未完整传输、无法解析时记录 `omitted: true`；字段截断标记为 `[TRUNCATED]`。不要将内容日志公开分享；排查完毕应关闭开关，并按需要清理备份。

每个 HTTP 响应附带网关生成的 `X-Request-ID`，对应日志中的 `request_id`。聊天事件还包含规范化请求的 HMAC 指纹 `request_fingerprint`：同一进程内相同请求有相同指纹，用于辅助识别重发，不执行自动去重。指纹密钥启动时随机生成，因此不能跨重启或多 worker 比较；指纹不同也不能证明业务意图不同。

工具执行仍属于 Hermes/OpenClaw 等客户端。网关内容日志只能记录模型工具调用及客户端回传的结果，不能证明命令真正执行。完整脱敏命令、工作目录、退出码和 stdout/stderr、工具调用 ID 需由执行端保留；聊天卡片省略和 Agent 循环预算也需在客户端处理。

流式心跳不再刷新上游数据的空闲期限。上游空闲超时时返回 `stream_idle_timeout` SSE 错误并结束；一旦已输出数据，普通网络异常不会再换 Key 重放回答，而是返回 `upstream_stream_interrupted`。首包前的既有重试/降级保持不变。客户端仍需检查 SSE 错误，HTTP 200 不代表模型业务成功。断开或取消时等待流式子任务清理并关闭上游生成器。

`request_finished` 的耗时涵盖完整响应传输；`outcome=completed` 仅表示传输完成，流内模型错误应结合 `upstream_failed/stream_failed` 判断。HTTP 200 不一定等于推理成功。

旧日志不会主动删除，首次轮转前可能同时包含旧文本与新 JSON；依赖旧格式的采集脚本需要更新。应用直接管理的运行/内容日志按大小轮转，超过备份数量的旧文件会被覆盖；没有按天过期功能。daemon 的 stdout 重定向、Uvicorn access log 和 `error_models.log` 不受此轮转控制，需由部署端处理。多 worker 不应共享同一个轮转文件，应配置独立路径或使用外部日志收集。`/stats` 中既有错误信息和 API 错误响应不受上述日志隐私开关控制。

## 常见更换场景

### 把 `auto` 固定到某个供应商的指定模型

给供应商配置 `default_model`：

```yaml
providers:
  openrouter:
    default_model: nvidia/nemotron-3-super-120b-a12b:free
```

这样自动路由中，`openrouter` 只会尝试这个模型。

### 允许某个供应商自动使用所有高分模型

删除或留空该供应商的 `default_model`：

```yaml
providers:
  cerebras:
    default_model: ""
```

这样 `cerebras` 返回的模型会按评分进入候选。

### 把客户端模型名改到另一个上游

修改 `models` 别名：

```yaml
models:
  "gpt-4o-mini": { provider: "openrouter", model: "openai/gpt-4o-mini" }
```

改为：

```yaml
models:
  "gpt-4o-mini": { provider: "nvidia", model: "openai/gpt-4o-mini" }
```

客户端请求不变：

```json
{"model": "gpt-4o-mini"}
```

### 临时禁用某个供应商

最简单方式是清空该供应商的环境变量 key，然后重启：

```env
OPENROUTER_API_KEY=
```

也可以从 `config.yaml.providers` 删除该供应商。

### 某个上游不支持特定参数

用 `disabled_fields` 删除：

```yaml
providers:
  my-provider:
    disabled_fields:
      - parallel_tool_calls
      - stream_options.include_usage
```

### 某个上游要求额外参数

用 `param_overrides` 设置：

```yaml
providers:
  my-provider:
    param_overrides:
      - { op: "set", path: "reasoning.enabled", value: true }
```

## 当前实现注意点

- 面向个人使用；没有多租户、计费或任务队列。Key 轮询、冷却、会话切换和统计均是进程内状态，重启会重置，多进程不共享。
- 多模型接口只做独立调用和结果聚合；没有自动摘要、跨模型复核、投票或业务任务编排。MCP 将结果交给外部 Agent 处理。
- 管理 Agent 默认关闭，仅提供模型运维分析；没有多个 Agent 实例管理、持久化任务、人工审批执行或独立 manager 启动模式。
- 请求取消清理针对服务器任务收到取消信号的情况，不保证单纯网络断开就触发清理，也不保证上游退还已经消耗的额度。
- `tools.mode=local_agent` 是历史本地执行能力，不代表多模型接口或 MCP 支持命令执行。多模型工具结果拒绝规则与单模型工具透传规则不同。
- `model=auto`、`model=free`、`model=best-free` 当前走同一套聊天候选逻辑；免费过滤主要体现在 `/models?mode=free` 展示接口。
- `latest_models.log` 只有在模型刷新成功且至少获取到一个模型时才会重写；如果所有供应商 `/models` 都失败，旧文件会保留。
- `latest_models.log` 的理论排名是按供应商分组展示，每组内部按分数排序，不是一个纯全局列表。
- 自动路由的“实际排名”优先来自可选的 `custom_ranking`，其后来自当前缓存和 `default_model` 配置，不是基于历史成功率的实时学习排序。
- 只有配置了 API key 的供应商会实例化并参与自动发现；没有 key 的供应商仍可保留在配置中，但不会被自动路由使用。

## 验证与详细文档

基础测试：

```bash
python -m unittest discover -s tests -q
```

MCP 协议测试需先安装 `.[mcp]`；未安装 SDK 时相关测试会跳过。Windows 独立 MCP 环境可运行：

```powershell
.\.venv-mcp\Scripts\python.exe -m unittest discover -s tests -p test_mcp_bridge.py -v
```

MCP 测试包含真实 stdio 子进程的初始化、工具发现和调用模拟网关，以及鉴权头、部分成功、超时和取消清理。模拟测试不验证真实供应商可用性，也不等同于 OpenClaw/Hermes 宿主联调；测试数量以当前工作区实际收集结果为准。

- [MCP 接入与客户端配置](docs/mcp.md)
- [多模型工具边界与取消清理](docs/multi-model-tools.md)
- [只读管理 Agent](docs/manager-agent.md)
- [第一阶段结构整理记录](docs/phase-one-refactor.md)

## License

MIT
