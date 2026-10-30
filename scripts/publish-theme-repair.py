#!/usr/bin/env python3
"""Open/update one validated cloud-theme repair PR; never merge or publish releases."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPO = "iebb/mithka-tdjson"
BRANCH = "automation/tdlib-theme-patch"
PATCH = "patches/mithka-installed-cloud-themes.patch"
SHA = re.compile(r"[0-9a-f]{40}")
START = "<!-- mithka-theme-repair:begin -->"
END = "<!-- mithka-theme-repair:end -->"


def api(path, method="GET", body=None):
    command = ["gh", "api", f"repos/{REPO}/{path}", "--method", method]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(body) if body is not None else None,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def main():
    base = os.environ["PACKAGING_SHA"]
    if not SHA.fullmatch(base):
        raise ValueError("Invalid packaging base")
    patch = Path("repair-output/mithka-installed-cloud-themes.patch").read_bytes()
    report = json.loads(Path("repair-output/repair-report.json").read_text())
    if (
        len(patch) > 300_000
        or report["repaired_patch_sha256"] != hashlib.sha256(patch).hexdigest()
    ):
        raise ValueError("Validated patch digest mismatch")
    upstream = report["upstream_sha"]
    if not SHA.fullmatch(upstream):
        raise ValueError("Invalid upstream identity")
    if api("git/ref/heads/master")["object"]["sha"] != base:
        raise ValueError(
            "Master advanced; regenerate and validate against its new source"
        )
    prs = api(f"pulls?state=open&head=iebb:{BRANCH}&base=master&per_page=100")
    if len(prs) > 1:
        raise ValueError("Multiple canonical repair PRs require manual consolidation")
    parent = base
    branch_exists = False
    try:
        parent = api(f"git/ref/heads/{BRANCH}")["object"]["sha"]
        branch_exists = True
    except subprocess.CalledProcessError:
        if prs:
            raise ValueError("Canonical PR branch is unavailable")
    if prs:
        old_body = prs[0].get("body", "")
        if (
            old_body.count(START) != 1
            or old_body.count(END) != 1
            or old_body.index(START) >= old_body.index(END)
        ):
            raise ValueError(
                "Canonical PR has unmanaged review notes; manual integration required"
            )
        previous = re.search(
            r"Agent proposal commit: `([0-9a-f]{40})`",
            old_body[old_body.index(START) : old_body.index(END)],
        )
        if not previous or previous[1] != parent:
            raise ValueError(
                "Canonical PR has manual/unattested commits; preserve them for review"
            )
    if branch_exists and parent != base:
        comparison = api(f"compare/{base}...{parent}")
        if prs:
            changed = [file["filename"] for file in comparison.get("files", [])]
            if changed != [PATCH] or comparison.get("total_commits", 1001) > 1000:
                raise ValueError(
                    "Canonical branch contains other work; refusing to overwrite it"
                )
        elif comparison["status"] not in ("behind", "identical"):
            raise ValueError(
                "Previous closed repair branch has unmerged work; manual review required"
            )
    if (
        report.get("policy") != "additive-conflict-sides-only"
        or report.get("zero_cost") is not True
    ):
        raise ValueError("Unexpected repair policy or pricing evidence")
    source = api(f"git/commits/{base}")
    blob = api(
        "git/blobs", "POST", {"content": patch.decode("utf-8"), "encoding": "utf-8"}
    )
    tree = api(
        "git/trees",
        "POST",
        {
            "base_tree": source["tree"]["sha"],
            "tree": [
                {"path": PATCH, "mode": "100644", "type": "blob", "sha": blob["sha"]},
            ],
        },
    )
    parents = list(dict.fromkeys([parent, base]))
    commit = api(
        "git/commits",
        "POST",
        {
            "message": f"fix: refresh cloud-theme patch for TDLib {upstream[:12]}",
            "tree": tree["sha"],
            "parents": parents,
        },
    )
    if api("git/ref/heads/master")["object"]["sha"] != base:
        raise ValueError("Master advanced before PR publication")
    if branch_exists:
        if api(f"git/ref/heads/{BRANCH}")["object"]["sha"] != parent:
            raise ValueError("Canonical branch advanced during validation")
        api(f"git/refs/heads/{BRANCH}", "PATCH", {"sha": commit["sha"], "force": False})
    else:
        api("git/refs", "POST", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    body = (
        START + "\n"
        "## Change\n\nRefresh only the installed-cloud-theme patch after an additive upstream conflict. "
        "Both upstream profile-share declarations and Mithka cloud-theme additions are retained.\n\n"
        f"Upstream: `{upstream}`\n\nPackaging base: `{base}`\n\n"
        f"Validated patch SHA-256: `{report['repaired_patch_sha256']}`\n\n"
        f"Agent proposal commit: `{commit['sha']}`\n\n"
        f"[Read-only generation and schema/source validation]({os.environ['VALIDATION_URL']}). "
        "The free-model proposal is not merge approval. Native artifact builds/publication have not run in this lane.\n\n"
        "This is the single canonical PR for this repair; subsequent attempts update it. "
        "Session backup, transfer boost, release permissions and the HarmonyOS feature are unchanged.\n"
        + END
    )
    if prs:
        previous_body = prs[0].get("body", "")
        body = (
            previous_body[: previous_body.index(START)]
            + body
            + previous_body[previous_body.index(END) + len(END) :]
        )
        result = api(f"pulls/{prs[0]['number']}", "PATCH", {"body": body})
    else:
        result = api(
            "pulls",
            "POST",
            {
                "title": "fix: refresh the TDLib cloud-theme patch",
                "head": BRANCH,
                "base": "master",
                "body": body,
            },
        )
    # GITHUB_TOKEN pushes cannot trigger ordinary PR checks. Dispatch only the
    # fixed read-only lane with publication disabled by both input and ref.
    api(
        "actions/workflows/sync-upstream.yml/dispatches",
        "POST",
        {
            "ref": BRANCH,
            "inputs": {"upstream_ref": upstream, "validate_only": "true"},
        },
    )
    print(result["html_url"])


if __name__ == "__main__":
    main()
