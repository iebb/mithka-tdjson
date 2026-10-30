import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

SPEC = importlib.util.spec_from_file_location(
    "publish", Path(__file__).resolve().parents[1] / "scripts/publish-theme-repair.py"
)
publish = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publish)
BASE, HEAD, COMMIT, UPSTREAM = (c * 40 for c in "abcd")


class PublicationTests(unittest.TestCase):
    def run_publisher(self, prs, branch=None, files=None):
        calls = []

        def api(path, method="GET", body=None):
            calls.append((path, method, body))
            if method == "GET":
                if path == "git/ref/heads/master":
                    return {"object": {"sha": BASE}}
                if path.startswith("pulls?"):
                    return prs
                if path == f"git/ref/heads/{publish.BRANCH}":
                    if branch is None:
                        raise subprocess.CalledProcessError(1, ["gh"])
                    return {"object": {"sha": branch}}
                if path.startswith("compare/"):
                    return {
                        "files": [{"filename": f} for f in files or [publish.PATCH]],
                        "total_commits": 1,
                        "status": "ahead",
                    }
                if path == f"git/commits/{BASE}":
                    return {"tree": {"sha": "e" * 40}}
            if path == "git/blobs":
                return {"sha": "f" * 40}
            if path == "git/trees":
                self.assertEqual(body["base_tree"], "e" * 40)
                self.assertEqual([x["path"] for x in body["tree"]], [publish.PATCH])
                return {"sha": "1" * 40}
            if path == "git/commits":
                return {"sha": COMMIT}
            if path == "pulls" or path.startswith("pulls/"):
                return {"html_url": "https://github.com/iebb/mithka-tdjson/pull/5"}
            if path.startswith("git/refs"):
                self.assertNotIn("master", path)
                self.assertFalse(body.get("force", False))
                return {}
            if path == "actions/workflows/sync-upstream.yml/dispatches":
                self.assertEqual(
                    body["inputs"], {"upstream_ref": UPSTREAM, "validate_only": "true"}
                )
                return None
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as temporary:
            old = Path.cwd()
            try:
                os.chdir(temporary)
                out = Path("repair-output")
                out.mkdir()
                content = b"reviewed patch bytes\n"
                (out / "mithka-installed-cloud-themes.patch").write_bytes(content)
                (out / "repair-report.json").write_text(
                    json.dumps(
                        {
                            "repaired_patch_sha256": hashlib.sha256(
                                content
                            ).hexdigest(),
                            "upstream_sha": UPSTREAM,
                            "policy": "additive-conflict-sides-only",
                            "zero_cost": True,
                        }
                    )
                )
                with (
                    patch.object(publish, "api", side_effect=api),
                    patch.dict(
                        os.environ,
                        {
                            "PACKAGING_SHA": BASE,
                            "VALIDATION_URL": "https://github.com/iebb/mithka-tdjson/actions/runs/1",
                        },
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    publish.main()
            finally:
                os.chdir(old)
        return calls

    def test_new_proposal_changes_one_patch_and_creates_one_pr(self):
        calls = self.run_publisher([])
        self.assertEqual(sum(p == "pulls" and m == "POST" for p, m, b in calls), 1)
        self.assertFalse(any("release" in p for p, m, b in calls))

    def test_existing_bot_proposal_updates_same_pr_and_preserves_manual_notes(self):
        body = f"Reviewer note before\n{publish.START}\nAgent proposal commit: `{HEAD}`\n{publish.END}\nReviewer note after"
        calls = self.run_publisher([{"number": 5, "body": body}], HEAD)
        self.assertFalse(any(p == "pulls" and m == "POST" for p, m, b in calls))
        updated = next(b["body"] for p, m, b in calls if p == "pulls/5")
        self.assertIn("Reviewer note before", updated)
        self.assertIn("Reviewer note after", updated)
        self.assertIn(COMMIT, updated)

    def test_manual_commit_to_the_same_patch_is_not_overwritten(self):
        body = f"{publish.START}\nAgent proposal commit: `{'0' * 40}`\n{publish.END}"
        with self.assertRaisesRegex(ValueError, "manual/unattested"):
            self.run_publisher([{"number": 5, "body": body}], HEAD)

    def test_malformed_managed_section_fails_even_when_branch_equals_master(self):
        for body in ("Reviewer text", publish.END + publish.START):
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(ValueError, "unmanaged review notes"),
            ):
                self.run_publisher([{"number": 5, "body": body}], BASE)


if __name__ == "__main__":
    unittest.main()
