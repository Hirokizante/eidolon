import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts/check_private_files.py"


def git(repository, *args):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Test",
         "-c", "user.email=test@example.com", *args],
        cwd=repository, capture_output=True, check=True,
    )


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-qm", "Default ignore rules")
    return tmp_path


@pytest.mark.parametrize("path", [
    "SOUL.md", "SYSTEM.md", "USER.md", "POST_HISTORY.md", "POST HISTORY.md",
    "custom/SOUL.md", "config.json", "config.yaml", "config.toml", "config.local.json",
    "skills/personal/SKILL.md", "skills.json",
    ".env", ".env.production", "credentials.json", "secrets.yaml", "private.pem",
    "private.key", "backups/history.bundle", "state/requests.json", "diagnostics/request.json",
    "jobs.json", "settings.json", ".eidolon-interrupted-write", ".agents/skills/local/SKILL.md",
    "skills-lock.json", "room.db-wal",
])
def test_private_paths_are_ignored_and_guard_blocks_force_added_files(repository, path):
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("private test data")
    ignored = subprocess.run(["git", "check-ignore", "-q", path], cwd=repository)
    assert ignored.returncode == 0
    git(repository, "add", "--force", "--", path)
    result = subprocess.run([sys.executable, str(GUARD), path], cwd=repository, capture_output=True, text=True)
    assert result.returncode == 1
    assert path in result.stderr
    assert "private test data" not in result.stderr


def test_only_explicit_defaults_and_examples_pass_the_guard(repository):
    paths = ["config.example.json", "config.example.yaml", ".env.example", "src/eidolon/config.py"] + [
        f"src/eidolon/defaults/{name}.md" for name in ("SOUL", "SYSTEM", "USER", "POST_HISTORY")
    ]
    for path in paths:
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("default test data")
    git(repository, "add", "--", *paths)
    result = subprocess.run([sys.executable, str(GUARD), *paths], cwd=repository, capture_output=True)
    assert result.returncode == 0


def test_history_guard_detects_deleted_private_file_even_with_shared_blob(repository):
    default = repository / "src/eidolon/defaults/SOUL.md"
    default.parent.mkdir(parents=True)
    default.write_text("same blob")
    git(repository, "add", str(default.relative_to(repository)))
    git(repository, "commit", "-qm", "Default personality")
    private = repository / "SOUL.md"
    private.write_text("same blob")
    git(repository, "add", "--force", "SOUL.md")
    git(repository, "commit", "-qm", "Accidental private prompt")
    git(repository, "rm", "SOUL.md")
    git(repository, "commit", "-qm", "Removed prompt")
    tracked = subprocess.run([sys.executable, str(GUARD)], cwd=repository, capture_output=True)
    assert tracked.returncode == 0
    history = subprocess.run([sys.executable, str(GUARD), "--history"], cwd=repository, capture_output=True, text=True)
    assert history.returncode == 1
    assert "SOUL.md" in history.stderr
    assert "src/eidolon/defaults/SOUL.md" not in history.stderr


def test_history_guard_detects_private_prompt_in_a_tree_snapshot_ref(repository):
    private = repository / "SOUL.md"
    private.write_text("private snapshot data")
    git(repository, "add", "--force", "SOUL.md")
    snapshot = git(repository, "write-tree").stdout.decode().strip()
    git(repository, "update-ref", "refs/checkpoints/private", snapshot)
    git(repository, "rm", "--cached", "SOUL.md")
    tracked = subprocess.run([sys.executable, str(GUARD)], cwd=repository, capture_output=True)
    assert tracked.returncode == 0
    history = subprocess.run([sys.executable, str(GUARD), "--history"], cwd=repository, capture_output=True, text=True)
    assert history.returncode == 1
    assert "SOUL.md" in history.stderr
    assert "private snapshot data" not in history.stderr


def test_history_keeps_explicit_public_default_roots_across_package_renames(repository):
    old_root = "src/previous_package/defaults"
    old_default = repository / old_root / "SOUL.md"
    old_default.parent.mkdir(parents=True)
    old_default.write_text("generic bundled fixture")
    ignore = repository / ".gitignore"
    current_rules = ignore.read_text()
    ignore.write_text(current_rules + f"\n!/{old_root}/*.md\n")
    git(repository, "add", ".gitignore", str(old_default.relative_to(repository)))
    git(repository, "commit", "-qm", "Explicit public bundled defaults")
    git(repository, "rm", str(old_default.relative_to(repository)))
    ignore.write_text(current_rules)
    git(repository, "add", ".gitignore")
    git(repository, "commit", "-qm", "Move the bundled package")
    history = subprocess.run([sys.executable, str(GUARD), "--history"], cwd=repository,
                             capture_output=True)
    assert history.returncode == 0
    old_default.parent.mkdir(parents=True, exist_ok=True)
    old_default.write_text("a new private file under a retired root")
    git(repository, "add", "--force", str(old_default.relative_to(repository)))
    current = subprocess.run([sys.executable, str(GUARD)], cwd=repository, capture_output=True)
    assert current.returncode == 1
    git(repository, "commit", "-qm", "Accidental private file under retired package")
    history = subprocess.run([sys.executable, str(GUARD), "--history"], cwd=repository,
                             capture_output=True)
    assert history.returncode == 1


def test_history_retains_retired_private_state_patterns(repository):
    ignore = repository / ".gitignore"
    current_rules = ignore.read_text()
    ignore.write_text(current_rules + "\n/.previous-private-state/\n")
    git(repository, "add", ".gitignore")
    git(repository, "commit", "-qm", "Protect previous private state")
    private = repository / ".previous-private-state/arbitrary.txt"
    private.parent.mkdir()
    private.write_text("private synthetic content")
    git(repository, "add", "--force", str(private.relative_to(repository)))
    git(repository, "commit", "-qm", "Accidental private state")
    git(repository, "rm", str(private.relative_to(repository)))
    ignore.write_text(current_rules)
    git(repository, "add", ".gitignore")
    git(repository, "commit", "-qm", "Rename application state")
    history = subprocess.run([sys.executable, str(GUARD), "--history"], cwd=repository,
                             capture_output=True, text=True)
    assert history.returncode == 1
    assert ".previous-private-state/arbitrary.txt" in history.stderr
    assert "private synthetic content" not in history.stderr
