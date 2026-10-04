"""A1 HTTP 测试服务：把游戏发来的请求原样打印到终端，不改写、不转发。"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18666
DEFAULT_REPLY = "HTTP 测试服务已收到请求。"
SEPARATOR = "=" * 72


def read_chunked(stream):
    """按 chunked 传输编码读取请求体，保持原始字节。"""
    data = bytearray()
    while True:
        line = stream.readline(65537)
        if not line:
            break
        size_field = line.split(b";", 1)[0].strip()
        try:
            size = int(size_field, 16)
        except ValueError:
            break
        if size == 0:
            while True:
                trailer = stream.readline(65537)
                if trailer in (b"\r\n", b"\n", b""):
                    break
            break
        chunk = stream.read(size)
        data += chunk
        stream.read(2)
        if len(chunk) < size:
            break
    return bytes(data)


def format_body(raw, pretty):
    """请求体默认原样输出；开启 pretty 且能被 JSON 解析时再缩进。"""
    text = raw.decode("utf-8", errors="replace")
    if not pretty or not text.strip():
        return text
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return json.dumps(value, ensure_ascii=False, indent=2)


def print_request(handler, raw):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host, port = handler.client_address[0], handler.client_address[1]
    print()
    print(SEPARATOR)
    print(f"[A1_HttpTest] {stamp} 来自 {host}:{port}")
    print("请求行（原样）：")
    print("  " + handler.requestline)
    print(f"请求头（原样，{len(handler.headers)} 项）：")
    for name, value in handler.headers.items():
        print(f"  {name}: {value}")
    if raw:
        print(f"请求体（原样，{len(raw)} 字节）：")
        print(format_body(raw, handler.server.pretty))
    else:
        print("请求体（原样，0 字节）：")
    print(SEPARATOR, flush=True)


def completion_payload(model, text, stream):
    created = int(time.time())
    if stream:
        return {"id": "chatcmpl-a1-http-test", "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                             "finish_reason": None}]}
    return {"id": "chatcmpl-a1-http-test", "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "A1HttpTest"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def read_body(self):
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            return read_chunked(self.rfile)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def do_GET(self):
        print_request(self, b"")
        data = json.dumps({"ok": True, "service": "A1 HTTP test", "path": self.path},
                          ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.read_body()
        print_request(self, raw)
        stream = False
        model = ""
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            body = None
        if isinstance(body, dict):
            stream = bool(body.get("stream"))
            model = str(body.get("model") or "")
        self.send_reply(stream, model or "a1-http-test")
        if self.server.once:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def send_reply(self, stream, model):
        text = self.server.reply_text
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            done = {"id": "chatcmpl-a1-http-test", "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            frames = [completion_payload(model, text, True), done]
            for frame in frames:
                self.wfile.write(b"data: " + json.dumps(frame, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        data = json.dumps(completion_payload(model, text, False), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description="打印游戏发来的 HTTP 请求，不做任何改写。")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 18666")
    parser.add_argument("--reply", default=DEFAULT_REPLY, help="返回给游戏的固定回复文本")
    parser.add_argument("--pretty", action="store_true", help="请求体是 JSON 时缩进显示")
    parser.add_argument("--once", action="store_true", help="处理一次请求后退出")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.pretty = args.pretty
    server.reply_text = args.reply
    server.once = args.once
    print(f"[A1_HttpTest] 监听 http://{args.host}:{args.port}/v1/chat/completions")
    print("把游戏的 AI 自定义模型 API URL 指向该地址，请求会原样打印到本窗口。")
    print("只打印、不改写、不转发；Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    print("[A1_HttpTest] 已停止。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print("A1_HttpTest: " + str(exc), file=sys.stderr)
        sys.exit(1)
