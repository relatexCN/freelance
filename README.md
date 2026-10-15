# Freelance API Gateway

一个 OpenAI 兼容的多供应商模型网关。本开源分支保留单模型聊天、SSE 流式输出、模型发现与路由、故障回退、Key 轮询、服务端 CLI 和可选桌面客户端。定位免费大模型接口接入，鉴于免费模型的性能及稳定性，建议用于工具测试、对模型性能要求不高的低成本长期运行agent。

源码：<https://gitee.com/relatex/freelance> · MIT 许可

## 适用场景

- **Agent 24 小时运行**：为长期运行的 Agent 提供统一模型入口，通过备用供应商、Key 轮询和故障冷却减少单一上游异常带来的中断。适合配合进程守护及 Agent 自身的重试、任务恢复机制使用，不承诺上游持续可用或任务一定完成。
- **功能测试与联调**：通过桌面客户端验证模型连接、流式输出和响应结果，查看调用统计；通过 API 联调工具调用、会话路由及故障回退。
- **多个应用共用模型服务**：集中配置供应商地址、Key 和优先级，客户端通过统一地址调用，减少逐个应用修改上游配置的工作。

### 接入 OpenClaw、Hermes Agent、DeepSeek Harness 等客户端

可作为 OpenClaw、Hermes Agent、DeepSeek Harness 等 Agent 的 OpenAI 兼容后端。在客户端支持自定义 OpenAI Chat Completions 服务地址的模式下，使用以下配置：

| 客户端配置项 | 填写内容 |
| --- | --- |
| 接口类型 | OpenAI 兼容 / Chat Completions |
| Base URL | `http://127.0.0.1:8000/v1`；跨设备访问改为网关所在机器的内网 IP，并确保网关监听该地址 |
| API Key | 网关 `.env` 中的 `API_KEY`，不是上游供应商 Key |
| 模型名称 | `auto`，或配置好的模型别名，例如 `preferred` |

不同客户端的配置字段名称可能不同。上述为接口层接入方式，不代表已完成各客户端所有版本和功能的集成测试；仅使用 Responses、Anthropic 等其他协议的模式不能直接接入本分支的聊天接口。

Agent 使用工具时，保持 `settings.tools.mode: passthrough`，并选择支持工具调用的上游模型。工具由 Agent 执行，网关只透传工具定义及调用结果。自动回退不会保证不同模型具有相同的上下文长度、工具能力或输出习惯，因此备用模型也应满足任务要求。

### 推荐的使用步骤

1. 为每个供应商填写可用 Key，并设置一个已验证可调用的 `default_model`。这里指你为该供应商选定的默认模型，需要填写实际模型 ID。
2. 根据任务需要配置供应商优先级，准备至少一个能力相近的备用模型；需要自动选择与回退时，客户端模型填写 `auto`。
3. 启动网关，确认 `/health` 返回就绪，再通过桌面测试或 API 发送一条简单请求。
4. 对 Agent 进一步验证多轮对话、工具调用和任务所需的流式输出，再投入长期运行。服务端部署可使用 CLI 配合系统进程守护运行。

## 本分支范围

- 单次请求返回一个模型的聊天结果，兼容 `/v1/chat/completions` 和 `/chat/completions`。
- 自动发现模型，按评分、默认模型和自定义优先级生成路由候选。
- 上游故障时按策略切换候选；支持 Key、模型和供应商冷却。
- 无可用 Key 的候选会提前跳过，不计入模型请求和失败次数。流式调用在尚未输出内容时静默切换；已输出部分回答或思考内容后切换，才显示备用模型重新回答提示。
- 标准 OpenAI 工具定义、工具调用结果和 `response_format` 透传，工具由调用客户端执行。
- 调用统计、供应商健康检查、结构化日志和启动就绪状态。
- CLI 独立运行；桌面支持本地配置与进程管理，远程仅提供调用调试和只读状态查看。

