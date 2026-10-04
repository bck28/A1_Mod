"""A1 prompt replacement proxy. Python standard library only."""
from __future__ import annotations

import argparse
import copy
import datetime
import http.client
import json
import os
from pathlib import Path
import re
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PLACEHOLDER = re.compile(r"\{[A-Z][A-Z0-9_]*\}")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def write_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(value, encoding="utf-8", newline="")
    os.replace(temp, path)


def prompt_path(root, key, language):
    for value in (key, language):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
            raise ValueError(f"Invalid prompt filename: {value}")
    root = Path(root).resolve()
    path = root / "prompts" / language / (key + ".txt")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Prompt files must stay inside the Mod folder")
    return path


def load_prompt_files(root, original=None):
    """Text files are authoritative; a missing file uses the game's original."""
    if original is None:
        original = read_json(Path(root) / "templates.original.json")
    edited = copy.deepcopy(original)
    for row in edited:
        for language in row["templateText"]:
            path = prompt_path(root, row["templateKey"], language)
            if path.exists():
                row["templateText"][language] = path.read_text(encoding="utf-8-sig")
    return edited


def load_extra_instructions(root, config):
    path = Path(root) / "extra_system_instructions.txt"
    return path.read_text(encoding="utf-8-sig").strip() if path.exists() else config.get("extra_system_instructions", "")


def table(rows):
    result = {}
    for row in rows:
        key = row["templateKey"]
        if key in result:
            raise ValueError(f"Duplicate template: {key}")
        if not isinstance(row["templateText"], dict):
            raise ValueError(f"Invalid translations: {key}")
        result[key] = row["templateText"]
    return result


def compile_rules(original, edited):
    """Replace static spans; never consume rendered placeholder values."""
    old, new = table(original), table(edited)
    if old.keys() != new.keys():
        raise ValueError("Keep all templateKey entries; edit templateText only.")
    rules = {}
    changed = []
    for key, languages in old.items():
        if languages.keys() != new[key].keys():
            raise ValueError(f"Keep language keys: {key}")
        for language, source in languages.items():
            target = new[key][language]
            if not isinstance(target, str):
                raise ValueError(f"Text must be a string: {key}/{language}")
            source = source.replace("\r\n", "\n")
            target = target.replace("\r\n", "\n")
            if source == target:
                continue
            ident = f"{key}/{language}"
            if PLACEHOLDER.findall(source) != PLACEHOLDER.findall(target):
                raise ValueError(f"Keep placeholders and their order: {ident}")
            changed.append(ident)
            for before, after in zip(PLACEHOLDER.split(source), PLACEHOLDER.split(target)):
                if before == after:
                    continue
                if not before.strip():
                    if not after.strip():
                        continue  # Text editors commonly add a final newline.
                    raise ValueError(f"Cannot add text in an empty placeholder gap: {ident}")
                if before in rules and rules[before][0] != after:
                    raise ValueError(f"Conflicting replacements for shared text: {ident}")
                owners = rules.get(before, (None, []))[1] + [ident]
                rules[before] = (after, owners)
    # One simultaneous substitution prevents replacements from matching themselves.
    patterns = []
    for source in sorted(rules, key=len, reverse=True):
        escaped = re.escape(source)
        if len(source.strip()) < 12:
            escaped = r"(?<!\w)" + escaped + r"(?!\w)"
        patterns.append("(?:" + escaped + ")")
    matcher = re.compile("|".join(patterns)) if patterns else None
    return rules, matcher, changed


def rewrite_request(body, original, edited, extra=""):
    rules, matcher, changed = compile_rules(original, edited)
    result = copy.deepcopy(body)
    hits = {}
    fields = 0

    def replace(text):
        nonlocal fields
        fields += 1
        if matcher:
            text = text.replace("\r\n", "\n")
            def substitute(match):
                target, owners = rules[match.group()]
                for owner in owners:
                    hits[owner] = hits.get(owner, 0) + 1
                return target
            text = matcher.sub(substitute, text)
        if extra:
            text += "\n\n" + extra
        return text

    if isinstance(result.get("system_prompt"), str):
        result["system_prompt"] = replace(result["system_prompt"])
    messages = result.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = replace(content)
        elif isinstance(content, list):
            # Append extra instructions once, after the final text part.
            parts = [p for p in content if isinstance(p, dict) and p.get("type") == "text"
                     and isinstance(p.get("text"), str)]
            for part in parts:
                old_extra = extra
                if part is not parts[-1]:
                    extra = ""
                part["text"] = replace(part["text"])
                extra = old_extra
    return result, {"edited_templates": changed, "matched_templates": hits,
                    "unmatched_templates": [x for x in changed if x not in hits],
                    "system_text_fields": fields, "extra_instructions_used": bool(extra and fields)}


