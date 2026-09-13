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

## 项目结构

```text
app/
  main.py                         FastAPI 入口、鉴权、中间件、模型缓存刷新、健康检查
  config.py                       读取 .env/config.yaml，维护供应商、模型路由、错误策略
  scoring.py                      模型评分、免费/付费过滤、排序
  cli.py                          freelance-api 命令入口
  models/
    request.py                    ChatCompletion 请求模型
    response.py                   OpenAI 兼容响应模型
  providers/
    base.py                       Provider 抽象基类
    openai_compatible.py          OpenAI-compatible 上游实现，请求透传、key 轮询、错误分类
  routers/
    chat.py                       /chat/completions、/models 路由
    stats.py                      统计接口
  services/
    agent.py                      请求编排、多模型尝试、工具调用模式
    router.py                     模型候选生成、provider/model 可用性判断、错误日志
    gateway_errors.py             统一错误对象和状态码规则
    streamer.py                   SSE 流式心跳和首包超时策略
    stats_service.py              请求统计
  utils/
    responses.py                  OpenAI 风格错误响应
    tool_converter.py             XML 工具调用解析
    tool_executor.py              本地工具执行

config.yaml                       供应商、模型别名、超时、冷却、透传、工具模式配置
.env                              网关 key 和供应商 API key
latest_models.log                 最近一次模型发现和实际路由候选说明
error_models.log                  按天追加的模型调用错误日志
freelance.log                     网关运行日志
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

项目支持 Python 3.8+。推荐优先使用仓库内置脚本启动：脚本会自动检查 Python、创建 `.venv`、安装依赖，并把命令转发给 `app.cli`。

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

如果系统没有 `python3`，请先安装 Python 3.8+。使用 Homebrew 的示例：

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
    "models": [
      "ollama:deepseek-v3.2",
      "openrouter:minimax/minimax-m2.5:free"
    ],
    "messages": [
      {"role": "user", "content": "分析今天的 AI 行业趋势"}
    ],
    "parallel": true,
    "stream": false
  }'
```

- `models` 为可选参数；传入时仅请求指定目标，支持 `provider:model-id` 和 `models` 配置中的别名。
- 省略 `models` 时，请求所有已配置 API Key 且设置了 `default_model` 的供应商默认模型；没有 `default_model` 或没有 API Key 的供应商会跳过。
- `parallel=true` 并行调用；`false` 按列表顺序串行调用。
- `stream` 为可选参数，默认 `false`。`stream=false` 返回聚合 JSON；`stream=true` 返回 SSE，每个 JSON 数据块都会附带 `provider`、`model` 和 `status`，所有模型结束后统一发送 `data: [DONE]`。
- 单个模型失败不会取消其他调用，整体状态为 `success`、`partial_success` 或 `failed`。
- 每项包含 `provider`、`model`、`status`、`latency_ms`、`content`、完整 `response`、`usage` 或 `error`。
- `settings.multi_model_max_models` 同时限制显式目标和自动选择的默认模型数量，默认最多 5 个；重复目标返回 400。若可调用的默认模型超过限制，应提高该配置或显式传入 `models`。
- 当所有上游模型均失败时，接口返回 HTTP 502，并在响应体中保留每个模型的错误详情；部分成功仍返回 HTTP 200。
- 多模型接口不执行本地 Agent 工具循环；当 `tools.mode=local_agent` 时返回 400。需要本地工具执行时请使用单模型接口，或将工具模式设为 `passthrough` 由客户端处理。

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

## 配置入口

项目有两个主要配置来源：

- `.env`：放网关访问 key 和供应商 API key。
- `config.yaml`：放供应商定义、模型别名、默认模型、错误策略、透传策略、工具模式。

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

## 日志文件

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
- 错误内容。
- 实际 `provider:model`。

### freelance.log

主运行日志，包含启动预热、请求耗时、模型刷新、上游调用等信息。

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

- `model=auto`、`model=free`、`model=best-free` 当前走同一套聊天候选逻辑；免费过滤主要体现在 `/models?mode=free` 展示接口。
- `latest_models.log` 只有在模型刷新成功且至少获取到一个模型时才会重写；如果所有供应商 `/models` 都失败，旧文件会保留。
- `latest_models.log` 的理论排名是按供应商分组展示，每组内部按分数排序，不是一个纯全局列表。
- 自动路由的“实际排名”优先来自可选的 `custom_ranking`，其后来自当前缓存和 `default_model` 配置，不是基于历史成功率的实时学习排序。
- 只有配置了 API key 的供应商会实例化并参与自动发现；没有 key 的供应商仍可保留在配置中，但不会被自动路由使用。

## License

MIT
