"""HTTP transport for gateway debugging with inference credentials and SSE parsing."""
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
    STATUS_TIMEOUT = 3
    OVERVIEW_TIMEOUT = 8

    def __init__(self, url, api_key=""):
        self.url = normalize_url(url)
        self.api_key = api_key

    def headers(self):
        key = self.api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    @staticmethod
    def check(response):
        if response.is_error:
            # Do not echo raw upstream errors that might contain credentials or prompts.
            messages = {401: "认证失败，请检查对应的 Key", 409: "配置已变化或服务忙，请刷新后重试",
                        422: "参数无效，请检查输入", 503: "服务未就绪，请检查网关启动状态"}
            raise ValueError(messages.get(response.status_code, f"请求失败，HTTP {response.status_code}"))

    def request(self, method, path, body=None):
        with httpx.Client(timeout=httpx.Timeout(120, connect=5), follow_redirects=False) as client:
            response = client.request(method, self.url + path, json=body, headers=self.headers())
            self.check(response)
            return response.json()

    def status(self, cancelled):
        """Cancellable, bounded probe; never use a chat request's long timeout."""
        return asyncio.run(self._status(cancelled))

    async def _status(self, cancelled):
        if cancelled.is_set():
            return None

        async def receive():
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.STATUS_TIMEOUT, connect=min(1, self.STATUS_TIMEOUT)),
                                         follow_redirects=False) as client:
                response = await client.get(self.url + "/health",
                                            headers=self.headers())
                if response.status_code == 503:
                    state = response.json().get("error", {}).get("code", "").removeprefix("service_")
                    if state in {"starting", "failed", "stopping"}:
                        return {"status": state}
                self.check(response)
                return {"status": "ready"}

        return await self._bounded(receive, cancelled, self.STATUS_TIMEOUT)

    def overview(self, cancelled, include_models=True):
        return asyncio.run(self._overview(cancelled, include_models))

    async def _overview(self, cancelled, include_models):
        async def receive():
            state = await self._status(cancelled)
            if state is None:
                return None
            if state["status"] != "ready":
                return {"state": state["status"]}
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.OVERVIEW_TIMEOUT, connect=1),
                                         follow_redirects=False) as client:
                async def get(path):
                    response = await client.get(self.url + path, headers=self.headers())
                    self.check(response)
                    return response.json()

                paths = [("health", "/health/providers"), ("stats", "/stats")]
                if include_models:
                    paths.append(("models", "/v1/models"))
                tasks = [asyncio.create_task(get(path)) for _, path in paths]
                try:
                    values = await asyncio.gather(*tasks)
                except Exception:
                    raise ValueError("网关已就绪，但运行数据加载失败；可重新连接或点击启动网关重试加载") from None
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                result = dict(zip((name for name, _ in paths), values))
                providers = result.pop("health")["providers"]
                stats = result.pop("stats")
                result["snapshot"] = {
                    "providers": [{"provider": name, "available_keys": item["usable_keys"],
                                   "key_count": item["total_keys"], "cooldown_remaining_seconds": item["melted_remaining_s"],
                                   "consecutive_failures": item["consecutive_failures"]} for name, item in providers.items()],
                    "metrics": [{"target": name, **{key: value.get(key) for key in (
                        "request_count", "success_count", "error_count", "avg_latency_ms")}}
                        for name, value in stats.items()],
                }
                return {"state": "ready", **result}

        return await self._bounded(receive, cancelled, self.OVERVIEW_TIMEOUT)

    async def _bounded(self, receive, cancelled, timeout):
        if cancelled.is_set():
            return None

        async def watch_cancel():
            while not cancelled.is_set():
                await asyncio.sleep(0.05)

        receiver = asyncio.create_task(receive())
        watcher = asyncio.create_task(watch_cancel())
        try:
            done, _ = await asyncio.wait({receiver, watcher}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if cancelled.is_set():
                return None
            if receiver in done:
                return await receiver
            if not done:
                raise ValueError("状态检查超时；请刷新确认网关状态")
            return None
        finally:
            receiver.cancel()
            watcher.cancel()
            await asyncio.gather(receiver, watcher, return_exceptions=True)

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
