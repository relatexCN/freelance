# Freelance API Gateway 🚀

高性能、多厂家、智能分流的 OpenAI 兼容 API 中转网关。

本项目通过统一的 OpenAI 标准接口，聚合了 **Ollama (Cloud)**, **OpenRouter**, **Google Gemini**, **Groq**, **Cerebras**, **GitHub Models** 等数十个顶级算力平台。只需要在 `.env` 中配置模型提供商的 API Key 即可。灵感来自 FREERIDE 项目。

> **全自动、零配置的白嫖体验**：
> 如果你在请求中不指定任何模型名，或者模型名为空，网关会自动为你接通当前评分最高的免费模型。

***

## ✨ 核心特性

- **🎯 零参数智能选模**: 请求中不传递 `model` 参数时，默认自动查询并路由到当前最优的可用模型。
- **📊 全量战斗力可视化**: `/v1/models` 接口默认返回所有已配置厂商的模型排行，并在 `owned_by` 中量化显示实力得分。
- **🛡️ 工业级高可用机制**:
  - **并发发现 (Parallel Discovery)**: 并行抓取所有 Provider 模型列表，刷新效率提升 300%。
  - **启动预热 (Initial Hot Load)**: 启动时完成首次同步，确保上线即具备网关能力。
  - **阶梯式超时 (Tiered Timeout)**: 首选模型 20s 快速 Failover，后续尝试恢复标准超时，保障稳定性。
  - **单次请求熔断 (Circuit Breaker)**: Provider 连续失败 2 次后在本次请求中自动避开该厂商，配置位置在config.yaml中的max_retries。
- **⚡ 极速推理支持**: 原生对接 Groq, Cerebras 等低延迟厂家。

***

## 📡 API 接口参考

完全兼容 OpenAI SDK。

### 基础信息

| 项    | 值                                                      |
| ---- | ------------------------------------------------------ |
| 基础路径 | `http://localhost:8000`                                |
| 认证方式 | `Authorization: Bearer <GATEWAY_KEY>`                  |
| 内容类型 | `application/json`                                     |
| 字符编码 | UTF-8                                                  |

***

## 🔌 核心接口详解

### 1. 聊天补全接口

**接口地址**: `POST /v1/chat/completions`\
**兼容路径**: `POST /chat/completions` (无 `/v1` 前缀也支持)

#### 请求参数

| 参数名                         | 类型            | 必填 | 默认值      | 说明                                                                                               |
| --------------------------- | ------------- | -- | -------- | ------------------------------------------------------------------------------------------------ |
| **model**                   | string        | 否  | `"auto"` | 模型名称。特殊值：- `auto`: 自动选择最优模型- `free`: 仅使用免费模型- `best-free`: 最优免费模型- 具体模型名: 如 `gpt-4o`, `llama3` 等 |
| **messages**                | array         | 是  | -        | 消息历史数组，包含对话上下文                                                                                   |
| **temperature**             | float         | 否  | null     | 采样温度，范围 `0.0~2.0`。值越高越随机，越低越确定                                                                   |
| **top\_p**                  | float         | 否  | null     | 核采样参数，范围 `0.0~1.0`。与 `temperature` 二选一使用                                                         |
| **n**                       | integer       | 否  | null     | 生成的聊天补全选择数量                                                                                      |
| **stream**                  | boolean       | 否  | `false`  | 是否使用流式响应。`true` 时返回 SSE 流                                                                        |
| **stop**                    | string/array  | 否  | null     | 停止序列，遇到该序列时停止生成                                                                                  |
| **max\_tokens**             | integer       | 否  | null     | 生成的最大 token 数                                                                                    |
| **max\_completion\_tokens** | integer       | 否  | null     | 补全的最大 token 数（OpenAI 新参数）                                                                        |
| **presence\_penalty**       | float         | 否  | null     | 存在惩罚，范围 `-2.0~2.0`。正值增加新话题的可能性                                                                   |
| **frequency\_penalty**      | float         | 否  | null     | 频率惩罚，范围 `-2.0~2.0`。正值减少重复内容                                                                      |
| **logit\_bias**             | object        | 否  | null     | 修改指定 token 出现的概率                                                                                 |
| **logprobs**                | boolean       | 否  | null     | 是否返回对数概率                                                                                         |
| **top\_logprobs**           | integer       | 否  | null     | 返回最可能的 N 个 token 及其对数概率                                                                          |
| **user**                    | string        | 否  | null     | 最终用户唯一标识符，用于滥用检测                                                                                 |
| **seed**                    | integer       | 否  | null     | 随机种子，用于可重复生成                                                                                     |
| **tools**                   | array         | 否  | null     | 可用工具列表，支持函数调用                                                                                    |
| **tool\_choice**            | string/object | 否  | null     | 工具选择策略：`auto`, `none`, 或指定工具                                                                     |
| **response\_format**        | object        | 否  | null     | 响应格式配置                                                                                           |

