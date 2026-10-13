"""HTTP transport with explicit management/inference credentials and SSE parsing."""
import asyncio
import json
from urllib.parse import urlsplit, urlunsplit

import httpx


def normalize_url(value):
    parts = urlsplit(value.strip().rstrip("/"))
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("请输入不含凭据及查询参数的 HTTP(S) 网关地址")
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class GatewayClient:
    def __init__(self, url, api_key="", admin_key=""):
        self.url = normalize_url(url)
        self.api_key, self.admin_key = api_key, admin_key

    def headers(self, admin=False):
        key = self.admin_key if admin else self.api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    @staticmethod
    def check(response):
        if response.is_error:
            # Do not echo raw upstream errors that might contain credentials or prompts.
            messages = {401: "认证失败，请检查对应的 Key", 409: "配置已变化或服务忙，请刷新后重试",
                        422: "参数无效，请检查输入", 503: "服务未就绪或管理功能未启用"}
            raise ValueError(messages.get(response.status_code, f"请求失败，HTTP {response.status_code}"))

    def request(self, method, path, body=None, admin=False):
        with httpx.Client(timeout=httpx.Timeout(120, connect=5), follow_redirects=False) as client:
            response = client.request(method, self.url + path, json=body, headers=self.headers(admin))
            if path == "/v1/chat/completions/multi" and response.status_code in {502, 504}:
                result = response.json()
                if isinstance(result, dict) and result.get("status") == "failed" and isinstance(result.get("responses"), list):
                    return result
            self.check(response)
            return response.json()

    def stream(self, body, emit, cancelled):
        return asyncio.run(self._stream(body, emit, cancelled))

    async def _stream(self, body, emit, cancelled):
        async def receive():
            async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=5), follow_redirects=False) as client:
                async with client.stream("POST", self.url + "/v1/chat/completions", json=body, headers=self.headers()) as response:
                    return await self._receive(response, emit)

        async def watch_cancel():
            while not cancelled.is_set():
                await asyncio.sleep(0.1)
            return "调用已取消；上游可能仍消耗额度"

        receiver = asyncio.create_task(receive())
        watcher = asyncio.create_task(watch_cancel())
        try:
            done, _ = await asyncio.wait({receiver, watcher}, return_when=asyncio.FIRST_COMPLETED)
            return await (receiver if receiver in done else watcher)
        finally:
            receiver.cancel()
            watcher.cancel()
            await asyncio.gather(receiver, watcher, return_exceptions=True)

    async def _receive(self, response, emit):
        self.check(response)
        data = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                payload = "\n".join(data)
                data.clear()
                if payload == "[DONE]":
                    return "调用结束"
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    raise ValueError("网关返回了无效流式数据") from None
                if "error" in event:
                    emit("\n[流式调用失败]\n")
                    raise ValueError("流式调用失败，请结合请求 ID 检查服务日志")
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        emit(delta["content"])
                    if delta.get("tool_calls"):
                        emit("\n[模型请求工具调用；测试界面不执行工具]\n")
        raise ValueError("流式连接提前结束，未收到完成标记")
