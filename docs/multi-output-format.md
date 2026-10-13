# 多模型结果格式化

多模型接口使用代码解析与校验，不增加默认模型调用，不修改业务判断。`overall_ai_probability`、`model_breakdown` 等是调用方的汇总字段，不是本网关的响应结构。

## 请求控制

- `output_format: "auto"`（默认）：识别完整 JSON 对象/数组、完整 Markdown JSON 代码块、前置说明文字后的单个 JSON。规范化为紧凑 JSON 字符串，并返回 `structured_content`。普通文本原样保留；明显以 JSON 开始但内容损坏的输出标为失败。不能保证任意自然语言都能转换为指定结构。
- `output_format: "json"`：必须返回完整对象/数组，无法解析则该模型项失败。不凭正则提取一个数字，不补字段，不修补截断内容。
- `output_format: "text"`：保留原始文本，不做 JSON 解析。
- `output_schema`：可选的网关本地校验结构，提供后自动要求 JSON。它与 `output_format` 不发给上游，不额外改写提示词。调用方仍应在消息中要求模型按目标结构返回。
- 已传入 `response_format.type=json_object/json_schema` 时也进行对应本地校验；`json_schema` 使用其中的 `json_schema.schema`，仍按原逻辑转发上游。它与 `output_schema` 同时存在时必须一致；`text` 与 JSON 要求冲突时返回 400。

JSON Schema 使用 Draft 2020-12 的受限子集：`type/properties/required/additionalProperties/items/enum/const/minimum/maximum/exclusiveMinimum/exclusiveMaximum/minLength/maxLength/minItems/maxItems/minProperties/maxProperties/title/description`。不支持引用、正则或组合 schema，避免远程取数和昂贵校验。schema 最大 16384 字符、嵌套 12 层；待解析内容最多 262144 字符、数据嵌套 64 层。重复键、非有限数值、多段歧义 JSON 不作为有效结构。JSON 模式达到上游 token 上限（`finish_reason=length`）标为 `output_truncated`。

## 示例：固定业务结构

```json
{
  "messages": [
    {"role": "user", "content": "分析待测文本并只返回一个 JSON 对象，包含 ai_probability（0至100的数字）、verdict（AI/HUMAN/UNCERTAIN）、reason（非空字符串）、evidence（字符串数组）。待测文本：……"}
  ],
  "output_format": "json",
  "output_schema": {
    "type": "object",
    "required": ["ai_probability", "verdict", "reason", "evidence"],
    "properties": {
      "ai_probability": {"type": "number", "minimum": 0, "maximum": 100},
      "verdict": {"type": "string", "enum": ["AI", "HUMAN", "UNCERTAIN"]},
      "reason": {"type": "string", "minLength": 1},
      "evidence": {"type": "array", "items": {"type": "string"}}
    },
    "additionalProperties": false
  }
}
```

发送到 `POST /v1/chat/completions/multi`，不指定模型时仍调用配置中的默认模型。

## 返回与调用方处理

- `content`：成功解析后为规范化 JSON 字符串；普通文本保持不变。
- `structured_content`：解析并通过所需校验的对象/数组，调用方优先读取它。
- `formatting_status`：`json` / `text` / `error` / `not_attempted`（上游超时、不可用、工具协议拒绝等未格式化情况）。
- `formatted_by: "code"`：代码进行了 JSON 格式化或尝试校验，无额外 LLM 调用。
- `response.choices[].message.content`：保留上游原始内容。格式失败时 `content` 也保留原文用于排查，不能因非空就当成成功答案。
- `error_code`：区分 `invalid_output_json`、`output_schema_mismatch`、`output_truncated`、`output_too_large`、`empty_model_output` 与上游错误。

格式错误使用该模型项的 `status: error`，参与现有 `partial_success/failed` 汇总，但不触发网络熔断或额外重试，已消费的 token 仍计入 usage。全部模型失败沿用 HTTP 502（全部总超时为 504）。原有工具拒绝及取消清理逻辑保持不变。

对于示例业务，只有 `status == success && formatting_status == json` 且按业务 schema 校验的项才应参与概率汇总；保留失败列表，不用缺字段项计算平均值。格式正确不表示模型概率可靠，也不意味着模型结论达成一致。网关不修改 15/88 等原始数值、不重算 verdict、不捏造 evidence。页面代码需改为读取新字段；仅升级网关不能自动修改页面的正则提取或平均值逻辑。

## 时间与部署

格式化在每个模型调用任务中完成，无新增网络请求；仍共享原有多模型总预算（默认 90 秒），不为格式修复延长或重新启动预算。超时、模型冷却不能靠格式化恢复。完整格式失败时适当调整调用方 prompt 或输出 token 上限，而不是让另一个模型猜测缺失内容。

新增依赖 `jsonschema>=4.18,<5`；部署时先更新依赖（例如 `python -m pip install -e .`），再重启网关。未指定 schema 的旧请求默认获得自动 JSON 格式识别；若依赖逐字节原文，请设置 `output_format: "text"` 或读取原始 `response`。
