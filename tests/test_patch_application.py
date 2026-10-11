"""Offline regression checks for the production patch application helper."""

import shutil
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PatchApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tdjson-patch-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        (self.root / "patches").mkdir()
        self.helper = self.root / "scripts" / "apply-mithka-patches.sh"
        shutil.copyfile(ROOT / "scripts" / "apply-mithka-patches.sh", self.helper)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "-b", "master")
        self.git("config", "user.name", "Patch regression test")
        self.git("config", "user.email", "patch-test@example.invalid")
        self.file = self.source / "example.cpp"
        self.file.write_text(
            "preamble\n" * 10 + "void run() {\n  work();\n}\n" + "after\n" * 10
        )
        self.git("add", "example.cpp")
        self.git("commit", "-m", "Synthetic patch base")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.source), *args],
            check=True, capture_output=True, text=True,
        ).stdout

    def install_patch(self, patch):
        (self.root / "patches" / "example.patch").write_text(patch)
        (self.root / "patches" / "series").write_text("example.patch\n")

    def apply(self):
        return subprocess.run(
            ["bash", str(self.helper), str(self.source)],
            capture_output=True, text=True,
        )

    def test_context_patch_applies_after_unrelated_line_shift_and_is_idempotent(self):
        original = self.file.read_text()
        self.file.write_text(original.replace("  work();", "  extra();\n  work();"))
        patch = self.git("diff")
        self.file.write_text("unrelated prefix\n" + original)
        self.install_patch(patch)
        first = self.apply()
        self.assertEqual(first.returncode, 0, first.stderr)
        expected = "unrelated prefix\n" + original.replace(
            "  work();", "  extra();\n  work();"
        )
        self.assertEqual(self.file.read_text(), expected)
        second = self.apply()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already applied", second.stdout)
        self.assertEqual(self.file.read_text(), expected)

    def test_unanchored_insertion_is_not_applied_to_a_stale_line(self):
        self.install_patch(
            "--- a/example.cpp\n+++ b/example.cpp\n"
            "@@ -2,0 +3 @@\n+  extra();\n"
        )
        original = self.file.read_text()
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.file.read_text(), original)

    def test_missing_series_member_fails_without_mutating_source(self):
        (self.root / "patches" / "series").write_text("missing.patch\n")
        original = self.file.read_text()
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stderr)
        self.assertEqual(self.file.read_text(), original)

    def test_series_is_idempotent_when_a_later_patch_edits_earlier_context(self):
        original = self.file.read_text()
        first = original.replace("  work();", "  extra();\n  work();")
        self.file.write_text(first)
        self.install_patch(self.git("diff"))
        self.git("add", "example.cpp")
        self.git("commit", "-m", "First synthetic patch")
        final = first.replace("  extra();", "  extra();\n  later();")
        self.file.write_text(final)
        (self.root / "patches" / "later.patch").write_text(self.git("diff"))
        (self.root / "patches" / "series").write_text(
            "example.patch\nlater.patch\n"
        )
        status_before = self.git("status", "--porcelain")
        real_index_before = (self.source / ".git" / "index").read_bytes()
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already applied", result.stdout)
        self.assertEqual(self.file.read_text(), final)
        self.assertEqual(self.git("status", "--porcelain"), status_before)
        self.assertEqual(
            (self.source / ".git" / "index").read_bytes(), real_index_before
        )

    def test_all_current_modification_hunks_have_real_context(self):
        names = (ROOT / "patches" / "series").read_text().splitlines()
        for name in names:
            if not name or name.startswith("#"):
                continue
            lines = (ROOT / "patches" / name).read_text().splitlines()
            hunks = [i for i, line in enumerate(lines) if line.startswith("@@ ")]
            self.assertTrue(hunks, name)
            for index, start in enumerate(hunks):
                end = hunks[index + 1] if index + 1 < len(hunks) else len(lines)
                body = []
                for line in lines[start + 1:end]:
                    if line.startswith("diff --git "):
                        break
                    body.append(line)
                self.assertRegex(lines[start], r"^@@ -\d+(,\d+)? \+\d+(,\d+)? @@")
                self.assertTrue(
                    any(line.startswith(" ") for line in body),
                    f"{name}: unanchored hunk {lines[start]}",
                )

    def test_windows_generator_follows_the_installed_generation(self):
        script = (ROOT / "scripts" / "build-desktop-lib.sh").read_text()
        start = script.index("    vs_generator='Visual Studio 17 2022'")
        end = script.index('    cmake -S "$TD_SOURCE"', start)
        selector = script[start:end]
        directory = self.root / "vs18"
        command = (
            "cygpath() { printf '%s' " + shlex.quote(str(directory)) + "; }\n"
            + selector + "\nprintf '%s' \"$vs_generator\""
        )
        def selected():
            return subprocess.run(
                ["bash", "-c", command], check=True, capture_output=True, text=True,
            ).stdout
        self.assertEqual(selected(), "Visual Studio 17 2022")
        directory.mkdir()
        self.assertEqual(selected(), "Visual Studio 18 2026")


if __name__ == "__main__":
    unittest.main()
