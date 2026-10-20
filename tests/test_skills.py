import json
import os

import pytest

from eidolon.config import initialize_config
from eidolon.skills import SkillError, SkillLibrary


def create(library, name="daily-plan", *, enabled=True):
    return library.save(name, "Use when planning the day.", "Ask for priorities, then make a plan.",
                        create=True, enabled=enabled)


def test_setup_and_empty_library_install_nothing(tmp_path):
    config = initialize_config(state_dir=tmp_path / "state")
    library = SkillLibrary(config.state_dir)
    assert library.list_skills() == []
    assert not library.directory.exists() and not library.settings_path.exists()


def test_create_edit_switches_and_restart_preserve_private_files(tmp_path):
    library = SkillLibrary(tmp_path)
    saved = create(library)
    path = library.directory / "daily-plan/SKILL.md"
    assert path.read_text().startswith("---\nname: daily-plan\n")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert library.settings_path.stat().st_mode & 0o777 == 0o600
    restarted = SkillLibrary(tmp_path)
    assert restarted.get(saved["name"])["enabled"]
    restarted.set_enabled(saved["name"], False)
    with pytest.raises(ValueError, match="turned off"):
        library.read_for_companion(saved["name"])
    edited = restarted.save(saved["name"], "Use for planning.", "Start with one priority.",
                            enabled=False, expected_hash=saved["hash"])
    assert edited["hash"] != saved["hash"]
    assert not SkillLibrary(tmp_path).get(saved["name"])["enabled"]
    restarted.set_enabled(saved["name"], True)
    assert library.read_for_companion(saved["name"])["content"] == "Start with one priority."
    with pytest.raises(SkillError, match="already exists"):
        create(library)


def test_import_is_opt_in_metadata_survives_edit_and_stale_save_is_rejected(tmp_path):
    directory = tmp_path / "skills/reading"
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text("---\nname: reading\ndescription: Use when reading.\n"
                    "license: MIT\nmetadata:\n  author: example\n---\n\nAsk about the book.\n")
    library = SkillLibrary(tmp_path)
    saved = library.get("reading")
    assert not saved["enabled"]
    library.set_enabled("reading", True)
    library.save("reading", "Use when discussing books.", "Ask what stood out.",
                 enabled=True, expected_hash=saved["hash"])
    assert "license: MIT" in path.read_text() and "author: example" in path.read_text()
    with pytest.raises(ValueError, match="changed on disk"):
        library.save("reading", "Old draft", "Old instructions", enabled=True,
                     expected_hash=saved["hash"])
    assert library.get("reading")["instructions"] == "Ask what stood out."


@pytest.mark.parametrize("name", ["../escape", "/tmp/escape", "UPPER", "bad--name", "x-", "x" * 65])
def test_names_cannot_escape_or_violate_skill_format(tmp_path, name):
    with pytest.raises(SkillError):
        create(SkillLibrary(tmp_path), name)
    assert not (tmp_path / "skills").exists()


def test_invalid_skills_are_visible_but_cannot_be_enabled(tmp_path):
    library = SkillLibrary(tmp_path)
    create(library)
    path = library.directory / "daily-plan/SKILL.md"
    path.write_text("---\nname: different-name\ndescription: Invalid\n---\nBody\n")
    entry = library.list_skills()[0]
    assert entry["error"] and entry["name"] == "daily-plan"
    with pytest.raises(SkillError, match="folder name"):
        library.set_enabled("daily-plan", True)
    library.set_enabled("daily-plan", False)
    assert not library.list_skills()[0]["enabled"]


def test_supporting_files_are_bounded_and_confined_to_enabled_skill(tmp_path):
    library = SkillLibrary(tmp_path / "state")
    create(library)
    folder = library.directory / "daily-plan"
    (folder / "references").mkdir()
    (folder / "references/example.md").write_text("An example plan.")
    assert library.read_for_companion("daily-plan", "references/example.md")["content"] == "An example plan."
    outside = tmp_path / "outside.md"
    outside.write_text("private data")
    (folder / "escape.md").symlink_to(outside)
    (folder / "escape-folder").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(folder / "pipe")
    (folder / "large.md").write_text("x" * 32_001)
    (folder / "binary").write_bytes(b"\0binary")
    for path in ("../outside.md", str(outside), "escape.md", "escape-folder/outside.md",
                 "pipe", "large.md", "binary", ".hidden"):
        with pytest.raises((ValueError, OSError)):
            library.read_for_companion("daily-plan", path)
    library.set_enabled("daily-plan", False)
    with pytest.raises(ValueError, match="turned off"):
        library.read_for_companion("daily-plan", "references/example.md")


def test_symlink_roots_and_private_settings_cannot_redirect_reads_or_writes(tmp_path):
    library = SkillLibrary(tmp_path / "state")
    create(library)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("private data")
    (library.directory / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        library.get("linked")
    library.settings_path.unlink()
    library.settings_path.symlink_to(outside / "SKILL.md")
    with pytest.raises(OSError):
        library.set_enabled("daily-plan", False)
    assert (outside / "SKILL.md").read_text() == "private data"
    other = tmp_path / "other"
    other.mkdir()
    (other / "skills").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        SkillLibrary(other).list_skills()


def test_bad_settings_and_oversized_instructions_do_not_overwrite_existing_skill(tmp_path):
    library = SkillLibrary(tmp_path)
    saved = create(library)
    with pytest.raises(SkillError, match="12000"):
        library.save(saved["name"], "Description", "x" * 12_001,
                     enabled=True, expected_hash=saved["hash"])
    library.settings_path.write_text(json.dumps({"daily-plan": "false"}))
    with pytest.raises(ValueError, match="true or false"):
        library.save(saved["name"], "Description", "Replacement", enabled=True,
                     expected_hash=saved["hash"])
    assert "Ask for priorities" in (library.directory / "daily-plan/SKILL.md").read_text()


def test_skills_cannot_be_stored_in_checkout(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="outside Git"):
        create(SkillLibrary(tmp_path / "state"))


def test_private_state_can_use_a_symlinked_ancestor_without_allowing_skill_links(tmp_path):
    ancestor = tmp_path / "alias"
    target = tmp_path / "real"
    target.mkdir()
    ancestor.symlink_to(target, target_is_directory=True)
    library = SkillLibrary(ancestor / "state")
    create(library)
    assert library.get("daily-plan")["enabled"]
    assert library.read_for_companion("daily-plan")["content"].startswith("Ask for priorities")
