"""Read-only discovery of repository-local Claude-style Skills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import stat

from pony.security import redaction
from pony.security.private_files import private_directory_identity
from pony.security.workspace_files import (
    WorkspaceIOError,
    list_directory_names_anchored,
    read_regular_bytes_anchored,
)


MAX_PROJECT_SKILLS = 32
MAX_SKILL_FILE_BYTES = 8 * 1024
MAX_SKILL_TOTAL_BYTES = 64 * 1024
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")


@dataclass(frozen=True)
class ProjectSkill:
    """One validated, inert repository Skill document."""

    name: str
    description: str
    instructions: str


@dataclass(frozen=True)
class ProjectSkillCatalog:
    """The all-or-nothing result of one bounded repository scan."""

    skills: tuple[ProjectSkill, ...] = ()

    def get(self, name):
        return next((skill for skill in self.skills if skill.name == name), None)


def _parse_skill(data, directory_name, *, env, secret_env_names):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Skill must be UTF-8") from exc
    if redaction.contains_secret_material(
        text,
        env=env,
        secret_env_names=secret_env_names,
    ):
        raise ValueError("Skill contains secret material")

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("Skill frontmatter is required")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("Skill frontmatter is incomplete") from exc
    if end == 1:
        raise ValueError("Skill frontmatter is empty")

    fields = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not separator or key not in {"name", "description"} or not value:
            raise ValueError("Skill frontmatter is invalid")
        if key in fields:
            raise ValueError("Skill frontmatter has duplicate fields")
        fields[key] = value
    if set(fields) != {"name", "description"}:
        raise ValueError("Skill frontmatter is incomplete")
    if fields["name"] != directory_name or not _SKILL_NAME.fullmatch(fields["name"]):
        raise ValueError("Skill name must match its directory")
    if "\n" in fields["description"] or len(fields["description"]) > 512:
        raise ValueError("Skill description is invalid")

    instructions = "\n".join(lines[end + 1 :]).strip()
    if not instructions:
        raise ValueError("Skill instructions are required")
    return ProjectSkill(
        name=fields["name"],
        description=fields["description"],
        instructions=instructions,
    )


def discover_project_skills(
    workspace_root,
    *,
    expected_root_identity,
    env=None,
    secret_env_names=(),
    reserved_names=(),
):
    """Return valid ``.claude/skills/<name>/SKILL.md`` documents only.

    The layout fixes discovery depth.  Any unsafe entry, malformed Skill, or
    bound violation invalidates the whole catalog instead of partially loading
    a repository control plane whose contents changed during the scan.
    """
    try:
        listing = list_directory_names_anchored(
            workspace_root,
            ".claude/skills",
            max_entries=MAX_PROJECT_SKILLS,
            expected_root_identity=expected_root_identity,
        )
    except FileNotFoundError:
        return ProjectSkillCatalog()
    except (OSError, ValueError, WorkspaceIOError):
        return ProjectSkillCatalog()
    if listing["unsafe_count"]:
        return ProjectSkillCatalog()
    skill_root = Path(workspace_root) / ".claude" / "skills"
    try:
        skill_root_identity = private_directory_identity(skill_root)
    except (OSError, RuntimeError, ValueError):
        return ProjectSkillCatalog()

    reserved = {str(name).lstrip("/") for name in reserved_names}
    skills = []
    total_bytes = 0
    try:
        for entry in listing["entries"]:
            if not stat.S_ISDIR(entry["mode"]):
                return ProjectSkillCatalog()
            name = entry["name"]
            if not _SKILL_NAME.fullmatch(name):
                return ProjectSkillCatalog()
            if name in reserved:
                return ProjectSkillCatalog()
            document = read_regular_bytes_anchored(
                workspace_root,
                f".claude/skills/{name}/SKILL.md",
                max_bytes=MAX_SKILL_FILE_BYTES,
                expected_root_identity=expected_root_identity,
            )
            if not document["exists"]:
                return ProjectSkillCatalog()
            data = document["data"]
            total_bytes += len(data)
            if total_bytes > MAX_SKILL_TOTAL_BYTES:
                return ProjectSkillCatalog()
            skills.append(
                _parse_skill(
                    data,
                    name,
                    env=env,
                    secret_env_names=secret_env_names,
                )
            )
    except (OSError, ValueError, WorkspaceIOError):
        return ProjectSkillCatalog()
    try:
        if private_directory_identity(workspace_root) != tuple(expected_root_identity):
            return ProjectSkillCatalog()
        if private_directory_identity(skill_root) != skill_root_identity:
            return ProjectSkillCatalog()
    except (OSError, RuntimeError, ValueError):
        return ProjectSkillCatalog()
    return ProjectSkillCatalog(tuple(sorted(skills, key=lambda skill: skill.name)))
