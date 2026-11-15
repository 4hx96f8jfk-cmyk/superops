"""离线测试夹具：stdlib http.server 起的 mock OpenAI 兼容端点。

按 model 名切换行为，覆盖路由/探活/回退所需的全部场景：
  ok-model        正常回复 + usage
  boom            HTTP 500（模拟推理崩）
  empty-content   HTTP 200 但 content 为空（模拟 max_tokens 被吃光的假绿陷阱）
  reasoning-model content 空但 reasoning_content 有产出（reasoning 模型）
  slow-model      睡 0.3s（并发闸测试）
  crawl-model     睡 2s（per-provider timeout 超时降级测试）
  flaky-429       前两次 429、第三次起成功（错误分类重试测试）
  deny-401/403/400 固定拒绝码（不可重试分类测试）
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class MockState:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[dict] = []  # 收到的请求体（按序）
        self.inflight = 0
        self.max_inflight = 0
        self.counts: dict[str, int] = {}  # 按 model 的到达计数（flaky 场景用）


class _Handler(BaseHTTPRequestHandler):
    state: MockState  # 由 fixture 注入

    def log_message(self, *args):  # 静音
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _completion(self, content, finish_reason="stop", extra_msg=None):
        msg = {"role": "assistant", "content": content}
        if extra_msg:
            msg.update(extra_msg)
        return {
            "id": "mock-1",
            "object": "chat.completion",
            "model": self._model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self._model = body.get("model", "")
        model = self._model
        auth = self.headers.get("Authorization", "")
        with self.state.lock:
            self.state.requests.append({"path": self.path, "body": body, "auth": auth})
            self.state.inflight += 1
            self.state.max_inflight = max(self.state.max_inflight, self.state.inflight)
            self.state.counts[model] = self.state.counts.get(model, 0) + 1
            n = self.state.counts[model]
        try:
            if self.path != "/v1/chat/completions":
                self._json(404, {"error": "not found"})
            elif model == "boom":
                self._json(500, {"error": "model exploded"})
            elif model == "flaky-429":
                # 前两次 429（可重试），第三次起成功
                if n <= 2:
                    self._json(429, {"error": "rate limited"})
                else:
                    self._json(200, self._completion("flaky recovered"))
            elif model.startswith("deny-"):
                code = int(model.split("-", 1)[1])
                self._json(code, {"error": "denied"})
            elif model == "empty-content":
                # 假绿陷阱：HTTP 200、形状正确、content 为空
                self._json(200, self._completion("", finish_reason="length"))
            elif model == "reasoning-model":
                self._json(
                    200,
                    self._completion(
                        "", extra_msg={"reasoning_content": "让我想一想……"}
                    ),
                )
            elif model == "slow-model":
                time.sleep(0.3)
                self._json(200, self._completion("slow reply"))
            elif model == "crawl-model":
                # 明显超过 per-provider timeout，触发超时降级
                time.sleep(2.0)
                self._json(200, self._completion("too late"))
            else:
                self._json(200, self._completion(f"hello from {model}"))
        finally:
            with self.state.lock:
                self.state.inflight -= 1


@pytest.fixture()
def mock_server():
    """返回 (base_url, state)。base_url 已含 /v1 前缀。"""
    state = MockState()
    handler = type("Bound", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}/v1", state
    server.shutdown()
    server.server_close()


@pytest.fixture()
def dead_base_url():
    """一个确定没人监听的端口（TCP 必拒），用于 down 态测试。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1"
