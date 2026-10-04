import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("a1_mod", ROOT / "mod.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
ORIGINAL = mod.read_json(ROOT / "templates.original.json")


def seed_files(root, rows):
    for row in rows:
        for language, text in row['templateText'].items():
            mod.write_text(mod.prompt_path(root, row['templateKey'], language), text)


def edited_rules():
    edited = copy.deepcopy(ORIGINAL)
    item = next(r for r in edited if r["templateKey"] == "task.chat")
    source = item["templateText"]["zh-Hans"]
    item["templateText"]["zh-Hans"] = source.replace("## 任务：闲聊", "## 任务：闲聊\n测试规则：用简洁、自然的语言回应。", 1)
    return edited, source


class Replacements(unittest.TestCase):
    def test_original_is_noop(self):
        body = {"messages": [{"role": "system", "content": "保留原文\r\n下一行"},
                             {"role": "user", "content": "你好"}]}
        after, stats = mod.rewrite_request(body, ORIGINAL, ORIGINAL)
        self.assertEqual(after, body)
        self.assertEqual(stats["matched_templates"], {})

    def test_rendered_placeholders_and_history_preserved(self):
        edited, source = edited_rules()
        rendered = source.replace("{HIDDEN_TRIGGER}", "秘密触发条件：角色经历")
        rendered = rendered.replace("{DISPLAY_ACTIONS}", "可选动作：微笑")
        body = {"model": "test", "messages": [{"role": "system", "content": rendered},
                    {"role": "user", "content": source}, {"role": "assistant", "content": source}]}
        after, stats = mod.rewrite_request(body, ORIGINAL, edited)
        self.assertIn("测试规则", after["messages"][0]["content"])
        self.assertIn("秘密触发条件：角色经历可选动作：微笑", after["messages"][0]["content"])
        self.assertEqual(body["messages"][1:], after["messages"][1:])
        self.assertEqual(body["messages"][0]["content"], rendered)
        self.assertIn("task.chat/zh-Hans", stats["matched_templates"])

    def test_system_prompt_and_content_parts(self):
        edited, source = edited_rules()
        body = {"system_prompt": source, "messages": [{"role": "developer", "content": [
            {"type": "text", "text": source}, {"type": "image_url", "image_url": "image"},
            {"type": "text", "text": "动态事实"}]}]}
        after, stats = mod.rewrite_request(body, ORIGINAL, edited, "附加规则")
        self.assertIn("测试规则", after["system_prompt"])
        self.assertTrue(after["system_prompt"].endswith("附加规则"))
        parts = after["messages"][0]["content"]
        self.assertNotIn("附加规则", parts[0]["text"])
        self.assertEqual(parts[1], body["messages"][0]["content"][1])
        self.assertTrue(parts[2]["text"].endswith("附加规则"))
        self.assertEqual(stats["system_text_fields"], 3)

    def test_missing_placeholder_rejected(self):
        edited, _ = edited_rules()
        row = next(r for r in edited if r["templateKey"] == "task.chat")
        row["templateText"]["zh-Hans"] = row["templateText"]["zh-Hans"].replace("{HIDDEN_TRIGGER}", "")
        with self.assertRaisesRegex(ValueError, "placeholders"):
            mod.compile_rules(ORIGINAL, edited)

    def test_unmatched_and_nonrecursive(self):
        old = [{"templateKey": "test", "templateText": {"zh-Hans": "原始文字比较长以便匹配"}}]
        new = [{"templateKey": "test", "templateText": {"zh-Hans": "原始文字比较长以便匹配 + 新增"}}]
        after, stats = mod.rewrite_request({"system_prompt": "原始文字比较长以便匹配"}, old, new)
        self.assertEqual(after["system_prompt"], new[0]["templateText"]["zh-Hans"])
        _, stats = mod.rewrite_request({"system_prompt": "其他文字"}, old, new)
        self.assertEqual(stats["unmatched_templates"], ["test/zh-Hans"])

    def test_shared_span_conflict_rejected(self):
        old = [{"templateKey": k, "templateText": {"zh-Hans": "共用片段"}} for k in ["a", "b"]]
        new = [{"templateKey": k, "templateText": {"zh-Hans": v}} for k, v in [("a", "一个"), ("b", "另一个")]]
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            mod.compile_rules(old, new)

    def test_short_literal_word_boundaries(self):
        old = [{"templateKey": "name", "templateText": {"zh-Hans": "道侣"}}]
        new = [{"templateKey": "name", "templateText": {"zh-Hans": "伴侣"}}]
        after, _ = mod.rewrite_request({"system_prompt": "关系：道侣\n道侣关系"}, old, new)
        self.assertEqual(after["system_prompt"], "关系：伴侣\n道侣关系")


class Integration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.seen = []
        self.release = threading.Event()
        owner = self

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.seen.append((self.path, request, self.headers.get("Authorization")))
                if request.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
                    self.wfile.flush()
                    owner.release.wait(3)
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                else:
                    raw = b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
                    self.send_response(429 if request.get("fail") else 200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.up_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.up_thread.start()
        # Reserve a free port long enough to prepare the production config.
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.config = {"listen_port": self.port,
                       "upstream_url": f"http://127.0.0.1:{self.upstream.server_port}/v1",
                       "log_requests": True, "timeout_seconds": 5}
        mod.write_json(self.root / "config.json", self.config)
        mod.write_json(self.root / "templates.original.json", ORIGINAL)
        seed_files(self.root, ORIGINAL)
        self.proxy = mod.make_server(self.root)
        self.thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.release.set()
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.temp.cleanup()

    def request(self, body):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        client.request("POST", "/v1/chat/completions", json.dumps(body),
                       {"Content-Type": "application/json", "Authorization": "Bearer secret-test-key"})
        return client, client.getresponse()

    def test_normal_and_hot_reload_logs(self):
        edited, source = edited_rules()
        body = {"model": "test", "messages": [{"role": "system", "content": source}]}
        client, response = self.request(body)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["choices"][0]["message"]["content"], "ok")
        client.close()
        self.assertEqual(self.seen[-1][1], body)
        seed_files(self.root, edited)
        client, response = self.request(body)
        response.read()
        client.close()
        self.assertIn("测试规则", self.seen[-1][1]["messages"][0]["content"])
        self.assertEqual(self.seen[-1][0], "/v1/chat/completions")
        self.assertEqual(self.seen[-1][2], "Bearer secret-test-key")
        for file in (self.root / "logs").rglob("*.json"):
            self.assertNotIn("secret-test-key", file.read_text(encoding="utf-8"))
        logs = list((self.root / "logs").glob("*/replacement.json"))
        self.assertEqual(len(logs), 2)
        self.assertTrue(any(mod.read_json(x)["matched_templates"] for x in logs))

    def test_stream_first_event_arrives_before_completion(self):
        client, response = self.request({"messages": [], "stream": True})
        first = response.readline()
        self.assertIn(b'"content":"ok"', first)
        self.assertFalse(self.release.is_set())
        self.release.set()
        self.assertEqual(first + response.read(), b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')
        client.close()

    def test_upstream_errors_preserved(self):
        client, response = self.request({"fail": True})
        self.assertEqual(response.status, 429)
        self.assertIn(b'"choices"', response.read())
        client.close()

    def test_invalid_edit_prevents_forwarding(self):
        path = mod.prompt_path(self.root, "task.chat", "zh-Hans")
        path.write_text("错误修改，删掉了占位符", encoding="utf-8")
        client, response = self.request({"system_prompt": "test"})
        self.assertEqual(response.status, 422)
        response.read()
        self.assertEqual(self.seen, [])
        client.close()

    def test_direct_txt_changes_used_on_next_request(self):
        _, source = edited_rules()
        path = mod.prompt_path(self.root, "task.chat", "zh-Hans")
        body = {"messages": [{"role": "system", "content": source}]}
        for marker in ["第一版来自文件", "第二版即时生效"]:
            path.write_text(source.replace("## 任务：闲聊", "## 任务：闲聊\n" + marker), encoding="utf-8")
            client, response = self.request(body)
            self.assertEqual(response.status, 200)
            response.read()
            client.close()
            self.assertIn(marker, self.seen[-1][1]["messages"][0]["content"])
        logs = list((self.root / "logs").glob("*/replacement.json"))
        self.assertTrue(all(mod.read_json(x)["prompt_source"] == "text_files" for x in logs))
        self.assertTrue(all(mod.read_json(x)["edited_files"] == ["prompts/zh-Hans/task.chat.txt"] for x in logs))

    def test_extra_instruction_file_hot_reload_overrides_legacy_config(self):
        self.config["extra_system_instructions"] = "旧配置内容"
        mod.write_json(self.root / "config.json", self.config)
        path = self.root / "extra_system_instructions.txt"
        for extra in ["文件附加规则", ""]:
            path.write_text(extra, encoding="utf-8")
            client, response = self.request({"system_prompt": "原始系统信息"})
            self.assertEqual(response.status, 200)
            response.read()
            client.close()
            self.assertEqual(self.seen[-1][1]["system_prompt"], "原始系统信息" + ("\n\n" + extra if extra else ""))


    def test_health_and_loop_prevention(self):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        client.request("GET", "/health")
        response = client.getresponse()
        self.assertTrue(json.loads(response.read())["ok"])
        client.close()
        self.config["upstream_url"] = f"http://127.0.0.1:{self.port}/v1"
        mod.write_json(self.root / "config.json", self.config)
        with self.assertRaisesRegex(ValueError, "points back"):
            mod.load_config(self.root)


class FilePrompts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        mod.write_json(self.root / "templates.original.json", ORIGINAL)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_file_uses_original_and_ignores_legacy_json(self):
        edited, _ = edited_rules()
        mod.write_json(self.root / "templates.json", edited)
        self.assertEqual(mod.load_prompt_files(self.root), ORIGINAL)

    def test_utf8_bom_crlf_and_end_newline(self):
        edited, source = edited_rules()
        text = next(r for r in edited if r["templateKey"] == "task.chat")["templateText"]["zh-Hans"]
        path = mod.prompt_path(self.root, "task.chat", "zh-Hans")
        path.parent.mkdir(parents=True)
        path.write_bytes(b'\xef\xbb\xbf' + (text + "\n").replace("\n", "\r\n").encode("utf-8"))
        loaded = mod.load_prompt_files(self.root)
        after, _ = mod.rewrite_request({"system_prompt": source}, ORIGINAL, loaded)
        self.assertIn("测试规则", after["system_prompt"])
        self.assertNotIn("\ufeff", after["system_prompt"])

    def test_path_traversal_rejected(self):
        for key, language in [("../escape", "zh-Hans"), ("task.chat", "../../outside")]:
            with self.assertRaisesRegex(ValueError, "filename"):
                mod.prompt_path(self.root, key, language)

    def test_empty_file_removes_nonplaceholder_template(self):
        key = "relation.enemy.desc"
        path = mod.prompt_path(self.root, key, "zh-Hans")
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        source = mod.table(ORIGINAL)[key]["zh-Hans"]
        after, stats = mod.rewrite_request({"system_prompt": source}, ORIGINAL, mod.load_prompt_files(self.root))
        self.assertEqual(after["system_prompt"], "")
        self.assertIn(key + "/zh-Hans", stats["matched_templates"])



if __name__ == "__main__":
    unittest.main()
