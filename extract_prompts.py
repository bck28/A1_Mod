"""Extract this game's shipped AI prompt table without modifying game files."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import uuid

ROOT = Path(__file__).resolve().parent
MAX_PAYLOAD = 512 * 1024 * 1024
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ASSET = "Assets/Luban/Data/tbaiprompttemplate.json"


class Reader:
    def __init__(self, data, pos=0):
        if not 0 <= pos <= len(data):
            raise ValueError("Invalid UnityFS data offset")
        self.data, self.pos = data, pos

    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError("Truncated UnityFS data")
        part = self.data[self.pos:self.pos+n]
        self.pos += n
        return part

    def unpack(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))

    def string(self):
        end = self.data.find(b"\0", self.pos, self.pos + 4096)
        if end < 0:
            raise ValueError("Invalid UnityFS string")
        value = self.take(end-self.pos).decode("utf-8")
        self.take(1)
        return value


def lz4_block(src, expected):
    if not 0 <= expected <= MAX_PAYLOAD:
        raise ValueError("Invalid decompressed size")
    source = Reader(src)
    out = bytearray()

    def extended(n):
        if n == 15:
            while True:
                value = source.take(1)[0]
                n += value
                if value != 255:
                    break
        return n

    while source.pos < len(src):
        token = source.take(1)[0]
        count = extended(token >> 4)
        if len(out) + count > expected:
            raise ValueError("LZ4 output exceeds declared size")
        out.extend(source.take(count))
        if source.pos == len(src):
            break
        offset, = source.unpack("<H")
        if not offset or offset > len(out):
            raise ValueError("Invalid LZ4 match offset")
        count = extended(token & 15) + 4
        if len(out) + count > expected:
            raise ValueError("LZ4 output exceeds declared size")
        pattern = bytes(out[-offset:])
        out.extend((pattern * ((count + offset - 1) // offset))[:count])
    if len(out) != expected:
        raise ValueError("LZ4 decompressed size mismatch")
    return bytes(out)


def decompress(src, flags, expected):
    mode = flags & 63
    if mode == 0:
        if len(src) != expected:
            raise ValueError("Uncompressed size mismatch")
        return src
    if mode in (2, 3):
        return lz4_block(src, expected)
    raise ValueError(f"Unsupported bundle compression mode: {mode}")


def unpack_bundle(raw):
    if raw.startswith(b"A1BNDLHP"):
        reader = Reader(raw, 8)
        wrapper_version, wrapper_size, inner_size = reader.unpack("<IIQ")
        if wrapper_version != 2 or wrapper_size != 24 or inner_size != len(raw)-wrapper_size:
            raise ValueError("Unsupported or damaged A1 bundle wrapper")
        data = raw[wrapper_size:]
    else:
        wrapper_size = 0
        data = raw
    reader = Reader(data)
    if reader.take(8) != b"UnityFS\0":
        raise ValueError("Resource is not a supported UnityFS bundle")
    version, = reader.unpack(">I")
    if version not in (6, 7, 8):
        raise ValueError(f"Unsupported UnityFS version: {version}")
    reader.string()
    engine_version = reader.string()
    size, compressed_size, uncompressed_size, flags = reader.unpack(">QIII")
    if size != len(data) or not 0 < uncompressed_size <= 16 * 1024 * 1024:
        raise ValueError("Invalid UnityFS size")
    if version >= 7:
        reader.pos = (reader.pos + 15) // 16 * 16
    info_position = len(data)-compressed_size if flags & 128 else reader.pos
    info_reader = Reader(data, info_position)
    info = Reader(decompress(info_reader.take(compressed_size), flags, uncompressed_size), 16)
    block_position = reader.pos if flags & 128 else info_position+compressed_size
    if flags & 512:
        block_position = (block_position+15)//16*16
    block_count, = info.unpack(">I")
    if not 0 < block_count <= 16384:
        raise ValueError("Invalid UnityFS block count")
    blocks = []
    total = 0
    for _ in range(block_count):
        expanded, compressed, block_flags = info.unpack(">IIH")
        total += expanded
        if total > MAX_PAYLOAD:
            raise ValueError("Bundle exceeds 512 MiB extraction limit")
        blocks.append((expanded, compressed, block_flags))
    node_count, = info.unpack(">I")
    if not 0 < node_count <= 65536:
        raise ValueError("Invalid UnityFS node count")
    nodes = []
    for _ in range(node_count):
        offset, length, node_flags = info.unpack(">QQI")
        name = info.string()
        if offset+length > total:
            raise ValueError("UnityFS node exceeds payload")
        nodes.append((offset, length, name))
    block_reader = Reader(data, block_position)
    parts = [decompress(block_reader.take(n), flag, expanded) for expanded, n, flag in blocks]
    payload = b"".join(parts)
    return payload, nodes, {"unityfs_version": version, "engine_version": engine_version,
                            "wrapper_bytes": wrapper_size, "blocks": block_count,
                            "decompressed_bytes": len(payload)}


def validate_table(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("Prompt table must be a nonempty list")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"templateKey", "templateText"}:
            raise ValueError("Unsupported prompt table schema")
        key = row["templateKey"]
        if not isinstance(key, str) or not SAFE_NAME.fullmatch(key) or key in seen:
            raise ValueError("Invalid or duplicate templateKey")
        seen.add(key)
        texts = row["templateText"]
        if not isinstance(texts, dict) or not texts:
            raise ValueError("Invalid prompt translations")
        for language, text in texts.items():
            if not isinstance(language, str) or not SAFE_NAME.fullmatch(language) or not isinstance(text, str):
                raise ValueError("Invalid prompt language/text")
    return rows


def extract_table(payload, nodes):
    name = b"tbaiprompttemplate"
    pattern = struct.pack("<I", len(name)) + name
    found = []
    for node_offset, node_length, _ in nodes:
        data = memoryview(payload)[node_offset:node_offset+node_length].tobytes()
        position = 0
        while True:
            position = data.find(pattern, position)
            if position < 0:
                break
            end = (position + len(pattern) + 3)//4*4
            position += len(pattern)
            if end + 4 > len(data):
                continue
            length, = struct.unpack_from("<I", data, end)
            if not 0 < length <= 8 * 1024 * 1024 or end+4+length > len(data):
                continue
            try:
                rows = validate_table(json.loads(data[end+4:end+4+length].decode("utf-8-sig")))
            except (ValueError, UnicodeError):
                continue
            if rows not in found:
                found.append(rows)
    if len(found) != 1:
        raise ValueError(f"Expected one valid prompt TextAsset, found {len(found)}")
    return found[0]


def locate_bundle(game_dir):
    game = Path(game_dir).expanduser().resolve()
    packages = list(game.glob("*_Data/StreamingAssets/yoo/DefaultPackage"))
    if len(packages) != 1:
        raise ValueError("Game folder must contain exactly one *_Data/StreamingAssets/yoo/DefaultPackage")
    package = packages[0]
    version_file = package / "DefaultPackage.version"
    if version_file.exists():
        version = version_file.read_text(encoding="utf-8-sig").strip()
        if not SAFE_NAME.fullmatch(version):
            raise ValueError("Invalid package version")
        manifest_path = package / f"DefaultPackage_{version}.json"
    else:
        manifests = list(package.glob("DefaultPackage_*.json"))
        if len(manifests) != 1:
            raise ValueError("Cannot select manifest; DefaultPackage.version is missing")
        manifest_path = manifests[0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    assets = [a for a in manifest["AssetList"] if a.get("AssetPath") == ASSET]
    if len(assets) != 1:
        raise ValueError("Prompt template JSON asset missing or ambiguous in manifest")
    bundle_id = assets[0]["BundleID"]
    if type(bundle_id) is not int or not 0 <= bundle_id < len(manifest["BundleList"]):
        raise ValueError("Invalid prompt bundle index")
    bundle = manifest["BundleList"][bundle_id]
    digest = bundle["FileHash"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", digest):
        raise ValueError("Unsupported resource bundle hash")
    path = package / (digest + ".bundle")
    return game, manifest_path, path, bundle


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def export_templates(output, rows, overwrite=False, reset_extra=False):
    validate_table(rows)
    output = Path(output).resolve()
    baseline = output / "templates.original.json"
    old = validate_table(json.loads(baseline.read_bytes())) if baseline.exists() else []
    old_texts = {(r["templateKey"], lang): text for r in old for lang, text in r["templateText"].items()}
    report = {"created": [], "updated": [], "preserved_edits": [], "source_changed_for_edits": [],
              "extra_instructions_cleared": False}
    writes = []
    for row in rows:
        for language, text in row["templateText"].items():
            relative = Path("prompts") / language / (row["templateKey"] + ".txt")
            path = output / relative
            if not path.resolve().is_relative_to(output):
                raise ValueError("Template path escapes output directory")
            if not path.exists():
                report["created"].append(relative.as_posix())
                writes.append((path, text.encode("utf-8")))
                continue
            if overwrite:
                # Restore also works when a custom text file has invalid UTF-8.
                if path.read_bytes() != text.encode("utf-8"):
                    report["updated"].append(relative.as_posix())
                    writes.append((path, text.encode("utf-8")))
                continue
            current = path.read_text(encoding="utf-8-sig")
            if current == text:
                continue
            previous = old_texts.get((row["templateKey"], language))
            if current == previous:
                report["updated"].append(relative.as_posix())
                writes.append((path, text.encode("utf-8")))
            else:
                report["preserved_edits"].append(relative.as_posix())
                if previous is not None and previous != text:
                    report["source_changed_for_edits"].append(relative.as_posix())
    # Reject symlink escape and parse errors before writing any output.
    for fixed in (baseline, output / "extraction_report.json"):
        if not fixed.resolve().is_relative_to(output):
            raise ValueError("Output path escapes output directory")
    if reset_extra:
        extra_path = output / "extra_system_instructions.txt"
        if not extra_path.resolve().is_relative_to(output):
            raise ValueError("Extra instruction path escapes output directory")
        if not extra_path.exists() or extra_path.read_bytes():
            writes.append((extra_path, b""))
            report["extra_instructions_cleared"] = True
    baseline_data = json_bytes(rows)
    if baseline.exists() and json.loads(baseline.read_bytes()) != rows:
        writes.append((baseline, baseline_data))
    elif not baseline.exists():
        writes.append((baseline, baseline_data))
    prefix = "restore_" if reset_extra else "extraction_"
    backup = output / "backups" / (prefix + datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8])
    if not backup.resolve().is_relative_to(output):
        raise ValueError("Backup path escapes output directory")
    for path, _ in writes:
        if path.exists():
            atomic_write(backup / path.relative_to(output), path.read_bytes())
    report["backup_dir"] = str(backup) if backup.exists() else None
    for path, data in writes:
        atomic_write(path, data)
    return report


def restore_defaults(output=ROOT):
    output = Path(output).resolve()
    baseline = output / "templates.original.json"
    if not baseline.is_file():
        raise ValueError("缺少默认模板 templates.original.json，请先从游戏提取提示词")
    report_path = output / "restore_report.json"
    if not report_path.resolve().is_relative_to(output):
        raise ValueError("Restore report path escapes output directory")
    rows = validate_table(json.loads(baseline.read_bytes()))
    report = export_templates(output, rows, overwrite=True, reset_extra=True)
    report.update({"template_count": len(rows),
                   "text_file_count": sum(len(r["templateText"]) for r in rows),
                   "default_source": str(baseline), "game_files_modified": False})
    atomic_write(report_path, json_bytes(report))
    print(f"已恢复默认提示词：{report['text_file_count']} 个模板文件，附加系统规则已清空。")
    if report["backup_dir"]:
        print(f"修改前备份：{report['backup_dir']}")
    return report


def choose_game_directory(initial=""):
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise ValueError("无法打开目录选择窗口；请使用含 tkinter 的 Python，或指定 --game-dir") from exc
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        raise ValueError("无法打开目录选择窗口；可使用 --game-dir 指定游戏目录") from exc
    try:
        window.withdraw()
        window.attributes("-topmost", True)
        options = {"parent": window, "title": "选择《不问凡尘》游戏目录（包含 WorldApart_Data）",
                   "mustexist": True}
        if initial and Path(initial).is_dir():
            options["initialdir"] = str(Path(initial).resolve())
        return filedialog.askdirectory(**options)
    finally:
        window.destroy()


def run_extraction(game_dir, output=ROOT, overwrite=False):
    game, manifest, path, bundle = locate_bundle(game_dir)
    if path.stat().st_size > MAX_PAYLOAD:
        raise ValueError("Resource bundle exceeds 512 MiB extraction limit")
    raw = path.read_bytes()
    if bundle.get("FileSize") != len(raw):
        raise ValueError("Bundle size differs from selected manifest")
    print(f"读取资源包：{path}", flush=True)
    payload, nodes, details = unpack_bundle(raw)
    rows = extract_table(payload, nodes)
    output = Path(output).resolve()
    # Output must not overwrite shipped game resources, even if misconfigured.
    if any(output.is_relative_to(p.resolve()) or output == p.resolve() for p in game.glob("*_Data")):
        raise ValueError("Output must not be inside a game's *_Data resource directory")
    report = export_templates(output, rows, overwrite)
    report.update({"game_dir": str(game), "manifest": str(manifest), "bundle": str(path),
                   "bundle_sha256": hashlib.sha256(raw).hexdigest(), "package_bundle": bundle,
                   "template_count": len(rows), "text_file_count": sum(len(r["templateText"]) for r in rows),
                   "bundle_details": details, "game_files_modified": False})
    atomic_write(output / "extraction_report.json", json_bytes(report))
    print(f"提取完成：{report['template_count']} 个模板，{report['text_file_count']} 个文本文件。")
    print(f"创建 {len(report['created'])}，更新 {len(report['updated'])}，保留用户修改 {len(report['preserved_edits'])}。")
    print(f"模板目录：{output / 'prompts'}")
    if report["source_changed_for_edits"]:
        print("游戏原文发生变化，以下已编辑文件需检查兼容性：")
        for name in report["source_changed_for_edits"]:
            print("  " + name)
    return report


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="从不问凡尘安装目录提取提示词；默认保留已编辑文件")
    parser.add_argument("--game-dir", help="游戏安装文件夹（包含 WorldApart.exe）")
    parser.add_argument("--output", type=Path, default=ROOT, help="输出项目文件夹，默认程序所在目录")
    parser.add_argument("--overwrite", action="store_true", help="备份并重置已有模板为提取的原文")
    parser.add_argument("--restore-defaults", action="store_true", help="从已提取的默认模板恢复全部提示词，并清空附加规则")
    args = parser.parse_args(argv)
    if args.restore_defaults:
        if args.game_dir or args.overwrite:
            parser.error("--restore-defaults 使用已提取的原文，不与 --game-dir/--overwrite 同时使用")
        restore_defaults(args.output)
        return
    configured = ""
    config = ROOT / "config.json"
    config_data = None
    if not args.game_dir and config.exists():
        config_data = json.loads(config.read_text(encoding="utf-8-sig"))
        configured = config_data.get("game_dir", "")
    game_dir = args.game_dir or choose_game_directory(configured)
    if not game_dir:
        print("已取消选择，未提取或修改文件。")
        return
    run_extraction(game_dir, args.output, args.overwrite)
    if config_data is not None and not args.game_dir:
        config_data["game_dir"] = str(Path(game_dir).resolve())
        atomic_write(config, json_bytes(config_data))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, IndexError, struct.error) as exc:
        print("提取失败：" + str(exc), file=sys.stderr)
        sys.exit(1)
