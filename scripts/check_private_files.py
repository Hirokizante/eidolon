"""Reject ignored files even when force-added; print paths, never their contents."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def git(*args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *args], input=input, capture_output=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError("Git could not check private file paths")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    paths = args.paths
    historical_rules = b""
    if args.history:
        # Walk commit trees, not rev-list object names: one blob may appear under
        # both a safe name and a private name in different commits.
        commits = git("rev-list", "--all").splitlines()
        # Tools can keep snapshots in refs pointing directly to trees. rev-list
        # skips those objects, although a mirror push can still publish them.
        snapshots = [
            line.split(b" ", 1)[1]
            for line in git("for-each-ref", "--format=%(objecttype) %(objectname)").splitlines()
            if line.startswith(b"tree ")
        ]
        # Each revision grants its own explicit public-default exceptions. A
        # retired root must not exempt a later accidental private file there.
        policies: dict[bytes, tuple[bytes, set[bytes]]] = {}
        historical_paths: set[bytes] = set()
        for revision in set(commits + snapshots):
            entries = [entry.split(b"\t", 1) for entry in
                       git("ls-tree", "-rz", revision.decode()).split(b"\0") if entry]
            policy_id = next((header.split()[-1] for header, path in entries
                              if path == b".gitignore"), b"")
            if policy_id not in policies:
                content = git("cat-file", "blob", policy_id.decode()) if policy_id else b""
                roots = re.findall(rb"^!/(src/[A-Za-z_][A-Za-z0-9_]*/defaults)/\*\.md$",
                                   content, re.MULTILINE)
                public = {root + b"/" + name + b".md" for root in roots
                          for name in (b"SYSTEM", b"SOUL", b"USER", b"POST_HISTORY")}
                policies[policy_id] = (content, public)
            _, public = policies[policy_id]
            historical_paths.update(path for _, path in entries if path not in public)
        paths = sorted(path.decode("utf-8", errors="surrogateescape")
                       for path in historical_paths)
        historical_rules = b"\n".join(content for content, _ in policies.values())
    elif not paths:
        paths = [path.decode("utf-8", errors="surrogateescape")
                 for path in git("ls-files", "-z").split(b"\0") if path]
    if not paths:
        return 0
    data = b"\0".join(path.encode("utf-8", errors="surrogateescape") for path in paths) + b"\0"
    with tempfile.TemporaryDirectory(prefix="eidolon-privacy-") as directory:
        # History retains retired private state patterns without committing their
        # old names. Current .gitignore rules have higher precedence than excludes.
        policy = Path(directory) / "historical-ignore"
        policy.write_bytes(historical_rules)
        options = ["-c", f"core.excludesFile={policy}"] if args.history else []
        ignored = git(*options, "check-ignore", "--no-index", "-z", "--stdin", input=data)
    if not ignored:
        return 0
    print("Private or generated files must not be committed:", file=sys.stderr)
    for path in ignored.split(b"\0"):
        if path:
            print("  " + path.decode("utf-8", errors="replace"), file=sys.stderr)
    print("Keep local data in app state; commit only explicit defaults and examples.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
