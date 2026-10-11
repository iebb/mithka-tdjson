#!/usr/bin/env python3
"""Syntax-check the patched translation units with their generated CMake flags."""

import argparse
import json
import shlex
import subprocess
from pathlib import Path


UNITS = {
    "FileDownloadManager.cpp", "FileDownloader.cpp", "FileLoaderUtils.cpp",
    "FileUploadManager.cpp", "FileUploader.cpp", "td_json_client.cpp",
    "Requests.cpp", "ThemeManager.cpp",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_directory", type=Path)
    args = parser.parse_args()
    commands = json.loads(
        (args.build_directory / "compile_commands.json").read_text()
    )
    selected = {}
    for entry in commands:
        name = Path(entry["file"]).name
        if name in UNITS:
            selected.setdefault(name, entry)
    missing = UNITS - selected.keys()
    if missing:
        raise SystemExit(f"missing patched compile commands: {sorted(missing)}")
    for name, entry in selected.items():
        command = list(entry.get("arguments") or shlex.split(entry["command"]))
        output = command.index("-o")
        del command[output:output + 2]
        command.remove("-c")
        command.append("-fsyntax-only")
        subprocess.run(command, cwd=entry["directory"], check=True)
        print(f"Patched source syntax: {name} OK", flush=True)


if __name__ == "__main__":
    main()
