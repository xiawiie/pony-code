# ADR-0046: Read-only repository Skills

## Status

Accepted and implemented for the unreleased Pony 1.0 line.

## Context

Pony needs a familiar, Claude-style way for a repository to supply a focused
workflow.  Host execution is not a sandbox: treating arbitrary Skill folders,
scripts, HOME catalogs, or marketplaces as control-plane input would widen the
trusted surface and create configuration precedence that users cannot inspect.

## Decision

Pony recognizes exactly one project layout:

```text
.claude/skills/<lowercase-name>/SKILL.md
```

The document must be a UTF-8, single-link regular file beneath the trusted
repository root.  Descriptor-anchored reads reject symlinks, hardlinks,
special files, root identity drift, malformed or incomplete strict frontmatter,
known secret material, oversized files, too many entries, and aggregate size
overflow.  A catalog with any unsafe or malformed entry is empty; Pony never
partially trusts a changed control plane.

Frontmatter requires `name` and `description` and may contain one optional
`resources` field. `name` must be the directory name and use lowercase ASCII
letters, digits, and hyphens. `resources` is a comma-separated list of at most
eight POSIX relative paths inside the same Skill directory, for example:

```yaml
resources: references/checklist.md,templates/report.txt
```

There is no globbing, recursive reference discovery, dependency graph, or
implicit directory load. Each listed resource is a single-link regular UTF-8
file, at most 16 KiB; `SKILL.md` plus all resources still share the existing
64 KiB catalog cap and secret gate. The body and listed resources are inert
read-only model context. They cannot define tools, permissions, hooks,
providers, scripts, model targets, or configuration.

`/skill-name [prompt]` is discovered through the existing shared REPL handler
and completion surface.  It loads one matching Skill only for that top-level
turn as a required `<pony:active_skill>` source block. User instructions win,
then applicable project rules, then Skill instructions/resources. The Skill is not
copied to canonical Session messages, Memory, or durable trace metadata; tool
permissions and Host path/secret/trust checks remain unchanged.

Pony deliberately does not read `~/.claude/skills`, plugins, `.agents/skills`,
or any compatibility directory.  It does not execute Skill scripts, install
Skills, fetch a marketplace, or persist a loaded-Skill state.

## Consequences

- A repository can provide explicit, discoverable read-only workflows without
  adding a second command registry or a broad package/plugin runtime.
- A malformed project Skill hides the whole catalog until its owner repairs it,
  rather than silently selecting a subset with unclear authority.
- `/help` and `pony doctor` expose only stable reason codes and remediation;
  rejected paths or content are never echoed.
- Global catalog, online installation, script execution, and dynamic tool
  registration need separate security designs if concrete demand appears.
