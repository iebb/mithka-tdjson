"""Deterministic publisher body, also used to reject manual edits before merging."""

START = "<!-- mithka-theme-repair:begin -->"
END = "<!-- mithka-theme-repair:end -->"


def render_body(base, upstream, digest, head, validation_url):
    return (
        START + "\n"
        "## Change\n\nRefresh only the installed-cloud-theme patch after an additive upstream conflict. "
        "Both upstream profile-share declarations and Mithka cloud-theme additions are retained.\n\n"
        f"Upstream: `{upstream}`\n\nPackaging base: `{base}`\n\n"
        f"Validated patch SHA-256: `{digest}`\n\n"
        f"Agent proposal commit: `{head}`\n\n"
        f"[Read-only generation and schema/source validation]({validation_url}). "
        "The free-model proposal is not merge approval. Native artifact builds/publication have not run in this lane.\n\n"
        "This is the single canonical PR for this repair; subsequent attempts update it. "
        "Session backup, transfer boost, release permissions and the HarmonyOS feature are unchanged.\n"
        + END
    )