#### messages 对象结构

每个消息对象包含：

| 字段                 | 类型           | 必填 | 说明                                                                                         |
| ------------------ | ------------ | -- | ------------------------------------------------------------------------------------------ |
| **role**           | string       | 是  | 消息角色：- `system`: 系统提示- `user`: 用户消息- `assistant`: 助手回复- `developer`: 开发者提示- `tool`: 工具调用结果 |
| **content**        | string/array | 否  | 消息内容。可以是字符串或多模态内容数组                                                                        |
| **name**           | string       | 否  | 参与者名称                                                                                      |
| **tool\_calls**    | array        | 否  | 工具调用列表，由助手返回                                                                               |
| **tool\_call\_id** | string       | 否  | 工具调用 ID，用于关联结果                                                                             |

#### response\_format 对象

用于指定响应格式：

```json
{
  "type": "json_object",
  "schema": { ... }
}
```

- `type`: `text` 或 `json_object`
- 当 `type=json_object` 时，模型会输出有效的 JSON

#### 请求示例

```bash
# 智能选模示例
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <GATEWAY_KEY>" \
  -d '{
    "model": "auto",
    "messages": [
      {
        "role": "system",
        "content": "你是一个有用的助手。"
      },
      {
        "role": "user",
        "content": "你好，请介绍一下自己。"
      }
    ],
    "temperature": 0.7,
    "max_tokens": 1000
  }'
```

#### 流式响应示例

```bash
# 流式响应
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <GATEWAY_KEY>" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "写一首诗"}],
    "stream": true
  }'
```

流式响应使用 Server-Sent Events (SSE) 格式，每个事件包含一个数据块：

```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1712345678,"model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":"春"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1712345678,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"眠"},"finish_reason":null}]}

data: [DONE]
```

#### 响应格式（非流式）

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1712345678,
  "model": "gpt-4o",
  "system_fingerprint": "fp_xxx",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "你好！我是一个 AI 助手..."
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 15,
    "completion_tokens": 50,
    "total_tokens": 65
  }
}
```

***

### 2. 列出模型接口

**接口地址**: `GET /v1/models`\
**兼容路径**: `GET /models`

列出所有可用的模型，并按评分排序。

#### 查询参数

| 参数名      | 类型     | 必填 | 默认值      | 说明                                                         |
| -------- | ------ | -- | -------- | ---------------------------------------------------------- |
| **mode** | string | 否  | `"free"` | 过滤模式：- `free`: 仅显示免费模型（默认）- `paid`: 仅显示付费模型- `all`: 显示所有模型 |

#### 请求示例

```bash
# 获取模型列表（默认免费）
curl http://localhost:8000/v1/models

# 获取所有模型（免费+付费）
curl http://localhost:8000/v1/models?mode=all

