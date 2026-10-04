import copy
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("a1_extract", ROOT / "extract_prompts.py")
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)
SAMPLE = [{"templateKey": "task.chat", "templateText": {"zh-Hans": "游戏原文 {VALUE}", "en-US": "Original {VALUE}"}}]


def text_asset(rows):
    name = b"tbaiprompttemplate"
    blob = struct.pack("<I", len(name)) + name
    blob += b"\0" * (-len(blob) % 4)
    script = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    return blob + struct.pack("<I", len(script)) + script


def bundle(rows, end_info=False):
    payload = text_asset(rows)
    info = bytes(16) + struct.pack(">IIIH", 1, len(payload), len(payload), 0)
    info += struct.pack(">IQQI", 1, 0, len(payload), 0) + b"asset\0"
    prefix = b"UnityFS\0" + struct.pack(">I", 8) + b"5.x.x\0" + b"2022.3.43f1\0"
    size = ((len(prefix) + 20 + 15)//16*16) + len(info) + len(payload)
    header = prefix + struct.pack(">QIII", size, len(info), len(info), 128 if end_info else 0)
    header += b"\0" * (-len(header) % 16)
    data = header + (payload + info if end_info else info + payload)
    return b"A1BNDLHP" + struct.pack("<IIQ", 2, 24, len(data)) + data


class Extraction(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_game(self, asset=True):
        game = self.root / "game"
        package = game / "WorldApart_Data/StreamingAssets/yoo/DefaultPackage"
        package.mkdir(parents=True)
        raw = bundle(SAMPLE)
        digest = "1" * 32
        (package / (digest + ".bundle")).write_bytes(raw)
        manifest = {"AssetList": [{"AssetPath": extract.ASSET if asset else "unknown", "BundleID": 0}],
                    "BundleList": [{"FileHash": digest, "FileSize": len(raw)}]}
        # A version file must select the live version, not a stale lexicographic match.
        (package / "DefaultPackage.version").write_text("2.1", encoding="utf-8")
        (package / "DefaultPackage_2.1.json").write_text(json.dumps(manifest), encoding="utf-8")
        (package / "DefaultPackage_1.0.json").write_text("{}", encoding="utf-8")
        return game

    def test_wrapper_uncompressed_and_end_info_supported(self):
        for end in (False, True):
            payload, nodes, info = extract.unpack_bundle(bundle(SAMPLE, end))
            self.assertEqual(extract.extract_table(payload, nodes), SAMPLE)
            self.assertEqual(info["wrapper_bytes"], 24)

    def test_lz4_overlap_and_truncated_or_oversize_rejected(self):
        self.assertEqual(extract.lz4_block(b'\x10a\x01\x00', 5), b'aaaaa')
        for src, expected in [(b'\x20a', 2), (b'\x10a\x01', 5), (b'\x10a\x00\x00', 5),
                              (b'\x10a\x01\x00', 4), (b'\xf0', 20)]:
            with self.assertRaises(ValueError):
                extract.lz4_block(src, expected)

    def test_schema_and_ambiguous_text_assets_rejected(self):
        with self.assertRaises(ValueError):
            extract.validate_table(SAMPLE + SAMPLE)
        bad = copy.deepcopy(SAMPLE)
        bad[0]["templateKey"] = "../escape"
        with self.assertRaises(ValueError):
            extract.validate_table(bad)
        other = copy.deepcopy(SAMPLE)
        other[0]["templateText"]["zh-Hans"] = "另一个模板"
        first = text_asset(SAMPLE)
        first += bytes(-len(first) % 4)
        data = first + text_asset(other)
        with self.assertRaisesRegex(ValueError, "found 2"):
            extract.extract_table(data, [(0, len(data), "node")])

    def test_version_select_and_full_extraction(self):
        game = self.make_game()
        output = self.root / "mod"
        report = extract.run_extraction(game, output)
        self.assertTrue(report["manifest"].endswith("DefaultPackage_2.1.json"))
        self.assertEqual(report["text_file_count"], 2)
        self.assertEqual(json.loads((output / "templates.original.json").read_bytes()), SAMPLE)
        self.assertFalse(report["game_files_modified"])
        self.assertEqual((output / "prompts/zh-Hans/task.chat.txt").read_text(encoding="utf-8"), "游戏原文 {VALUE}")

    def test_bad_manifest_does_not_create_output(self):
        game = self.make_game(asset=False)
        output = self.root / "mod"
        with self.assertRaisesRegex(ValueError, "missing"):
            extract.run_extraction(game, output)
        self.assertFalse(output.exists())
        manifest_path = game / "WorldApart_Data/StreamingAssets/yoo/DefaultPackage/DefaultPackage_2.1.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["AssetList"][0] = {"AssetPath": extract.ASSET, "BundleID": -1}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "bundle index"):
            extract.run_extraction(game, output)
        self.assertFalse(output.exists())

    def test_preserve_edit_update_unmodified_and_backup(self):
        output = self.root / "mod"
        extract.export_templates(output, SAMPLE)
        edited = output / "prompts/zh-Hans/task.chat.txt"
        edited.write_text("用户修改 {VALUE}", encoding="utf-8")
        updated = copy.deepcopy(SAMPLE)
        updated[0]["templateText"] = {"zh-Hans": "游戏新原文 {VALUE}", "en-US": "New original {VALUE}"}
        report = extract.export_templates(output, updated)
        self.assertEqual(edited.read_text(encoding="utf-8"), "用户修改 {VALUE}")
        self.assertEqual(len(report["source_changed_for_edits"]), 1)
        self.assertEqual((output / "prompts/en-US/task.chat.txt").read_text(encoding="utf-8"), "New original {VALUE}")
        extract.export_templates(output, updated, overwrite=True)
        self.assertEqual(edited.read_text(encoding="utf-8"), "游戏新原文 {VALUE}")
        backups = list((output / "backups").glob("extraction_*/prompts/zh-Hans/task.chat.txt"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "用户修改 {VALUE}")

    def test_repeat_does_not_rewrite_unchanged_files(self):
        output = self.root / "mod"
        extract.export_templates(output, SAMPLE)
        path = output / "prompts/zh-Hans/task.chat.txt"
        before = path.stat().st_mtime_ns
        report = extract.export_templates(output, SAMPLE)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(report["created"], [])
        self.assertEqual(report["updated"], [])

    def test_resource_output_rejected(self):
        game = self.make_game()
        with self.assertRaisesRegex(ValueError, "resource directory"):
            extract.run_extraction(game, game / "WorldApart_Data/mod_output")
        self.assertFalse((game / "WorldApart_Data/mod_output").exists())

    def test_restore_defaults_repairs_files_and_clears_extra_with_backup(self):
        output = self.root / "mod"
        extract.export_templates(output, SAMPLE)
        path = output / "prompts/zh-Hans/task.chat.txt"
        raw_edit = b'\xff\xfeinvalid UTF-8 edit'
        path.write_bytes(raw_edit)
        missing = output / "prompts/en-US/task.chat.txt"
        missing.unlink()
        extra = output / "extra_system_instructions.txt"
        extra.write_text("用户附加规则", encoding="utf-8")
        report = extract.restore_defaults(output)
        self.assertEqual(path.read_text(encoding="utf-8"), "游戏原文 {VALUE}")
        self.assertEqual(missing.read_text(encoding="utf-8"), "Original {VALUE}")
        self.assertEqual(extra.read_bytes(), b'')
        backup = Path(report["backup_dir"])
        self.assertEqual((backup / "prompts/zh-Hans/task.chat.txt").read_bytes(), raw_edit)
        self.assertEqual((backup / "extra_system_instructions.txt").read_text(encoding="utf-8"), "用户附加规则")
        self.assertEqual(report["text_file_count"], 2)
        self.assertEqual(json.loads((output / "templates.original.json").read_bytes()), SAMPLE)
        self.assertEqual(json.loads((output / "restore_report.json").read_bytes())["backup_dir"], str(backup))
        again = extract.restore_defaults(output)
        self.assertIsNone(again["backup_dir"])

    def test_restore_missing_or_invalid_defaults_does_not_touch_files(self):
        output = self.root / "mod"
        path = output / "prompts/zh-Hans/task.chat.txt"
        path.parent.mkdir(parents=True)
        path.write_text("保留修改", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "先从游戏提取"):
            extract.restore_defaults(output)
        (output / "templates.original.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "nonempty"):
            extract.restore_defaults(output)
        self.assertEqual(path.read_text(encoding="utf-8"), "保留修改")

    def test_main_popup_selection_remembers_valid_path(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"game_dir": "previous", "upstream_url": "keep"}), encoding="utf-8")
        with mock.patch.object(extract, "ROOT", self.root), \
             mock.patch.object(extract, "choose_game_directory", return_value=str(self.root)) as chooser, \
             mock.patch.object(extract, "run_extraction") as run:
            extract.main([])
            chooser.assert_called_once_with("previous")
            run.assert_called_once_with(str(self.root), self.root, False)
        saved = json.loads(config.read_bytes())
        self.assertEqual(saved["game_dir"], str(self.root.resolve()))
        self.assertEqual(saved["upstream_url"], "keep")

    def test_main_cancel_changes_nothing_and_explicit_cli_skips_popup(self):
        config = self.root / "config.json"
        before = b'{"game_dir":"previous"}'
        config.write_bytes(before)
        with mock.patch.object(extract, "ROOT", self.root), \
             mock.patch.object(extract, "choose_game_directory", return_value="") as chooser, \
             mock.patch.object(extract, "run_extraction") as run:
            extract.main([])
            chooser.assert_called_once()
            run.assert_not_called()
        self.assertEqual(config.read_bytes(), before)
        with mock.patch.object(extract, "ROOT", self.root), \
             mock.patch.object(extract, "choose_game_directory") as chooser, \
             mock.patch.object(extract, "run_extraction") as run:
            extract.main(["--game-dir", "explicit"])
            chooser.assert_not_called()
            run.assert_called_once_with("explicit", self.root, False)

    def test_failed_extraction_does_not_remember_bad_selection(self):
        config = self.root / "config.json"
        before = b'{"game_dir":"previous"}'
        config.write_bytes(before)
        with mock.patch.object(extract, "ROOT", self.root), \
             mock.patch.object(extract, "choose_game_directory", return_value="invalid"), \
             mock.patch.object(extract, "run_extraction", side_effect=ValueError("invalid")):
            with self.assertRaises(ValueError):
                extract.main([])
        self.assertEqual(config.read_bytes(), before)

    def test_restore_cli_does_not_open_game_chooser(self):
        with mock.patch.object(extract, "ROOT", self.root), \
             mock.patch.object(extract, "choose_game_directory") as chooser, \
             mock.patch.object(extract, "restore_defaults") as restore:
            extract.main(["--restore-defaults"])
            chooser.assert_not_called()
            restore.assert_called_once_with(self.root)

    def test_folder_dialog_cleanup_on_success_cancel_and_error(self):
        import tkinter as tk
        from tkinter import filedialog
        for answer in (str(self.root), ""):
            with mock.patch.object(tk, "Tk") as create, \
                 mock.patch.object(filedialog, "askdirectory", return_value=answer) as dialog:
                self.assertEqual(extract.choose_game_directory(str(self.root)), answer)
                self.assertTrue(dialog.call_args.kwargs["mustexist"])
                create.return_value.withdraw.assert_called_once()
                create.return_value.destroy.assert_called_once()
        with mock.patch.object(tk, "Tk") as create, \
             mock.patch.object(filedialog, "askdirectory", side_effect=RuntimeError("dialog failed")):
            with self.assertRaises(RuntimeError):
                extract.choose_game_directory()
            create.return_value.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
