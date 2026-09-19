# MCP 多模型工具

提供独立的 stdio MCP 服务 `freelance-mcp`，工具名 `compare_models`。它通过 HTTP 调用已运行的网关 `/v1/chat/completions/multi`，不启动第二个网关，也不读取本地 Provider 配置。

## 安装和启动

使用 Python 3.10+。普通网关安装不强制引入 MCP；本模块使用官方 Python SDK 受维护的 v1 系列，锁定 `<2` 避免大版本接口变化。

```powershell
python -m venv .venv-mcp
.\.venv-mcp\Scripts\python.exe -m pip install -e ".[mcp]"
```

本工作区已建立并验证 `.venv-mcp`。MCP 客户端启动命令可使用以下绝对路径，安装后的程序无需依赖当前工作目录：

```text
D:\www\freelance\.venv-mcp\Scripts\python.exe -m app.mcp_server
```

Linux/macOS 使用对应虚拟环境的 `bin/python`。stdio 由客户端拉起，不需要给 MCP 开放新端口。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `FREELANCE_GATEWAY_URL` | 网关 API 基础地址，默认 `http://127.0.0.1:8000/v1`，不要填写完整多模型接口路径 |
| `FREELANCE_GATEWAY_KEY` | 必填，现有网关 Key；不是供应商 Key |
| `FREELANCE_MCP_TIMEOUT` | HTTP 总等待秒数，默认 120；应大于网关的 `multi_model_timeout` |

桥接进程不自动读取项目 `.env`，通过客户端的 `env` 显式传入以上变量。不同电脑部署时 URL 应为客户端可访问的网关地址，跨机器优先使用 HTTPS。

## Hermes

将以下片段合入 Hermes 的配置；路径按实际安装位置替换。密钥写入 Hermes 的环境/密钥配置，不放在提示词里。

```yaml
mcp_servers:
  freelance:
    command: 'D:\www\freelance\.venv-mcp\Scripts\python.exe'
    args: ['-m', 'app.mcp_server']
    timeout: 150
    env:
      FREELANCE_GATEWAY_URL: 'http://127.0.0.1:8000/v1'
      FREELANCE_GATEWAY_KEY: '${FREELANCE_GATEWAY_KEY}'
      FREELANCE_MCP_TIMEOUT: '120'
```

Hermes 会过滤子进程环境，因此必须显式配置 `env`。配置后重新加载 MCP/开启新会话，确认工具列表包含 `compare_models`（客户端可能添加服务名前缀）。

## OpenClaw

支持原生 `mcp.servers` 的版本可合入以下配置；旧版本需使用该版本支持的 MCP 客户端插件或 bundle，不能仅配置模型 Provider 的 baseUrl。

```json
{
  "mcp": {
    "servers": {
      "freelance": {
        "command": "D:\\www\\freelance\\.venv-mcp\\Scripts\\python.exe",
        "args": ["-m", "app.mcp_server"],
        "requestTimeoutMs": 150000,
        "env": {
          "FREELANCE_GATEWAY_URL": "http://127.0.0.1:8000/v1",
          "FREELANCE_GATEWAY_KEY": "${FREELANCE_GATEWAY_KEY}",
          "FREELANCE_MCP_TIMEOUT": "120"
        }
      }
    }
  }
}
```

启用后确认工具未被 Agent 的工具策略禁用。未修改用户实际的 OpenClaw/Hermes 配置，也未宣称完成两款宿主的真实联调。

## 工具参数与返回

```json
{"question": "比较这两个方案的优缺点", "context": "可选的背景资料", "parallel": true}
```

不提供 `models`、`tools` 等参数；每次调用使用网关当前加载的厂商默认模型。默认模型组合超出网关数量上限时仍会失败，不自动截断。网关工具模式应为 `passthrough`。

返回文本 JSON 与 MCP `structuredContent`，包含总状态、各模型状态/答案/耗时/用量及总用量。`partial_success` 的 MCP `isError` 为 false，以保留可用答案；全部失败或请求失败时为 true。原始响应和原始错误正文不传给 Agent。由 OpenClaw/Hermes 判断是否补问、比较及汇总；本服务不执行模型建议的工具。

取消 MCP 调用会取消本地 HTTP 等待并关闭连接。网关对非流式 TCP 断连尚无主动监测，所以不能保证取消 MCP 就立即取消远端所有模型调用；网关仍有自身整体超时。已消耗的上游用量无法撤销。

## 验证

```powershell
.\.venv-mcp\Scripts\python.exe -m unittest discover -s tests -q
```

包含真实 stdio 子进程初始化、工具发现及工具调用到模拟 HTTP 网关的端到端测试，以及部分成功、鉴权头、超时和取消关闭连接测试。未向真实模型发出请求。

参考：[官方 MCP Python SDK v1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)、[OpenClaw MCP 配置](https://docs.openclaw.ai/plugins/bundles)、[Hermes MCP 配置](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference)。
