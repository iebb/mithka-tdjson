#!/usr/bin/env python3
"""Merge only an attested, exact-head cloud-theme repair after native preflight."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from theme_repair_body import END, START, render_body

REPO = "iebb/mithka-tdjson"
BRANCH = "automation/tdlib-theme-patch"
PATCH = "patches/mithka-installed-cloud-themes.patch"
SHA = r"[0-9a-f]{40}"


def request(resource, method="GET", body=None, raw=False):
    result = subprocess.run(
        ["gh", "api", resource, "--method", method]
        # Logs contain ANSI escapes. Capture them for parsing; never render
        # raw log bodies or signed download URLs to the workflow terminal.
        + (["--allow-escape-sequences"] if raw else [])
        + (["--input", "-"] if body is not None else []),
        input=json.dumps(body) if body is not None else None,
        text=True,
        capture_output=True,
        check=True,
    )
    if len(result.stdout) > 2_000_000:
        raise ValueError("Oversized API response")
    return result.stdout if raw else json.loads(result.stdout or "null")


def api(path, method="GET", body=None, raw=False):
    return request(f"repos/{REPO}/{path}", method, body, raw)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run_identity(run, sha, branch, workflow):
    require(
        run["status"] == "completed"
        and run["conclusion"] == "success"
        and run["head_sha"] == sha
        and run["head_branch"] == branch
        and run["head_repository"]["full_name"] == REPO
        and run["path"] == f".github/workflows/{workflow}",
        "Run is unsuccessful, stale, or from another workflow/source",
    )


def metadata(pr):
    body = pr.get("body") or ""
    require(
        body.count(START) == body.count(END) == 1
        and body.index(START) < body.index(END),
        "Missing or malformed repair provenance",
    )
    before, managed = body.split(START)
    managed, after = managed.split(END)
    require(not (before + after).strip(), "Manual PR notes require human review")
    fields = {}
    for name, pattern in {
        "base": rf"Packaging base: `({SHA})`",
        "head": rf"Agent proposal commit: `({SHA})`",
        "upstream": rf"Upstream: `({SHA})`",
        "digest": r"Validated patch SHA-256: `([0-9a-f]{64})`",
        "generation": rf"\[Read-only generation and schema/source validation\]\(https://github.com/{REPO}/actions/runs/([0-9]+)\)",
    }.items():
        values = re.findall(pattern, managed)
        require(len(values) == 1, "Ambiguous or missing repair provenance")
        fields[name] = values[0]
    require(
        body
        == render_body(
            fields["base"],
            fields["upstream"],
            fields["digest"],
            fields["head"],
            f"https://github.com/{REPO}/actions/runs/{fields['generation']}",
        ),
        "Manually edited PR body requires human review",
    )
    return fields


def patch_bytes(sha):
    result = api(f"contents/{PATCH}?ref={sha}")
    require(
        result["type"] == "file"
        and result["encoding"] == "base64"
        and result["size"] <= 300_000,
        "Unexpected patch blob",
    )
    return base64.b64decode(result["content"])


def artifact_report(run_id):
    artifacts = api(f"actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
    found = [a for a in artifacts if a["name"] == "theme-repair"]
    require(
        len(found) == 1
        and not found[0]["expired"]
        and found[0]["size_in_bytes"] <= 1_000_000,
        "Missing or oversized generation artifact",
    )
    with tempfile.TemporaryDirectory() as temporary:
        subprocess.run(
            [
                "gh",
                "run",
                "download",
                str(run_id),
                "--repo",
                REPO,
                "--name",
                "theme-repair",
                "--dir",
                temporary,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        files = {p.name for p in Path(temporary).iterdir()}
        require(
            files == {"repair-report.json", Path(PATCH).name},
            "Unexpected artifact files",
        )
        patch = (Path(temporary) / Path(PATCH).name).read_bytes()
        report = json.loads((Path(temporary) / "repair-report.json").read_text())
    return report, patch


def review_clear(number):
    result = request(
        "graphql",
        "POST",
        {
            "query": """query($number:Int!) {
          repository(owner:"iebb", name:"mithka-tdjson") {
            pullRequest(number:$number) {
              reviewThreads(first:100) {
                pageInfo { hasNextPage }
                nodes { isResolved }
              }
            }
          }
        }""",
            "variables": {"number": number},
        },
    )
    require(not result.get("errors"), "Cannot verify review threads")
    threads = result["data"]["repository"]["pullRequest"]["reviewThreads"]
    require(
        not threads["pageInfo"]["hasNextPage"]
        and all(t["isResolved"] for t in threads["nodes"]),
        "Unresolved review threads require human review",
    )
    # Human feedback, even an edited review or discussion comment, takes the
    # automated lane out of scope. Never interpret natural language as approval.
    for path in (f"pulls/{number}/reviews", f"issues/{number}/comments"):
        entries = api(path + "?per_page=100")
        require(
            len(entries) < 100
            and all(e["user"]["type"] == "Bot" for e in entries)
            and all(e.get("state") != "CHANGES_REQUESTED" for e in entries),
            "PR feedback requires human review",
        )


def checks_clear(pr, head):
    # Job-token PR events can wait for human workflow approval. The publisher
    # explicitly dispatches the exact same trusted preflight; only its redundant
    # PR run may remain pending, never an unrelated check or a real failure.
    runs = api(f"actions/runs?head_sha={head}&event=pull_request&per_page=100")
    require(runs["total_count"] < 100, "Too many PR workflow runs to attest")
    redundant = set()
    for run in runs["workflow_runs"]:
        if run["status"] == "completed" and run["conclusion"] in (
            "success",
            "neutral",
            "skipped",
        ):
            continue
        numbers = [p["number"] for p in run["pull_requests"]]
        if not numbers:
            # GitHub sometimes omits the workflow run's pull_requests array.
            # Resolve the actual commit association rather than guessing.
            associated = api(f"commits/{head}/pulls?per_page=100")
            require(len(associated) < 100, "Too many associated PRs to attest")
            numbers = [
                p["number"]
                for p in associated
                if p["state"] == "open"
                and p["head"]["sha"] == head
                and p["head"]["ref"] == BRANCH
                and p["base"]["ref"] == "master"
            ]
        allowed = (
            run["head_sha"] == head
            and run["head_branch"] == BRANCH
            and run["head_repository"]["full_name"] == REPO
            and run["path"] == ".github/workflows/sync-upstream.yml"
            and run["event"] == "pull_request"
            and numbers == [pr["number"]]
            and (
                (
                    run["status"] in ("waiting", "requested", "pending", "queued")
                    and run["conclusion"] is None
                )
                or (
                    run["status"] == "completed"
                    and run["conclusion"] == "action_required"
                )
            )
        )
        require(allowed, "Failing, unknown, or unrelated pending PR workflow")
        jobs = api(f"actions/runs/{run['id']}/jobs?per_page=100")
        require(
            jobs["total_count"] == 0 and jobs["jobs"] == [],
            "Pending PR workflow already has jobs",
        )
        redundant.add(run["check_suite_id"])
    require(
        pr["mergeable_state"] == "clean" or redundant,
        "Nonpassing aggregate status is not an attested redundant PR run",
    )
    preview = pr.get("merge_commit_sha") or ""
    require(re.fullmatch(SHA, preview), "Cannot attest the synthetic merge checks")
    for sha in dict.fromkeys((head, preview)):
        checks = api(f"commits/{sha}/check-runs?per_page=100")
        require(checks["total_count"] < 100, "Too many commit checks to attest")
        for check in checks["check_runs"]:
            passed = check["status"] == "completed" and check["conclusion"] in (
                "success",
                "neutral",
                "skipped",
            )
            duplicate = (
                (
                    check["status"] != "completed"
                    and check["conclusion"] is None
                    or check["status"] == "completed"
                    and check["conclusion"] == "action_required"
                )
                and check["app"]["slug"] == "github-actions"
                and check["check_suite"]["id"] in redundant
            )
            require(
                passed or duplicate,
                "Failing, unknown, or unrelated pending commit check",
            )
        statuses = api(f"commits/{sha}/status?per_page=100")
        require(
            statuses["total_count"] < 100
            and all(s["state"] == "success" for s in statuses["statuses"]),
            "Failing or pending commit status",
        )


def merge_repair(run_id, check_only=False):
    require(str(run_id).isdigit(), "Invalid validation run ID")
    run = api(f"actions/runs/{run_id}")
    run_identity(run, run["head_sha"], BRANCH, "sync-upstream.yml")
    require(
        run["event"] == "workflow_dispatch", "Expected dispatched read-only preflight"
    )
    prs = api(f"pulls?state=open&head=iebb:{BRANCH}&base=master&per_page=100")
    if not prs:
        print("No open canonical repair PR; nothing to merge")
        return None
    require(len(prs) == 1, "Multiple canonical repair PRs require consolidation")
    pr = api(f"pulls/{prs[0]['number']}")
    fields = metadata(pr)
    base, head = fields["base"], fields["head"]
    require(
        pr["user"]["login"] == "github-actions[bot]"
        and not pr["draft"]
        and not pr["locked"]
        and pr["head"]["repo"]["full_name"] == REPO
        and pr["head"]["ref"] == BRANCH
        and pr["head"]["sha"] == head
        and pr["base"]["ref"] == "master"
        and pr["base"]["sha"] == base
        and run["head_sha"] == head
        and pr["mergeable"] is True
        and pr["mergeable_state"] in ("clean", "unstable"),
        "PR is stale, manually changed, blocked, or outside the agent scope",
    )
    comparison = api(f"compare/{base}...{head}")
    require(
        comparison["status"] == "ahead"
        and comparison["total_commits"] <= 1000
        and [(f["filename"], f["status"]) for f in comparison["files"]]
        == [(PATCH, "modified")],
        "Repair includes unrelated changes",
    )
    source = api(f"git/commits/{head}")
    require(
        base in [p["sha"] for p in source["parents"]],
        "Repair does not contain the current base",
    )
    jobs = api(f"actions/runs/{run_id}/jobs?per_page=100")["jobs"]
    required = {"Resolve upstream TDLib", "Validate Mithka patches"}
    for name in required:
        found = [j for j in jobs if j["name"] == name]
        require(
            len(found) == 1 and found[0]["conclusion"] == "success",
            "Required preflight did not pass",
        )
    require(
        all(j["conclusion"] == "skipped" for j in jobs if j["name"] not in required),
        "Expected validation-only build lane",
    )
    prepare = next(j for j in jobs if j["name"] == "Resolve upstream TDLib")
    logs = api(f"actions/jobs/{prepare['id']}/logs", raw=True)
    require(
        set(re.findall(rf"Upstream commit: ({SHA})\b", logs)) == {fields["upstream"]},
        "Preflight tested a different upstream",
    )
    generation = api(f"actions/runs/{fields['generation']}")
    run_identity(generation, base, "master", "repair-theme-patch.yml")
    require(
        generation["event"] in ("workflow_dispatch", "workflow_run"),
        "Untrusted generation trigger",
    )
    gen_jobs = api(f"actions/runs/{fields['generation']}/jobs?per_page=100")["jobs"]
    for name in ("propose", "validate"):
        found = [j for j in gen_jobs if j["name"] == name]
        require(
            len(found) == 1 and found[0]["conclusion"] == "success",
            "Generation validation did not pass",
        )
    report, candidate = artifact_report(fields["generation"])
    current, repaired = patch_bytes(base), patch_bytes(head)
    require(
        report["policy"] == "additive-conflict-sides-only"
        and report["zero_cost"] is True
        and report["upstream_sha"] == fields["upstream"]
        and report["original_patch_sha256"] == hashlib.sha256(current).hexdigest()
        and report["repaired_patch_sha256"]
        == fields["digest"]
        == hashlib.sha256(repaired).hexdigest()
        and current != repaired
        and repaired == candidate,
        "Proposed patch differs from the bounded, validated generation artifact",
    )
    review_clear(pr["number"])
    checks_clear(pr, head)
    require(
        api("git/ref/heads/master")["object"]["sha"] == base,
        "Master advanced; regenerate and revalidate",
    )
    fresh = api(f"pulls/{pr['number']}")
    require(
        fresh["state"] == "open"
        and fresh["head"]["sha"] == head
        and fresh["base"]["sha"] == base
        and fresh["body"] == pr["body"],
        "PR changed immediately before publication",
    )
    if check_only:
        print(
            f"PR #{pr['number']} at {head} is eligible; check-only mode made no changes"
        )
        return None
    require(
        os.environ.get("TDLIB_REPAIR_AUTO_MERGE") == "true",
        "Automatic repair merging is not enabled",
    )
    # Publish the exact checked tree with both parents. Non-force ref update
    # rejects a concurrent master commit instead of merging untested changes.
    merged = api(
        "git/commits",
        "POST",
        {
            "message": f"Merge pull request #{pr['number']} from iebb/{BRANCH}\n\nValidated additive TDLib cloud-theme patch repair",
            "tree": source["tree"]["sha"],
            "parents": [base, head],
        },
    )
    api("git/refs/heads/master", "PATCH", {"sha": merged["sha"], "force": False})
    require(
        api("git/ref/heads/master")["object"]["sha"] == merged["sha"],
        "Remote master changed after publication",
    )
    # Job-token pushes do not trigger ordinary push CI. Start the existing
    # canonical build pipeline explicitly, pinned to the validated upstream.
    api(
        "actions/workflows/sync-upstream.yml/dispatches",
        "POST",
        {
            "ref": "master",
            "inputs": {"upstream_ref": fields["upstream"], "validate_only": "false"},
        },
    )
    # GitHub can mark the PR merged shortly after the ref update. Native CI is
    # already dispatched, so delayed PR bookkeeping cannot strand the build.
    for attempt in range(6):
        actual = api(f"pulls/{pr['number']}")
        if actual["merged"] and actual["merge_commit_sha"] == merged["sha"]:
            break
        if attempt < 5:
            time.sleep(2)
    else:
        raise ValueError(
            "GitHub has not confirmed the exact PR merge; native CI was dispatched"
        )
    print(
        f"Merged PR #{pr['number']} as {merged['sha']}; native build workflow dispatched"
    )
    return merged["sha"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    merge_repair(args.run_id, args.check_only)