本分支不包含官网、多模型聚合接口、MCP、管理 Agent、本地工具执行、XML 工具转换和结果二次格式化。不注册对应接口、不提供相关命令或依赖。旧版的相应配置段应从配置文件中移除；`tools.mode: local_agent` 不再支持，请改为 `passthrough` 或 `disabled`。

## 环境与安装

Python 3.10+，建议使用 Python 3.11。在项目根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

Linux/macOS 使用 `.venv/bin/python`。已有环境更新源码后需重新执行 `python -m pip install -e .` 同步依赖；CLI 脚本仅在首次创建环境时安装依赖。

在 `.env` 中填写网关调用 Key、供应商 Key，在 `config.yaml` 中填写供应商地址和默认模型。例如：

```dotenv
API_KEY=your-gateway-key
OPENROUTER_API_KEY=your-provider-key
```

```yaml
providers:
  openrouter:
    type: openai
    base_url: https://openrouter.ai/api/v1
    api_key: ${OPENROUTER_API_KEY:}
    default_model: your-upstream-model-id
    timeout: 120

models:
  preferred:
    provider: openrouter
    model: your-upstream-model-id

settings:
  tools:
    mode: passthrough
```

上面的 Key 和模型名称为占位值。已有 `.env` 请直接编辑，不要用示例覆盖。供应商 Key 支持用英文逗号分隔多个值；`API_KEYS` 可配置多个客户端调用 Key。不要提交密钥或运行日志。

## CLI 启动

Windows：

```powershell
.\freelance.bat start --host 127.0.0.1 --port 8000
.\freelance.bat start -d --host 127.0.0.1 --port 8000
.\freelance.bat restart -d --host 127.0.0.1 --port 8000
.\freelance.bat stop
.\freelance.bat keygen
```

Linux/macOS：

```bash
chmod +x freelance.sh
./freelance.sh start --host 127.0.0.1 --port 8000
./freelance.sh stop
```

也可以运行 `python -m app.cli start --host 127.0.0.1 --port 8000`，或安装后的 `freelance-api` 命令。默认监听地址为 `0.0.0.0`；通过 `--host`、`--port` 指定 CLI 的实际监听参数。

## 接口

除公开的根路径、健康状态和 API 文档外，调用需使用 `Authorization: Bearer <API_KEY>`。

| 接口 | 用途 |
| --- | --- |
| `POST /v1/chat/completions` | 普通或流式聊天；根路径别名 `/chat/completions` |
| `GET /v1/models` | 模型和虚拟路由列表；根路径别名 `/models` |
| `GET /v1/models/{model_id}` | 模型详情 |
| `GET /health` | 就绪后的健康状态 |
| `GET /health/providers` | 供应商和 Key 可用状态 |
| `GET /stats` | 当前进程的累计调用统计 |
| `GET /docs` | 交互式 API 文档 |

例如：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer your-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

使用 OpenAI SDK 或兼容客户端时，将 Base URL 设置为 `http://127.0.0.1:8000/v1`，填写网关调用 Key。`stream: true` 返回 SSE，客户端应处理 `[DONE]` 和流内错误。

工具调用采用 `settings.tools.mode: passthrough`：网关保留上游返回的 `tool_calls`、`finish_reason` 和多个 choice，由客户端执行工具并提交下一轮消息。`disabled` 可移除请求中的工具控制字段。网关不执行文件、Shell 或其他工具操作。

## 模型路由与优先级

`auto`、`free`、`best-free` 是自动路由名称，不保证上游免费。显式模型名称和 `models` 中的别名可指定目标；按当前故障回退策略保留默认候选。

建议为每个供应商设置 `default_model`，优先使用已经过功能测试、符合任务能力要求的实际模型 ID。否则该供应商发现的多个模型可能按评分进入候选列表，增加尝试次数，并可能选中不适合 Agent 工具调用的模型。`default_model` 用于收敛自动候选；显式请求或 `custom_ranking` 仍可指定其他模型。