def load_config(root):
    config = read_json(root / "config.json")
    parsed = urlsplit(config["upstream_url"])
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("upstream_url must be an HTTP(S) URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("upstream_url cannot contain query strings or fragments")
    if config.get("listen_host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("listen_host must be 127.0.0.1")
    if not 1 <= int(config["listen_port"]) <= 65535:
        raise ValueError("Invalid listen_port")
    if parsed.hostname in ("127.0.0.1", "localhost", "::1") and (parsed.port or (443 if parsed.scheme == "https" else 80)) == int(config["listen_port"]):
        raise ValueError("upstream_url points back to this Mod")
    return config, parsed


def upstream_path(parsed):
    path = parsed.path.rstrip("/")
    return path if path.endswith("/chat/completions") else path + "/chat/completions"


def log_request(root, before, after, stats):
    folder = root / "logs" / (datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    # No HTTP headers, API keys, or responses are recorded.
    write_json(folder / "request.original.json", before)
    write_json(folder / "request.modified.json", after)
    write_json(folder / "replacement.json", stats)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def error_json(self, status, message):
        data = json.dumps({"error": {"message": message, "type": "a1_mod_error"}}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        if self.path != "/health":
            return self.error_json(404, "Use POST /v1/chat/completions")
        try:
            config, _ = load_config(self.server.mod_root)
            original = read_json(self.server.mod_root / "templates.original.json")
            _, _, changed = compile_rules(original, load_prompt_files(self.server.mod_root, original))
            data = json.dumps({"ok": True, "edited_templates": changed,
                               "prompt_source": "prompts/<language>/<templateKey>.txt",
                               "request_logging": config.get("log_requests", True)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ValueError, OSError, KeyError) as exc:
            self.error_json(422, str(exc))

    def do_POST(self):
        if urlsplit(self.path).path not in ("/chat/completions", "/v1/chat/completions"):
            return self.error_json(404, "Unsupported endpoint")
        connection = None
        sent_headers = False
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16 * 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                return self.error_json(413, "Expected JSON with Content-Length, up to 16 MiB")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Request must be a JSON object")
            root = self.server.mod_root
            config, parsed = load_config(root)
            original = read_json(root / "templates.original.json")
            after, stats = rewrite_request(body, original, load_prompt_files(root, original),
                                           load_extra_instructions(root, config))
            stats["prompt_source"] = "text_files"
            stats["edited_files"] = [f"prompts/{ident.rsplit('/', 1)[1]}/{ident.rsplit('/', 1)[0]}.txt"
                                     for ident in stats["edited_templates"]]
            if config.get("log_requests", True):
                log_request(root, body, after, stats)
            print(f"[A1_Mod] system_fields={stats['system_text_fields']} matched={sum(stats['matched_templates'].values())} unmatched={len(stats['unmatched_templates'])}", flush=True)
            cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
            connection = cls(parsed.hostname, parsed.port, timeout=float(config.get("timeout_seconds", 180)))
            nominated = {x.strip().lower() for x in self.headers.get("Connection", "").split(",")}
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP | nominated | {"host", "content-length", "accept-encoding", "content-type"}}
            headers["Content-Type"] = "application/json"
            headers["Accept-Encoding"] = "identity"
            # Optional environment variable only; keys never go into config or logs.
            env_name = config.get("api_key_env", "")
            if env_name:
                key = os.environ.get(env_name)
                if not key:
                    raise ValueError(f"API key environment variable is unset: {env_name}")
                for name in list(headers):
                    if name.lower() == "authorization":
                        del headers[name]
                headers["Authorization"] = "Bearer " + key
            encoded = json.dumps(after, ensure_ascii=False, separators=(",", ":")).encode()
            connection.request("POST", upstream_path(parsed), encoded, headers)
            response = connection.getresponse()
            self.send_response(response.status)
            excluded = HOP | {"content-length", "server", "date"}
            excluded |= {x.strip().lower() for x in response.getheader("Connection", "").split(",")}
            for name, value in response.getheaders():
                if name.lower() not in excluded:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            sent_headers = True
            self.close_connection = True
            # read1 streams bytes as they arrive, including SSE; response body is unchanged.
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (ValueError, KeyError, TypeError, OSError, http.client.HTTPException) as exc:
            if not sent_headers:
                status = 422 if isinstance(exc, (ValueError, KeyError, TypeError)) else 502
                # Connection failures are deliberately reported without credentials or headers.
                message = str(exc) if status == 422 else "Upstream connection failed; check URL, connectivity and timeout."
                self.error_json(status, message)
            else:
                self.close_connection = True
        finally:
            if connection:
                connection.close()


def make_server(root):
    root = Path(root)
    config, _ = load_config(root)
    original = read_json(root / "templates.original.json")
    compile_rules(original, load_prompt_files(root, original))
    server = ThreadingHTTPServer(("127.0.0.1", int(config["listen_port"])), Handler)
    server.daemon_threads = True
    server.mod_root = root
    return server


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "check"])
    args = parser.parse_args()
    config, parsed = load_config(ROOT)
    original = read_json(ROOT / "templates.original.json")
    _, _, changed = compile_rules(original, load_prompt_files(ROOT, original))
    print(f"修改的模板/语言数量：{len(changed)}")
    print(f"提示词文件夹：{ROOT / 'prompts'}")
    print(f"游戏自定义 API URL：http://127.0.0.1:{config['listen_port']}/v1")
    print(f"模型服务：{parsed.scheme}://{parsed.hostname}{upstream_path(parsed)}")
    if args.command == "check":
        print("配置与占位符检查通过。")
        return
    server = make_server(ROOT)
    print("Mod 已启动。保持窗口打开；Ctrl+C 停止。每次请求自动读取最新模板。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as exc:
        print("A1_Mod: " + str(exc), file=sys.stderr)
        sys.exit(1)
