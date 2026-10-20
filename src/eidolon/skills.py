"""Private, opt-in Markdown workflows shared by the service and companion."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from eidolon.config import _require_private_location, atomic_write, private_directory


MAX_SKILL_BYTES = 32_000
MAX_INSTRUCTIONS = 12_000


class SkillError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def validate_skill_fields(name: str, description: str, instructions: str) -> None:
    if (not isinstance(name, str) or len(name) > 64
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)):
        raise SkillError("name", "Use 1–64 lowercase letters, numbers, and single hyphens.")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        raise SkillError("description", "Describe when to use this skill in 1–1024 characters.")
    if not isinstance(instructions, str) or not 1 <= len(instructions.strip()) <= MAX_INSTRUCTIONS:
        raise SkillError("instructions", "Enter 1–12000 characters of Markdown instructions.")
    if any("\0" in value for value in (description, instructions)):
        raise SkillError("instructions", "Skill text cannot contain null characters.")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    frontmatter: dict
    hash: str

    @classmethod
    def parse(cls, name: str, text: str) -> Skill:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise SkillError("instructions", "SKILL.md needs YAML frontmatter between --- lines.")
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            raise SkillError("instructions", "Close the YAML frontmatter with a --- line.")
        try:
            metadata = yaml.safe_load("".join(lines[1:end]))
        except yaml.YAMLError:
            raise SkillError("instructions", "SKILL.md frontmatter is not valid YAML.") from None
        if not isinstance(metadata, dict) or metadata.get("name") != name:
            raise SkillError("name", "The frontmatter name must match the skill folder name.")
        description = metadata.get("description")
        body = "".join(lines[end + 1:]).strip()
        validate_skill_fields(name, description, body)
        return cls(name, description.strip(), body, metadata,
                   hashlib.sha256(text.encode("utf-8")).hexdigest())


class SkillLibrary:
    """An empty library stays empty; files copied in by hand start turned off."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).expanduser().absolute()
        self.directory = self.state_dir / "skills"
        self.settings_path = self.state_dir / "skills.json"

    def _check_location(self) -> None:
        _require_private_location(self.state_dir.resolve())
        if self.state_dir.is_symlink() or self.directory.is_symlink():
            raise ValueError("The private skills directory must not be a symbolic link.")

    def _read_text(self, path: Path, *, max_bytes: int = MAX_SKILL_BYTES) -> str:
        from eidolon.tools.files import FileTools

        self._check_location()
        root = self.state_dir.resolve()
        files = FileTools([str(root)], max_bytes=max_bytes)
        result = files.read_file(str(root / path.relative_to(self.state_dir)))
        if result["truncated"]:
            raise ValueError(f"Skill files must be no larger than {max_bytes} bytes.")
        return result["content"]

    def _settings(self) -> dict[str, bool]:
        try:
            settings = json.loads(self._read_text(self.settings_path, max_bytes=1_000_000))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            raise ValueError("Skill settings are invalid; restore skills.json before saving.") from None
        if not isinstance(settings, dict) or any(
            not isinstance(name, str) or not isinstance(enabled, bool)
            for name, enabled in settings.items()
        ):
            raise ValueError("Skill settings must map skill names to true or false.")
        return settings

    def _skill_path(self, name: str) -> Path:
        validate_skill_fields(name, "description", "instructions")
        path = self.directory / name / "SKILL.md"
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("Skill folders and SKILL.md must not be symbolic links.")
        return path

    def get(self, name: str) -> dict:
        path = self._skill_path(name)
        skill = Skill.parse(name, self._read_text(path))
        return {"name": skill.name, "description": skill.description,
                "instructions": skill.instructions, "hash": skill.hash,
                "enabled": self._settings().get(name, False), "path": str(path)}

    def list_skills(self) -> list[dict]:
        self._check_location()
        settings = self._settings()
        if not self.directory.exists():
            return []
        result = []
        for directory in sorted(self.directory.iterdir()):
            if not directory.is_dir():
                continue
            try:
                validate_skill_fields(directory.name, "description", "instructions")
            except SkillError:
                continue
            if not (directory / "SKILL.md").exists():
                continue
            entry = {"name": directory.name, "description": "",
                     "enabled": settings.get(directory.name, False), "error": None}
            try:
                skill = Skill.parse(directory.name, self._read_text(self._skill_path(directory.name)))
                entry["description"] = skill.description
            except (ValueError, OSError) as exc:
                entry["error"] = str(exc)
            result.append(entry)
        return result

    def save(self, name: str, description: str, instructions: str, *, enabled: bool,
             create: bool = False, expected_hash: str | None = None) -> dict:
        validate_skill_fields(name, description, instructions)
        if not isinstance(enabled, bool) or not isinstance(create, bool):
            raise ValueError("Skill switches must be true or false.")
        self._check_location()
        path = self._skill_path(name)
        metadata = {}
        if create:
            if path.parent.exists():
                raise SkillError("name", "A skill with this name already exists. Choose another name.")
        else:
            previous = Skill.parse(name, self._read_text(path))
            if expected_hash != previous.hash:
                raise ValueError("This skill changed on disk. Reload it before saving your edits.")
            metadata = previous.frontmatter
        settings = self._settings()
        text = ("---\n" + yaml.safe_dump(
            {**metadata, "name": name, "description": description.strip()},
            sort_keys=False, allow_unicode=True,
        ) + "---\n\n" + instructions.strip() + "\n")
        if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
            raise SkillError("instructions", "The complete skill must fit within 32000 bytes.")
        private_directory(self.state_dir)
        private_directory(self.directory)
        private_directory(path.parent)
        atomic_write(path, text)
        settings[name] = enabled
        atomic_write(self.settings_path, json.dumps(settings, indent=2) + "\n")
        return self.get(name)

    def set_enabled(self, name: str, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("The skill switch must be true or false.")
        self._check_location()
        path = self._skill_path(name)
        # Invalid skills may be switched off, but never admitted to the companion.
        text = self._read_text(path)
        if enabled:
            Skill.parse(name, text)
        settings = self._settings()
        settings[name] = enabled
        atomic_write(self.settings_path, json.dumps(settings, indent=2) + "\n")
        return {"name": name, "enabled": enabled}

    def read_for_companion(self, name: str, path: str = "SKILL.md") -> dict:
        skill = self.get(name)
        if not skill["enabled"]:
            raise ValueError("This skill is turned off. Enable it in /skills first.")
        relative = Path(path)
        if (not path or relative.is_absolute() or ".." in relative.parts
                or path.startswith(".") or any(part.startswith(".") for part in relative.parts)):
            raise ValueError("Use a relative file path inside this skill's folder.")
        if path == "SKILL.md":
            content = skill["instructions"]
            digest = skill["hash"]
        else:
            content = self._read_text(self.directory / name / relative)
            if len(content) > MAX_INSTRUCTIONS:
                raise ValueError("Supporting text files must fit within 12000 characters.")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return {"name": name, "path": path, "content": content, "hash": digest}
