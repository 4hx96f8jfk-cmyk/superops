"""流式 chat_stream()：SSE 透传（M2-2.1）。

mock SSE server（stdlib http.server 线程）分块返回 OpenAI 兼容
`data: {...}\\n\\n` 帧 + `data: [DONE]`，客户端必须按序收全。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from modelhub import AllProvidersFailedError, ModelHub, ProviderConfig


@pytest.fixture()
def hub(tmp_path):
    h = ModelHub(db_path=tmp_path / "hub.db")
    yield h
    h.close()


def _sse_chunk(text, model="sse-model", usage=None):
    obj = {
        "id": "mock-sse",
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    if usage:
        obj["usage"] = usage
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


class _SSEHandler(BaseHTTPRequestHandler):
    chunks: list  # 由 fixture 注入：要逐块下发的 content 片段
    status: int  # 200 正常；其他码直接拒绝（模拟首字节前失败）
    delay: float
    bodies: list  # 由 fixture 注入：收到的请求体（验证 stream=true 透传）

    def log_message(self, *args):  # 静音
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.bodies.append(body)
        if self.status != 200:
            body = json.dumps({"error": "nope"}).encode()
            self.send_response(self.status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        frames = [_sse_chunk(c) for c in self.chunks]
        # 末尾带 usage 的收尾帧 + [DONE]
        frames.append(
            _sse_chunk(
                "",
                usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            )
        )
        frames.append("data: [DONE]\n\n")
        for f in frames:
            raw = f.encode()
            self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
            self.wfile.flush()
            if self.delay:
                time.sleep(self.delay)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def _sse_server(chunks, status=200, delay=0.0):
    bodies: list = []
    handler = type(
        "BoundSSE",
        (_SSEHandler,),
        {"chunks": chunks, "status": status, "delay": delay, "bodies": bodies},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1", bodies


def test_chat_stream_collects_deltas_in_order(hub):
    """分块下发 + 块间延迟，客户端按序收全且拼接一致。"""
    parts = ["He", "llo", " ", "世界", "!", ""]
    server, base_url, _ = _sse_server(parts, delay=0.01)
    try:
        hub.registry.register(
            ProviderConfig(name="sse", base_url=base_url, model="sse-model")
        )
        deltas = list(hub.chat_stream([{"role": "user", "content": "hi"}]))
        assert deltas == [p for p in parts if p]  # 空 delta 不下发
        assert "".join(deltas) == "Hello 世界!"
    finally:
        server.shutdown()
        server.server_close()


def test_chat_stream_records_usage(hub):
    """流收尾帧带 usage → 落一条 ok 流水。"""
    server, base_url, _ = _sse_server(["a", "b"])
    try:
        hub.registry.register(
            ProviderConfig(name="sse", base_url=base_url, model="sse-model")
        )
        list(hub.chat_stream([{"role": "user", "content": "hi"}]))
        rows = hub.usage.all_rows()
        assert len(rows) == 1
        assert rows[0]["status"] == "ok"
        assert rows[0]["prompt_tokens"] == 5 and rows[0]["completion_tokens"] == 3
    finally:
        server.shutdown()
        server.server_close()


def test_chat_stream_falls_back_before_first_delta(hub):
    """首字节前失败（HTTP 500）→ 换下一家；请求体必须带 stream=true。"""
    bad_server, bad_url, bad_bodies = _sse_server(["never"], status=500)
    ok_server, ok_url, ok_bodies = _sse_server(["o", "k"])
    try:
        hub.registry.register(
            ProviderConfig(name="bad", base_url=bad_url, model="m", kind="local")
        )
        hub.registry.register(
            ProviderConfig(name="good", base_url=ok_url, model="ok-model", kind="cloud")
        )
        text = "".join(hub.chat_stream([{"role": "user", "content": "hi"}]))
        assert text == "ok"
        assert ok_bodies and ok_bodies[-1]["stream"] is True
        assert len(bad_bodies) == 1  # 坏端点只撞一次
    finally:
        bad_server.shutdown()
        bad_server.server_close()
        ok_server.shutdown()
        ok_server.server_close()


def test_chat_stream_all_fail_raises(hub):
    bad_server, bad_url, _ = _sse_server(["x"], status=503)
    try:
        hub.registry.register(
            ProviderConfig(name="bad", base_url=bad_url, model="m", kind="local")
        )
        with pytest.raises(AllProvidersFailedError):
            list(hub.chat_stream([{"role": "user", "content": "hi"}]))
    finally:
        bad_server.shutdown()
        bad_server.server_close()
