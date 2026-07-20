"""Read-only discovery of repository-local Claude-style Skills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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
MAX_SKILL_RESOURCES = 8
MAX_SKILL_RESOURCE_BYTES = 16 * 1024
MAX_SKILL_TOTAL_BYTES = 64 * 1024
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
DEFAULT_SKILL_SECRET_ENV_NAMES = (
    "PONY_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)
_RESOURCE_REMEDIATION = (
    "fix the resources field and keep every UTF-8 resource inside its Skill directory"
)
_CATALOG_REMEDIATION = "repair .claude/skills and restart Pony"
_REMEDIATIONS = {
    "project_skill_catalog_rejected": _CATALOG_REMEDIATION,
    "project_skill_catalog_changed": _CATALOG_REMEDIATION,
    "project_skill_format_invalid": "fix strict Project Skill frontmatter and restart Pony",
    "project_skill_resource_rejected": _RESOURCE_REMEDIATION,
    "project_skill_secret_rejected": (
        "remove secret material from Project Skills and restart Pony"
    ),
}


class _SkillRejected(ValueError):
    def __init__(self, reason_code):
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class ProjectSkillResource:
    """One validated resource loaded with its owning Skill."""

    path: str
    content: str


@dataclass(frozen=True)
class _ParsedSkill:
    name: str
    description: str
    instructions: str
    resource_paths: tuple[str, ...]


@dataclass(frozen=True)
class ProjectSkill:
    """One validated, inert repository Skill document."""

    name: str
    description: str
    instructions: str
    resources: tuple[ProjectSkillResource, ...] = ()


@dataclass(frozen=True)
class ProjectSkillCatalog:
    """The all-or-nothing result of one bounded repository scan."""

    skills: tuple[ProjectSkill, ...] = ()
    status: str = "not_configured"
    reason_code: str = "project_skills_not_configured"
    remediation: str = "create .claude/skills/<name>/SKILL.md to add a Project Skill"

    def get(self, name):
        return next((skill for skill in self.skills if skill.name == name), None)

    def diagnostic(self):
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "remediation": self.remediation,
            "skill_count": len(self.skills),
        }


def _ready_catalog(skills):
    return ProjectSkillCatalog(
        tuple(sorted(skills, key=lambda skill: skill.name)),
        "ready",
        "project_skills_ready",
        "",
    )


def _rejected_catalog(reason_code, remediation):
    return ProjectSkillCatalog((), "invalid", reason_code, remediation)


def _rejected(reason_code):
    return _rejected_catalog(reason_code, _REMEDIATIONS[reason_code])


def _resource_paths(value):
    if not value:
        raise _SkillRejected("project_skill_format_invalid")
    paths = tuple(item.strip() for item in value.split(","))
    if not paths or len(paths) > MAX_SKILL_RESOURCES or len(set(paths)) != len(paths):
        raise _SkillRejected("project_skill_resource_rejected")
    for raw_path in paths:
        path = PurePosixPath(raw_path)
        if (
            not raw_path
            or len(raw_path) > 240
            or "\\" in raw_path
            or path.is_absolute()
            or path.as_posix() != raw_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or raw_path == "SKILL.md"
        ):
            raise _SkillRejected("project_skill_resource_rejected")
    return paths


def _parse_skill(data, directory_name, *, env, secret_env_names):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _SkillRejected("project_skill_format_invalid") from exc
    if redaction.contains_secret_material(
        text,
        env=env,
        secret_env_names=secret_env_names,
    ):
        raise _SkillRejected("project_skill_secret_rejected")

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise _SkillRejected("project_skill_format_invalid")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise _SkillRejected("project_skill_format_invalid") from exc
    if end == 1:
        raise _SkillRejected("project_skill_format_invalid")

    fields = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not separator or key not in {"name", "description", "resources"} or not value:
            raise _SkillRejected("project_skill_format_invalid")
        if key in fields:
            raise _SkillRejected("project_skill_format_invalid")
        fields[key] = value
    if not {"name", "description"} <= set(fields):
        raise _SkillRejected("project_skill_format_invalid")
    if fields["name"] != directory_name or not _SKILL_NAME.fullmatch(fields["name"]):
        raise _SkillRejected("project_skill_format_invalid")
    if "\n" in fields["description"] or len(fields["description"]) > 512:
        raise _SkillRejected("project_skill_format_invalid")

    instructions = "\n".join(lines[end + 1 :]).strip()
    if not instructions:
        raise _SkillRejected("project_skill_format_invalid")
    return _ParsedSkill(
        fields["name"],
        fields["description"],
        instructions,
        _resource_paths(fields["resources"]) if "resources" in fields else (),
    )


def _decode_resource(data, *, env, secret_env_names):
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _SkillRejected("project_skill_resource_rejected") from exc
    if redaction.contains_secret_material(
        content,
        env=env,
        secret_env_names=secret_env_names,
    ):
        raise _SkillRejected("project_skill_secret_rejected")
    return content


def discover_project_skills(
    workspace_root,
    *,
    expected_root_identity,
    env=None,
    secret_env_names=DEFAULT_SKILL_SECRET_ENV_NAMES,
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
        return _rejected("project_skill_catalog_rejected")
    if listing["unsafe_count"]:
        return _rejected("project_skill_catalog_rejected")
    skill_root = Path(workspace_root) / ".claude" / "skills"
    try:
        skill_root_identity = private_directory_identity(skill_root)
    except (OSError, RuntimeError, ValueError):
        return _rejected("project_skill_catalog_rejected")

    reserved = {str(name).lstrip("/") for name in reserved_names}
    skills = []
    total_bytes = 0
    try:
        for entry in listing["entries"]:
            if not stat.S_ISDIR(entry["mode"]):
                raise _SkillRejected("project_skill_catalog_rejected")
            name = entry["name"]
            if not _SKILL_NAME.fullmatch(name):
                raise _SkillRejected("project_skill_catalog_rejected")
            if name in reserved:
                raise _SkillRejected("project_skill_catalog_rejected")
            skill_directory = skill_root / name
            try:
                skill_directory_identity = private_directory_identity(skill_directory)
                document = read_regular_bytes_anchored(
                    workspace_root,
                    f".claude/skills/{name}/SKILL.md",
                    max_bytes=MAX_SKILL_FILE_BYTES,
                    expected_root_identity=expected_root_identity,
                )
            except (OSError, ValueError, WorkspaceIOError) as exc:
                raise _SkillRejected("project_skill_catalog_rejected") from exc
            if not document["exists"]:
                raise _SkillRejected("project_skill_format_invalid")
            data = document["data"]
            total_bytes += len(data)
            if total_bytes > MAX_SKILL_TOTAL_BYTES:
                raise _SkillRejected("project_skill_catalog_rejected")
            parsed = _parse_skill(
                data,
                name,
                env=env,
                secret_env_names=secret_env_names,
            )
            resources = []
            for resource_path in parsed.resource_paths:
                try:
                    resource = read_regular_bytes_anchored(
                        workspace_root,
                        f".claude/skills/{name}/{resource_path}",
                        max_bytes=MAX_SKILL_RESOURCE_BYTES,
                        expected_root_identity=expected_root_identity,
                    )
                except (OSError, ValueError, WorkspaceIOError) as exc:
                    raise _SkillRejected("project_skill_resource_rejected") from exc
                if not resource["exists"]:
                    raise _SkillRejected("project_skill_resource_rejected")
                total_bytes += len(resource["data"])
                if total_bytes > MAX_SKILL_TOTAL_BYTES:
                    raise _SkillRejected("project_skill_resource_rejected")
                resources.append(
                    ProjectSkillResource(
                        path=resource_path,
                        content=_decode_resource(
                            resource["data"],
                            env=env,
                            secret_env_names=secret_env_names,
                        ),
                    )
                )
            if private_directory_identity(skill_directory) != skill_directory_identity:
                raise _SkillRejected("project_skill_catalog_changed")
            skills.append(
                ProjectSkill(
                    name=parsed.name,
                    description=parsed.description,
                    instructions=parsed.instructions,
                    resources=tuple(resources),
                )
            )
    except _SkillRejected as exc:
        return _rejected(exc.reason_code)
    except (OSError, ValueError, WorkspaceIOError):
        return _rejected("project_skill_catalog_rejected")
    try:
        if private_directory_identity(workspace_root) != tuple(expected_root_identity):
            return _rejected("project_skill_catalog_changed")
        if private_directory_identity(skill_root) != skill_root_identity:
            return _rejected("project_skill_catalog_changed")
    except (OSError, RuntimeError, ValueError):
        return _rejected("project_skill_catalog_changed")
    return _ready_catalog(skills)