# 仅获取付费模型
curl http://localhost:8000/v1/models?mode=paid
```

#### 响应格式

```json
{
  "object": "list",
  "data": [
    {
      "id": "openai/gpt-4o",
      "object": "model",
      "created": 1715558400,
      "owned_by": "openrouter | Score: 0.9523",
      "score": 0.9523,
      "context_length": 128000,
      "capabilities": ["tools", "vision"]
    },
    {
      "id": "meta-llama/llama-3.3-70b-instruct",
      "object": "model",
      "created": 1733529600,
      "owned_by": "openrouter | Score: 0.8945",
      "score": 0.8945,
      "context_length": 128000,
      "capabilities": null
    }
  ]
}
```

**响应字段说明**:

- `score`: 模型综合评分（0\~1），越高越好
- `context_length`: 模型支持的最大上下文长度（tokens）
- `capabilities`: 模型支持的能力列表（如 tools, vision 等）
- `owned_by`: 显示提供商名称和评分

***

### 3. 获取单个模型信息

**接口地址**: `GET /v1/models/{model_id}`\
**兼容路径**: `GET /models/{model_id}`

获取指定模型的详细信息。

#### 路径参数

| 参数名           | 类型     | 必填 | 说明                                  |
| ------------- | ------ | -- | ----------------------------------- |
| **model\_id** | string | 是  | 模型 ID，如 `gpt-4o`, `openai/gpt-4o` 等 |

#### 请求示例

```bash
curl http://localhost:8000/v1/models/gpt-4o
```

#### 响应格式

```json
{
  "id": "gpt-4o",
  "object": "model",
  "created": 1715558400,
  "owned_by": "openrouter | Score: 0.9523",
  "score": 0.9523,
  "context_length": 128000,
  "capabilities": ["tools", "vision"]
}
```

***

### 4. 健康检查接口

**接口地址**: `GET /health`

检查服务运行状态。

#### 请求示例

```bash
curl http://localhost:8000/health
```

#### 响应格式

```json
{
  "status": "healthy",
  "timestamp": 1712345678
}
```

***

### 5. 根路径信息

**接口地址**: `GET /`

获取网关基本信息和可用端点。

#### 请求示例

```bash
curl http://localhost:8000/
```

#### 响应格式

```json
{
  "name": "Freelance API Gateway",
  "version": "1.0.0",
  "status": "running",
  "providers": ["ollama", "openrouter", "groq", "cerebras"],
  "endpoints": {
    "chat_completions": "/chat/completions",
    "models": "/models",
    "health": "/health"
  }
}
```

***

### 6. 自动路由模式

网关支持智能自动路由，无需指定具体模型：

| 模式值         | 说明            | 使用场景        |
| ----------- | ------------- | ----------- |
| `auto`      | 自动选择评分最高的可用模型 | 通用场景，追求最佳体验 |
| `free`      | 仅使用免费模型       | 成本敏感场景      |
| `best-free` | 选择评分最高的免费模型   | 平衡成本与质量     |

**示例**:

```json
{
  "model": "auto",
  "messages": [{"role": "user", "content": "你好"}]
}
```

***

## ❌ 错误处理

### HTTP 状态码

| 状态码     | 说明    | 常见原因          |
| ------- | ----- | ------------- |
| **200** | 成功    | 请求正常处理        |
| **401** | 未授权   | API Key 无效或缺失 |
| **404** | 未找到   | 请求的模型不存在      |
| **502** | 网关错误  | 上游提供商服务异常     |
| **503** | 服务不可用 | 自动路由缓存未初始化    |
| **504** | 超时    | 请求处理超时        |

### 错误响应格式

```json
{
  "error": {
    "message": "Invalid or missing API key. Please use 'Authorization: Bearer <GATEWAY_KEY>'",
    "type": "invalid_request_error",
    "code": "invalid_api_key"
  }
}
```

**错误类型**:

- `invalid_request_error`: 请求参数错误
- `upstream_error`: 上游提供商错误
- `invalid_api_key`: API Key 无效

***

## 🛠️ 快速开始 (快速启动)

本项目支持 **Python 3.8+**，且提供了一键式自动化环境准备工具。
一.复制`.env.example` 到 `.env` 并根据需要修改模型提供商的 API Key 。
二.运行 `.\freelance.bat start` 或 `./freelance.sh start` 启动服务。
### Windows (PowerShell/CMD):

```powershell
.\freelance.bat start
```

### Linux / macOS:

```bash
chmod +x freelance.sh
./freelance.sh start
```

> \[!NOTE]
> `freelance` 脚本会自动：
>
> 1. 创建虚拟环境 (`.venv`)。
> 2. 安装所有依赖及 `freelance-api` 命令。
> 3. 启动/管理服务。

***

## 📊 模型打分逻辑 (Scoring Logic)

Link 网关采用动态加权评分系统，自动选出当前能力最强且最稳定的模型（尤其在 `auto` 模式下）：

| 维度            | 权重      | 逻辑说明                              |
| :------------ | :------ | :-------------------------------- |
| **📏 上下文长度**  | **40%** | 以 1,000,000 tokens 为满分。上下文越大得分越高。 |
| **🧠 模型能力**   | **30%** | 根据厂商显性能力声明（tools, vision 等）打分。    |
| **📅 发布时效**   | **20%** | 计算模型发布天数，越新的模型权重越高。               |
| **🛡️ 厂家信任度** | **10%** | 对核心官方节点保底加分。                      |

***

## 💡 进阶命令

你可以通过 `freelance` 脚本直接调用所有 `freelance-api` 指令：

- **后台启动**: `.\freelance.bat start -d -p 8000` (Windows) 或 `./freelance.sh start -d` (Unix)
- **停止服务**: `.\freelance.bat stop`
- **生成密钥**: `.\freelance.bat keygen`
- **重启服务**: `.\freelance.bat restart -d`

***

##  OPENCLAW接入指南

**运行openclaw config->添加freelance网关->填写freelance API Gateway的API Key->填写模型ID：`auto`**

***

***
## 示例：
### 配置
平台A = api key1,api key2
平台B = 
平台C = api key3,api key4
### 运行模式
freelance->平台A->api key1
          |
          V
freelance->平台A->api key2
          |
          V
freelance->平台C->api key3
          |
          V
freelance->平台C->api key4
          |
          V
无可用模型，报错
### 简介
1.freelance运行时遍历所有带api key的平台。
2.调用models接口为所有可用平台模型打分按score高低依次排序
3.根据模型socre排行，使用对应平台的api key调用模型(比如score分排名第一的是平台A的模型1，则使用平台A的api key1调用模型1)
4.如果平台A的模型返回错误，则根据不同错误代码进行不同的处理
5.若调用模型1返回的是Timeout/网络错误,则尝试max_retries次，如果max_retries次都返回Timeout/网络错误，则直接跳过模型1，使用模型2进行尝试,跳过的模型1禁用一小时不再尝试。
6.若调用模型1返回的是非429频率限制方面的错误而是api key无效级别的错误(如401/403),则直接跳过api key1并禁用api key1三小时，使用api key2进行尝试。
7.若调用模型1返回的是429频率限制方面的错误，则直接跳过api key1并禁用api key1半小时，使用api key2进行尝试。
8.若调用模型1返回的是其他错误，则禁用api key1三小时，使用api key2进行尝试。
9.如果models接口中平台A的所有模型都返回错误，则视为该平台A暂不可用，禁用平台A三小时，跳过无api key的平台B,尝试带api key的平台C。
10.若所有api key都尝试了一遍，或者所有模型都尝试了一遍，都没有成功，则返回错误。

***

### 模型选择与上游调用逻辑总结
#### 1. 候选排序 (Ranking)：配置默认优先 > 缓存评分排序	

解析 auto 请求时，首先按 config.yaml 中定义的 default_model 排序，随后追加模型评分缓存中的其余模型。

#### 2. 循环尝试 (Iteration)：平台/模型可用性预检	

遍历候选列表。在调用前检查 is_provider_available。

#### 3. 智能优化 (Optimization)：熔断平台批量跳过 (新增)

循环内一旦发现某个平台处于熔断状态，系统会动态标记该平台。本轮请求中属于该平台的后续模型将不再记入报错详情，直接静默跳过。

#### 4. 故障处理 (Failure)：异常捕获 -> 熔断记录

若调用抛出异常（如超时），调用 record_failure。目前策略是：任何异常都会导致整个平台进入 1 小时（由配置决定）的熔断期。

#### 5. 错误反馈 (Response)：去重后的详细报错 (新增)

若所有候选者均不可用，将 tried_details 去重后返回给下游。客户端将只会看到每种错误状态的唯一代表，避免了因模型数量过多导致的“错误堆叠”。

#### 6. 智能体迭代 (Loop)：多轮 Turn 机制	

如果模型返回工具调用请求，Gateway 执行工具后会发起新的一轮 Turn，重新执行上述 1-5 步逻辑。

***

***

1. groq/llama-3.3-70b        (score: 0.85)  ← groq 无 default_model → ✅ 可调用
2. openrouter/nemotron-3     (score: 0.82)  ← openrouter 有 default_model 但不是这个 → ❌ 跳过
3. openrouter/gpt-4o-mini    (score: 0.78)  ← openrouter 有 default_model 但不是这个 → ❌ 跳过
4. nvidia/llama-3-nemotron   (score: 0.75)  ← nvidia 无 default_model → ✅ 可调用
5. openrouter/nemotron-3     (score: 0.82)  ← 这是 openrouter 的 default_model → ✅ 可调用

***

## 📜 开源协议

基于 MIT License 开源。请勿将其用于任何破坏厂家免费额度的恶意滥用。
