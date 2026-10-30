import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

SPEC = importlib.util.spec_from_file_location(
    "merge", Path(__file__).resolve().parents[1] / "scripts/merge-theme-repair.py"
)
merge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge)
BASE, HEAD, UPSTREAM, MERGED = (c * 40 for c in "abcd")
ORIGINAL, REPAIRED = b"original patch\n", b"validated additive repair\n"


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.base = BASE
        self.head = HEAD
        self.final_read_head = HEAD
        self.reject_ref = False
        self.fields = {
            "Packaging base": BASE,
            "Agent proposal commit": HEAD,
            "Upstream": UPSTREAM,
            "Validated patch SHA-256": hashlib.sha256(REPAIRED).hexdigest(),
        }
        body = merge.render_body(
            BASE,
            UPSTREAM,
            self.fields["Validated patch SHA-256"],
            HEAD,
            f"https://github.com/{merge.REPO}/actions/runs/1",
        )
        self.pr = {
            "number": 6,
            "state": "open",
            "user": {"login": "github-actions[bot]"},
            "draft": False,
            "locked": False,
            "body": body,
            "head": {
                "sha": HEAD,
                "ref": merge.BRANCH,
                "repo": {"full_name": merge.REPO},
            },
            "base": {"sha": BASE, "ref": "master"},
            "mergeable": True,
            "mergeable_state": "clean",
            "merge_commit_sha": "f" * 40,
        }
        self.runs = {
            "1": self.make_run(BASE, "master", "repair-theme-patch.yml"),
            "2": self.make_run(HEAD, merge.BRANCH, "sync-upstream.yml"),
        }
        self.comparison = {
            "status": "ahead",
            "total_commits": 1,
            "files": [{"filename": merge.PATCH, "status": "modified"}],
        }
        self.jobs = {
            "1": [
                {"name": n, "conclusion": "success"} for n in ("propose", "validate")
            ],
            "2": [
                {"name": n, "conclusion": "success", "id": i}
                for i, n in enumerate(
                    ("Resolve upstream TDLib", "Validate Mithka patches")
                )
            ]
            + [{"name": "iOS XCFramework", "conclusion": "skipped"}],
        }
        self.report = {
            "policy": "additive-conflict-sides-only",
            "zero_cost": True,
            "upstream_sha": UPSTREAM,
            "original_patch_sha256": hashlib.sha256(ORIGINAL).hexdigest(),
            "repaired_patch_sha256": hashlib.sha256(REPAIRED).hexdigest(),
        }
        self.candidate = REPAIRED
        self.comments, self.reviews, self.threads = [], [], []
        self.duplicates, self.check_runs, self.statuses = [], [], []
        self.reads = 0

    @staticmethod
    def make_run(sha, branch, workflow):
        return {
            "status": "completed",
            "conclusion": "success",
            "head_sha": sha,
            "head_branch": branch,
            "head_repository": {"full_name": merge.REPO},
            "path": f".github/workflows/{workflow}",
            "event": "workflow_dispatch",
        }

    def api(self, path, method="GET", body=None, raw=False):
        self.calls.append((path, method, body))
        if path == "git/ref/heads/master":
            return {"object": {"sha": self.base}}
        if path.startswith("pulls?"):
            return [self.pr]
        if path == "pulls/6":
            self.reads += 1
            result = copy.deepcopy(self.pr)
            if self.reads > 1:
                result["head"]["sha"] = self.final_read_head
            if self.base == MERGED:
                result.update(merged=True, merge_commit_sha=MERGED)
            return result
        if path.startswith("compare/"):
            return self.comparison
        if path == f"git/commits/{HEAD}":
            return {"parents": [{"sha": BASE}], "tree": {"sha": "e" * 40}}
        if path.startswith("actions/runs/"):
            run_id = path.split("/")[2]
            return (
                {"jobs": self.jobs[run_id], "total_count": len(self.jobs[run_id])}
                if "/jobs?" in path
                else self.runs[run_id]
            )
        if path.startswith("actions/runs?"):
            return {
                "total_count": len(self.duplicates),
                "workflow_runs": self.duplicates,
            }
        if path.startswith("commits/"):
            if "/pulls?" in path:
                return [self.pr]
            if "/check-runs?" in path:
                return {
                    "total_count": len(self.check_runs),
                    "check_runs": self.check_runs,
                }
            return {"total_count": len(self.statuses), "statuses": self.statuses}
        if path.startswith("actions/jobs/"):
            return f"Upstream commit: {UPSTREAM}\n"
        if path.startswith("contents/"):
            content = ORIGINAL if path.endswith(BASE) else REPAIRED
            return {
                "type": "file",
                "encoding": "base64",
                "size": len(content),
                "content": base64.b64encode(content).decode(),
            }
        if path == "pulls/6/reviews?per_page=100":
            return self.reviews
        if path == "issues/6/comments?per_page=100":
            return self.comments
        if path == "git/commits" and method == "POST":
            self.assertEqual(body["parents"], [BASE, HEAD])
            self.assertEqual(body["tree"], "e" * 40)
            return {"sha": MERGED}
        if path == "git/refs/heads/master" and method == "PATCH":
            self.assertEqual(body, {"sha": MERGED, "force": False})
            if self.reject_ref:
                raise subprocess.CalledProcessError(1, ["gh"])
            self.base = MERGED
            return {}
        if path == "actions/workflows/sync-upstream.yml/dispatches":
            self.assertEqual(
                body,
                {
                    "ref": "master",
                    "inputs": {"upstream_ref": UPSTREAM, "validate_only": "false"},
                },
            )
            return None
        raise AssertionError(path)

    def execute(self, check_only=False, enabled=True):
        threads = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": self.threads,
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        }
        with (
            patch.object(merge, "api", side_effect=self.api),
            patch.object(merge, "request", return_value=threads),
            patch.object(
                merge, "artifact_report", return_value=(self.report, self.candidate)
            ),
            patch.dict(os.environ, {"TDLIB_REPAIR_AUTO_MERGE": str(enabled).lower()}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return merge.merge_repair("2", check_only)

    def assert_no_writes(self):
        self.assertFalse(any(method != "GET" for _, method, _ in self.calls))

    def test_attested_exact_head_merges_and_dispatches_pinned_native_build(self):
        self.assertEqual(self.execute(), MERGED)
        self.assertEqual(self.base, MERGED)

    def test_check_only_never_writes(self):
        self.execute(check_only=True)
        self.assert_no_writes()

    def pending_duplicate(self):
        self.duplicates = [
            {
                **self.make_run(HEAD, merge.BRANCH, "sync-upstream.yml"),
                "id": 3,
                "event": "pull_request",
                "status": "waiting",
                "conclusion": None,
                "pull_requests": [{"number": 6}],
                "check_suite_id": 123,
            }
        ]
        self.jobs["3"] = []
        self.check_runs = [
            {
                "status": "queued",
                "conclusion": None,
                "app": {"slug": "github-actions"},
                "check_suite": {"id": 123},
            }
        ]
        self.pr["mergeable_state"] = "unstable"

    def test_only_matching_zero_job_duplicate_can_remain_pending(self):
        self.pending_duplicate()
        self.assertEqual(self.execute(), MERGED)

    def test_omitted_run_pr_array_requires_positive_commit_association(self):
        self.pending_duplicate()
        self.duplicates[0]["pull_requests"] = []
        self.assertEqual(self.execute(), MERGED)

    def test_failed_sync_pr_workflow_blocks_even_without_check_runs(self):
        self.pending_duplicate()
        self.duplicates[0].update(status="completed", conclusion="failure")
        self.check_runs = []
        with self.assertRaises(ValueError):
            self.execute()
        self.assert_no_writes()

    def test_other_pending_or_failed_checks_are_not_exempted(self):
        self.pending_duplicate()
        for status, conclusion in [("queued", None), ("completed", "failure")]:
            self.check_runs.append(
                {
                    "status": status,
                    "conclusion": conclusion,
                    "app": {"slug": "github-actions"},
                    "check_suite": {"id": 456},
                }
            )
            with self.subTest(conclusion=conclusion), self.assertRaises(ValueError):
                self.execute()
            self.assert_no_writes()
            self.check_runs.pop()

    def test_duplicate_wrong_sha_pr_suite_or_started_jobs_blocks(self):
        for field, value in [
            ("head_sha", BASE),
            ("pull_requests", [{"number": 7}]),
            ("check_suite_id", 999),
        ]:
            self.pending_duplicate()
            self.duplicates[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.execute()
            self.assert_no_writes()
        self.pending_duplicate()
        self.jobs["3"] = [{"name": "started"}]
        with self.assertRaises(ValueError):
            self.execute()
        self.assert_no_writes()

    def test_pending_or_failed_legacy_status_blocks(self):
        for state in ["pending", "failure", "error"]:
            self.statuses = [{"state": state}]
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.execute()
            self.assert_no_writes()

    def test_disabled_auto_merge_never_writes(self):
        with self.assertRaisesRegex(ValueError, "not enabled"):
            self.execute(enabled=False)
        self.assert_no_writes()

    def test_changed_master_and_changed_head_stop_publication(self):
        for attribute in ("base", "final_read_head"):
            with self.subTest(attribute=attribute):
                setattr(self, attribute, "f" * 40)
                with self.assertRaises(ValueError):
                    self.execute()
                self.assert_no_writes()
                setattr(self, attribute, BASE if attribute == "base" else HEAD)

    def test_failed_skipped_or_wrong_source_preflight_cannot_merge(self):
        for key, value in (
            ("conclusion", "failure"),
            ("head_sha", BASE),
            ("event", "push"),
        ):
            with self.subTest(key=key):
                original = self.runs["2"][key]
                self.runs["2"][key] = value
                with self.assertRaises(ValueError):
                    self.execute()
                self.assert_no_writes()
                self.runs["2"][key] = original
        self.jobs["2"][1]["conclusion"] = "skipped"
        with self.assertRaises(ValueError):
            self.execute()
        self.assert_no_writes()

    def test_other_files_and_manual_commits_cannot_merge(self):
        self.comparison["files"].append(
            {"filename": "scripts/other.py", "status": "modified"}
        )
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.execute()
        self.assert_no_writes()

    def test_fork_or_human_authored_pr_cannot_merge(self):
        for key in ("fork", "author"):
            with self.subTest(key=key):
                saved = copy.deepcopy(self.pr)
                if key == "fork":
                    self.pr["head"]["repo"]["full_name"] = "other/repo"
                else:
                    self.pr["user"]["login"] = "iebb"
                with self.assertRaises(ValueError):
                    self.execute()
                self.assert_no_writes()
                self.pr = saved

    def test_stale_generation_and_changed_artifact_cannot_merge(self):
        self.runs["1"]["head_sha"] = HEAD
        with self.assertRaises(ValueError):
            self.execute()
        self.runs["1"]["head_sha"] = BASE
        self.candidate = b"manual same-file change\n"
        with self.assertRaisesRegex(ValueError, "differs"):
            self.execute()
        self.assert_no_writes()

    def test_paid_or_unbounded_proposals_cannot_merge(self):
        for key, value in (("zero_cost", False), ("policy", "arbitrary-rewrite")):
            with self.subTest(key=key):
                original = self.report[key]
                self.report[key] = value
                with self.assertRaises(ValueError):
                    self.execute()
                self.assert_no_writes()
                self.report[key] = original

    def test_human_feedback_or_unresolved_threads_block_merge(self):
        for attribute, value in (
            ("comments", [{"user": {"type": "User"}}]),
            ("reviews", [{"user": {"type": "Bot"}, "state": "CHANGES_REQUESTED"}]),
            ("threads", [{"isResolved": False}]),
        ):
            with self.subTest(attribute=attribute):
                setattr(self, attribute, value)
                with self.assertRaises(ValueError):
                    self.execute()
                self.assert_no_writes()
                setattr(self, attribute, [])

    def test_manual_notes_block_merge(self):
        self.pr["body"] += "\nDo not merge"
        with self.assertRaisesRegex(ValueError, "Manual PR notes"):
            self.execute()
        self.assert_no_writes()

    def test_manual_notes_or_prose_edits_inside_managed_block_block_merge(self):
        original = self.pr["body"]
        for body in (
            original.replace(
                merge.START, merge.START + "\nMaintainer note: do not merge."
            ),
            original.replace("## Change", "## Change - needs more review"),
        ):
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(ValueError, "Manually edited"),
            ):
                self.pr["body"] = body
                self.execute()
            self.assert_no_writes()

    def test_concurrent_master_update_fails_without_dispatch(self):
        self.reject_ref = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        self.assertEqual(self.base, BASE)
        self.assertFalse(any("dispatches" in path for path, _, _ in self.calls))


if __name__ == "__main__":
    unittest.main()