```yaml
auto_routing:
  provider_order: [openrouter, nvidia]
  custom_ranking:
    - provider: openrouter
      model: your-preferred-model
```

- `provider_order` 指定供应商先后；未配置默认模型时，也能按已发现的实际模型排序。
- `custom_ranking` 指定优先模型，支持明确的 `{provider, model}` 条目、配置中的别名及 `provider:model` 简写。
- 设置供应商顺序后，候选先按供应商分组，同一供应商保留自定义模型及默认候选的相对顺序。
- 未指定供应商和未列出的默认候选保留为后续回退，不因列表不完整而丢弃。
- 同时清空两个列表或删除 `auto_routing` 可恢复默认评分排序。
- `/switch`、`/switch:provider:model`、`/switch:auto` 可按现有会话切换规则调整路由；客户端需传递稳定的会话标识。

## 可靠性与日志

`settings.reliability` 保留上游连接超时、请求总时限、慢请求告警、首个有效输出等待时限、模型熔断与恢复探测配置。`request_timeout_seconds: 0` 表示不设置整次单模型请求的硬截止；连接和读取等阶段仍有各自超时。

HTTP 已监听但模型初始化未完成时，业务和健康请求返回 503，并包含 `service_starting` 等状态码及 `Retry-After`。服务就绪后，未知接口正常返回 404。流式响应开始后若失败，HTTP 200 不能再更改，错误通过 SSE 返回。

`settings.error_policy` 支持按状态码控制重试及 Key、模型、供应商禁用；`cooldown`、`stream_probe`、`connection_pool` 及供应商级参数透传选项继续保留，详见 `config.yaml`。

日志默认不包含完整消息正文或 Key 片段。`request_id` 关联请求开始、响应开始和结束事件；`outcome: completed` 表示传输结束，不代表 HTTP 成功。若显式开启正文日志，日志会包含请求或响应内容。

## 桌面客户端

```powershell
python -m pip install -e ".[desktop]"
python -m app.desktop
```

本地模式直接读写配置文件，支持供应商、别名和 `^` / `v` 优先级调整，通过“保存供应商配置”保存，重启网关后生效。远程模式仅使用调用 Key 调试接口及查看状态，不提供服务端配置修改或远程进程控制。

便携版默认使用 EXE 所在目录的 `config.yaml`、`.env`，源码桌面使用项目根目录。缺失时创建默认配置，已有配置保留；界面不提供配置目录选择。详见 [桌面客户端说明](docs/desktop.md)。

### 手动生成 Windows 便携版

在 Windows 项目根目录执行：

```powershell
python -m pip install -e ".[desktop,desktop-build]"
python scripts/build_desktop.py --distpath dist/open-source
python scripts/verify_desktop_bundle.py --executable dist/open-source/FreelanceDesktop/FreelanceDesktop.exe
```

产物是 `dist/open-source/FreelanceDesktop` 整个文件夹，不能只复制 EXE。也可省略 `--distpath` 使用默认 `dist`，或用 `--distpath .` 生成在项目根目录的 `FreelanceDesktop` 子文件夹。构建脚本不自动生成 ZIP。

重复构建会清理目标 `FreelanceDesktop` 文件夹。构建前停止对应网关、退出客户端，并备份其 `config.yaml`、`.env`。分发前移除个人配置和日志。`dist`、`build` 和 `.cache` 都是本机构建缓存，不属于开源源码；旧版产物不会因源码剥离而自动更新。

## 验证

```powershell
python -m unittest discover -s tests -q
```

发布边界测试在独立临时配置下验证实际应用导入、已移除接口的 404、单模型回复、工具透传、流式输出和候选回退，不调用真实供应商。桌面测试需要安装 `.[desktop]`。

项目使用 MIT 许可。模型调用费用、配额和服务条款由上游供应商决定。
