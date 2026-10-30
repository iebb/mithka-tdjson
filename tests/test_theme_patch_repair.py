"""Offline boundaries for the fixed-scope free-model conflict proposer."""

import importlib.util
import contextlib
import io
import json
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "theme_repair", ROOT / "scripts/repair-theme-patch.py"
)
repair = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = repair
SPEC.loader.exec_module(repair)

PATH = "td/telegram/ThemeManager.h"
SOURCE = b"// outside prefix\n<<<<<<< ours\n  void profile_share();\n=======\n  void installed_cloud();\n>>>>>>> theirs\n// outside suffix\n"


class FakeResponse(io.BytesIO):
    pass


def response(content, **extra):
    payload = {
        "model": "example/model:free",
        "usage": {"cost": 0},
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
    }
    payload.update(extra)
    return FakeResponse(json.dumps(payload).encode())


class AdditiveRepairTests(unittest.TestCase):
    def setUp(self):
        self.conflicts = repair.conflicts_in(PATH, SOURCE)
        self.value = "  void profile_share();\n\n  void installed_cloud();\n"

    def replacement_content(self, value=None):
        return json.dumps(
            {
                "resolutions": [
                    {"id": "c1", "replacement": self.value if value is None else value}
                ]
            }
        )

    def content(self, order="left_then_right"):
        return json.dumps({"resolutions": [{"id": "c1", "order": order}]})

    def test_free_http_request_and_exact_additive_replacement_preserve_outside_bytes(
        self,
    ):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return response(self.content())

        result, metadata = repair.ask_free_model(
            self.conflicts, "fake-test-secret", opener=opener
        )
        request, timeout = calls[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "openrouter/free")
        self.assertEqual(
            payload["provider"]["max_price"], {"prompt": 0, "completion": 0}
        )
        self.assertEqual(payload["provider"]["data_collection"], "deny")
        self.assertTrue(payload["provider"]["require_parameters"])
        self.assertEqual(timeout, 60)
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertNotIn("fake-test-secret", request.data.decode())
        output = repair.replace_hunks(SOURCE, self.conflicts, result)
        self.assertEqual(
            output,
            b"// outside prefix\n" + self.value.encode() + b"// outside suffix\n",
        )
        self.assertEqual(
            metadata,
            {"model_ids": ["example/model:free"], "zero_cost": True, "attempts": 1},
        )

    def test_model_cannot_edit_outside_hunks_drop_code_or_insert_markers_commands_or_binary(
        self,
    ):
        invalid = [
            self.value + "// outside suffix changed\n",
            "  void profile_share();\n",
            "  void installed_cloud();\n  void profile_share(int changed);\n",
            self.value + 'system("curl secret");\n',
            self.value + "<<<<<<< marker\n",
            self.value + "\0\n",
            self.value.rstrip("\n"),
        ]
        for value in invalid:
            with self.subTest(value=repr(value)), self.assertRaises(repair.RepairError):
                repair.validate_replacements(
                    self.replacement_content(value), self.conflicts
                )
        reversed_result = repair.validate_replacements(
            self.replacement_content(
                "  void installed_cloud();\n  void profile_share();\n"
            ),
            self.conflicts,
        )
        self.assertIn("c1", reversed_result)

    def test_strict_response_schema_rejects_missing_extra_duplicate_and_wrong_ids(self):
        invalid = [
            "{}",
            '{"resolutions": [], "commands": []}',
            '{"resolutions": []}',
            self.content().replace('"c1"', '"other"'),
            '{"resolutions": [], "resolutions": []}',
            self.content("drop_right"),
            self.content(None),
            self.content(1),
            self.replacement_content(),
            json.dumps(
                {
                    "resolutions": [
                        {
                            "id": "c1",
                            "order": "left_then_right",
                            "replacement": self.value,
                        }
                    ]
                }
            ),
            json.dumps({"resolutions": [{"id": "c1", "order": "left_then_right"}] * 2}),
        ]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(repair.RepairError):
                repair.validate_orders(content, self.conflicts)
        reversed_result = repair.validate_orders(
            self.content("right_then_left"), self.conflicts
        )
        self.assertEqual(
            reversed_result["c1"],
            self.conflicts[0].right + b"\n" + self.conflicts[0].left,
        )

    def test_conflict_paths_symlinks_nested_and_truncated_markers_fail_closed(self):
        for path, data in [
            ("../ThemeManager.h", SOURCE),
            ("td/telegram/Requests.cpp", SOURCE),
            (PATH, SOURCE.replace(b">>>>>>> theirs\n", b"")),
            (PATH, SOURCE.replace(b"  void profile_share();", b"<<<<<<< nested")),
            (PATH, SOURCE + b"=======\n"),
            (PATH, SOURCE + b"\0"),
        ]:
            with (
                self.subTest(path=path, data=data),
                self.assertRaises(repair.RepairError),
            ):
                repair.conflicts_in(path, data)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / PATH
            target.parent.mkdir(parents=True)
            outside = root / "other.txt"
            outside.write_text("private sentinel")
            target.symlink_to(outside)
            with self.assertRaises(repair.RepairError):
                repair.plain_file(root, PATH)

    def test_paid_unknown_cost_truncated_and_tool_responses_are_never_accepted(self):
        invalid = [
            {"model": "example/paid"},
            {"usage": {"cost": 0.01}},
            {"usage": {}},
            {"usage": {"cost": False}},
            {
                "choices": [
                    {"finish_reason": "length", "message": {"content": self.content()}}
                ]
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": self.content(), "tool_calls": [{}]},
                    }
                ]
            },
        ]
        for envelope in invalid:
            count = []

            def opener(request, timeout):
                count.append(request)
                return response(self.content(), **envelope)

            with (
                self.subTest(envelope=envelope),
                self.assertRaises(repair.RepairError),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                repair.ask_free_model(
                    self.conflicts, "fake", opener=opener, sleeper=lambda _: None
                )
            self.assertEqual(len(count), repair.ATTEMPTS)

    def test_request_and_response_sizes_are_bounded_without_logging_provider_errors(
        self,
    ):
        too_large = repair.Conflict(
            "c1", PATH, 0, 1, b"x" * repair.MAX_REQUEST, b"y\n", b"", b""
        )
        with self.assertRaises(repair.RepairError):
            repair.request_payload([too_large])
        calls = []

        def opener(request, timeout):
            calls.append(request)
            raise repair.URLError("provider detail containing fake-secret")

        diagnostics = io.StringIO()
        with (
            self.assertRaises(repair.RepairError) as caught,
            contextlib.redirect_stderr(diagnostics),
        ):
            repair.ask_free_model(
                self.conflicts, "fake-secret", opener=opener, sleeper=lambda _: None
            )
        self.assertEqual(len(calls), repair.ATTEMPTS)
        self.assertNotIn("fake-secret", str(caught.exception))
        self.assertNotIn("fake-secret", diagnostics.getvalue())
        self.assertNotIn("provider detail", diagnostics.getvalue())
        self.assertEqual(diagnostics.getvalue().count("URLError"), repair.ATTEMPTS)
        self.assertEqual(
            repair.safe_diagnostic(repair.RepairError("fake-secret")),
            "Deterministic repair boundary rejected the proposal",
        )
        self.assertEqual(
            repair.safe_diagnostic(
                repair.RepairError("Provider returned an incomplete response")
            ),
            "Provider returned an incomplete response",
        )
        error = repair.HTTPError(
            "https://example.invalid/private", 429, "fake-secret", {}, None
        )
        try:
            self.assertEqual(repair.safe_diagnostic(error), "HTTP status 429")
        finally:
            error.close()

    def test_complete_series_repair_replays_and_keeps_original_checkout_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            packaging, source = root / "packaging", root / "source"
            (packaging / "patches").mkdir(parents=True)
            (packaging / "scripts").mkdir()
            shutil.copyfile(
                ROOT / "scripts/apply-mithka-patches.sh",
                packaging / "scripts/apply-mithka-patches.sh",
            )
            source.mkdir()

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(source), *args], stderr=subprocess.PIPE
                )

            git("init", "-b", "master")
            git("config", "user.name", "Offline repair test")
            git("config", "user.email", "repair@example.invalid")
            git("config", "commit.gpgSign", "false")
            original = {
                "td/generate/scheme/td_api.tl": "// prefix\nthemeSettings value:int32 = ThemeSettings;\n\n\n// rich message\n// suffix\n",
                "td/telegram/ThemeManager.h": "class ThemeManager {\n  void gifts();\n\n  void current_state();\n};\n",
                "td/telegram/Requests.cpp": "// prefix\nvoid requests() {}\n// suffix\n",
                "td/telegram/Requests.h": "// prefix\nvoid requests();\n// suffix\n",
                "td/telegram/ThemeManager.cpp": "// prefix\nvoid themes() {}\n// suffix\n",
                "backup.cpp": "// before\nvoid backup() {}\n// after\n",
                "boost.cpp": "// before\nvoid boost() {}\n// after\n",
            }
            for path, text in original.items():
                file = source / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text)
            git("add", "--all")
            git("commit", "-m", "Original synthetic upstream")

            def patch_for(name, updates):
                for path, text in updates.items():
                    (source / path).write_text(text)
                (packaging / "patches" / name).write_bytes(git("diff", "--full-index"))
                for path in updates:
                    (source / path).write_text(original[path])

            patch_for(
                repair.SERIES[0],
                {
                    "backup.cpp": original["backup.cpp"].replace(
                        "void backup()", "void saved_backup()"
                    )
                },
            )
            cloud = dict(
                (path, text)
                for path, text in original.items()
                if path in repair.CLOUD_FILES
            )
            cloud["td/generate/scheme/td_api.tl"] = cloud[
                "td/generate/scheme/td_api.tl"
            ].replace(
                "// rich message",
                "installedCloudTheme slug:string title:string = InstalledCloudTheme;\ninstalledCloudThemes themes:vector<installedCloudTheme> = InstalledCloudThemes;\ngetInstalledCloudThemes theme_format:string = InstalledCloudThemes;\n\n// rich message",
            )
            cloud["td/telegram/ThemeManager.h"] = cloud[
                "td/telegram/ThemeManager.h"
            ].replace(
                "  void current_state();",
                "  void get_installed_cloud_themes();\n\n  void current_state();",
            )
            for path in (
                "td/telegram/Requests.cpp",
                "td/telegram/Requests.h",
                "td/telegram/ThemeManager.cpp",
            ):
                cloud[path] = cloud[path].replace(
                    "// suffix", "// Mithka retained addition\n// suffix"
                )
            patch_for(repair.TARGET, cloud)
            patch_for(
                repair.SERIES[2],
                {
                    "boost.cpp": original["boost.cpp"].replace(
                        "void boost()", "void saved_boost()"
                    )
                },
            )
            (packaging / "patches/series").write_text("\n".join(repair.SERIES) + "\n")
            upstream_schema = original["td/generate/scheme/td_api.tl"].replace(
                "// rich message",
                "profileShareThemeBackdrop id:int32 = ProfileShareThemeBackdrop;\n\n// rich message",
            )
            upstream_header = original["td/telegram/ThemeManager.h"].replace(
                "  void current_state();",
                "  void get_profile_share_theme_backdrops();\n  void set_profile_share_theme();\n\n  void current_state();",
            )
            (source / "td/generate/scheme/td_api.tl").write_text(upstream_schema)
            (source / "td/telegram/ThemeManager.h").write_text(upstream_header)
            git("add", "--all")
            git("commit", "-m", "Upstream additive profile-share theme support")
            original_head = git("rev-parse", "HEAD").decode().strip()
            provider_calls = []

            def model(conflicts, key):
                def opener(request, timeout):
                    provider_calls.append(json.loads(request.data))
                    value = {
                        "resolutions": [
                            {
                                "id": item.id,
                                "order": "left_then_right",
                            }
                            for item in conflicts
                        ]
                    }
                    return response(json.dumps(value))

                return repair.ask_free_model(
                    conflicts, key, opener=opener, sleeper=lambda _: None
                )

            output = root / "output"
            report = repair.repair(source, output, "fake", root=packaging, model=model)
            self.assertEqual(len(provider_calls), 1)
            self.assertEqual(report["upstream_sha"], original_head)
            proposed = (output / repair.TARGET).read_bytes()
            self.assertEqual(report["repaired_patch_sha256"], repair.sha256(proposed))
            self.assertEqual(report["zero_cost"], True)
            self.assertEqual(git("status", "--porcelain"), b"")
            self.assertEqual(git("rev-parse", "HEAD").decode().strip(), original_head)
            self.assertEqual(
                (source / "td/generate/scheme/td_api.tl").read_text(), upstream_schema
            )
            self.assertEqual(
                (source / "td/telegram/ThemeManager.h").read_text(), upstream_header
            )
            # Installing the validated proposal makes the next run a clean
            # no-op: no provider request, unchanged patch bytes, still replayed.
            (packaging / "patches" / repair.TARGET).write_bytes(proposed)

            def no_model(*_):
                self.fail("A clean series must not call the model")

            noop_output = root / "noop-output"
            noop = repair.repair(
                source, noop_output, "", root=packaging, model=no_model
            )
            self.assertEqual(noop["model_ids"], [])
            self.assertEqual(noop["attempts"], 0)
            self.assertTrue(noop["zero_cost"])
            self.assertEqual(
                noop["original_patch_sha256"], noop["repaired_patch_sha256"]
            )
            self.assertEqual((noop_output / repair.TARGET).read_bytes(), proposed)
            self.assertEqual(git("status", "--porcelain"), b"")


if __name__ == "__main__":
    unittest.main()
