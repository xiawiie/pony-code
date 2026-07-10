# Pico A1 Sensitive Data and Safe Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close Pico's sensitive-data and automatic-execution trust boundaries so Provider requests, local artifacts, direct tools, project configuration, and the automatic shell lane fail closed without adding an OS sandbox.

**Architecture:** Keep `pico/security.py` as the small functional policy source, add one shared `pico/safe_subprocess.py` for frozen absolute executables and hardened Git/rg calls, and enforce the policy at existing SessionStore, ContextManager, ToolExecutor, CLI, and file-tool boundaries. The automatic shell lane accepts only exact simple argv grammars and runs with `shell=False`; explicitly approved complex shell remains a documented human-authorized escape hatch.

**Tech Stack:** Python 3.11+, stdlib (`re`, `os`, `stat`, `tempfile`, `json`, `getpass`, `subprocess`, `urllib`), pytest, Ruff; no new runtime or test dependencies.

## Global Constraints

- Authoritative design: `docs/superpowers/specs/2026-07-10-pico-security-trust-baseline-design.md` at commit `3848529` or later.
- Preserve session schema v3 and the four domain command risk classes: `read_only | workspace_write | external_effect | destructive`.
- Do not add an OS sandbox, policy registry, secret vault, encryption layer, or third-party shell parser.
- Provider-visible system/messages, in-memory session, normal artifacts, approval output, verification output, and CLI inspection must not contain known or high-confidence secret material.
- Exact recovery bytes are never replaced by `<redacted>`; A2 excludes sensitive path/content from blobs.
- Direct file/search/memory tools and `approval=auto` shell must not access sensitive paths. A user-approved complex `shell=True` command is explicitly outside that static guarantee.
- Automatic commands use a frozen absolute executable and `shell=False`; unknown options, binary paths, wrappers, interpreters, composites, redirects, and parse errors require approval or are rejected.
- `.env` is exactly `<resolved workspace root>/.env`, must be a regular non-symlink file, is atomically written, and has mode 0600 on POSIX.
- `.pico` private files are 0600 and owned directories are 0700; permission hardening never follows symlinks.
- Make surgical changes only; remove only compatibility code made obsolete by this plan.
- Every task follows red-green-refactor, ends with focused green tests, and creates one intentional commit.

## Cross-Plan Interfaces Frozen by A1

The following names are consumed by A2 and A3 and must not be renamed after Task 10 without updating those plans:

```text
pico.security.contains_secret_material(text, env=None, secret_env_names=None) -> bool
pico.security.SensitiveDataBlockedError -> RuntimeError subclass
pico.security.redact_text(text, env=None, secret_env_names=None) -> str
pico.security.redact_artifact(value, key=None, env=None, secret_env_names=None) -> Any
pico.security.sanitize_provider_payload(system, messages, env=None, secret_env_names=None) -> tuple[str, list]
pico.security.sensitive_path_reason(raw_path) -> str
pico.security.is_sensitive_path(raw_path) -> bool
pico.security.require_regular_no_symlink(path, *, allow_missing=False) -> Path
pico.security.ensure_private_dir(path) -> Path
pico.security.ensure_private_file(path) -> Path

pico.safe_subprocess.discover_lexical_repo_root(cwd) -> Path
pico.safe_subprocess.build_trusted_executables(workspace_root, *, env=None, names=()) -> dict[str, str]
pico.safe_subprocess.run_hardened_git(executable, args, *, cwd, timeout=5, check=False, text=False) -> CompletedProcess
pico.safe_subprocess.run_hardened_rg(executable, args, *, cwd, timeout=20) -> CompletedProcess

pico.config.project_env_path(workspace_root) -> Path
pico.config.read_project_env(workspace_root, warn=True) -> dict[str, str]
pico.config.load_project_env(workspace_root, override=True, warn=True) -> dict[str, str]
pico.config.write_project_env_assignments(workspace_root, assignments) -> dict
pico.config.validate_provider_base_url(value) -> str

pico.recovery_policy.assess_command(command, workspace_root, executables=None) -> dict
pico.recovery_policy.command_risk_class(command, _depth=0) -> str
```

## File Responsibility Map

| File | A1 responsibility |
| --- | --- |
| `pico/security.py` | Secret detection/redaction, sensitive paths, no-follow/private permission helpers |
| `pico/safe_subprocess.py` | Lexical repo bootstrap, trusted absolute executable map, hardened Git/rg runners |
| `pico/config.py` | Exact project `.env`, import allow/deny rules, reversible private atomic writes |
| `pico/cli_commands.py`, `pico/cli_diagnostics.py`, `pico/cli.py` | Remove argv secret, add `config set-secret`, safe inspection/doctor output |
| `pico/session_store.py`, `pico/run_store.py` | Pre-load/full-candidate redaction and 0600/0700 persistence |
| `pico/context_manager.py`, `pico/agent_loop.py`, `pico/cli_start.py` | Provider request, decoded Action, immediate output, working-memory, error boundaries |
| `pico/providers/*.py` | Stable provider error categories without HTTP bodies or credential-bearing URLs |
| `pico/workspace.py`, `pico/repo_map.py`, `pico/workspace_observer.py` | Hardened Git and no-follow bootstrap/index reads |
| `pico/tools.py`, `pico/memory/*.py`, `pico/workspace_snapshot.py` | Sensitive file/content policy and controlled search |
| `pico/recovery_policy.py` | Exact command assessment and compatibility wrapper |
| `pico/tool_context.py`, `pico/tool_executor.py`, `pico/runtime.py` | Single gate, frozen executables, approval outcomes, safe results; remove raw Pico proxies |
| `pico/verification.py` | Evidence only for actually executed exact simple argv |

---

### Task 1: High-Confidence Secret Detection and Redaction

**Files:**
- Modify: `pico/security.py`
- Modify: `tests/test_security.py`

**Interfaces:**
- Produces: `contains_secret_material()`, strengthened `redact_text()` and `redact_artifact()`.
- Consumes: existing `detected_secret_env_items()`, `is_secret_env_name()`, `REDACTED_VALUE`.
- Later tasks rely on `redact_text(redact_text(x)) == redact_text(x)` and `contains_secret_material(redact_text(x)) is False` for supported patterns.

- [ ] **Step 1: Add failing redaction and false-positive tests**

```python
SECRET_SENTINEL = "github_pat_A123456789012345678901234567890"


def test_redact_text_removes_known_secret_even_inside_identifier():
    env = {"PICO_API_KEY": "alpha123456789"}
    assert redact_text("prefix_alpha123456789_suffix", env=env) == "prefix_<redacted>_suffix"


def test_redact_text_covers_high_confidence_material_without_env():
    samples = [
        SECRET_SENTINEL,
        "Authorization: Bearer bearer-secret-123456789",
        "password=correct-horse-battery-staple",
        "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----",
        "https://user:secret-pass@example.test/v1?api_key=alpha123456789",
        "https://user:secret-pass@example.test/v1",
        "tool --api-key sk-cli-123456789",
    ]
    for sample in samples:
        safe = redact_text(sample, env={})
        assert sample not in safe
        assert not contains_secret_material(safe, env={})


def test_secret_detector_ignores_security_prose_and_sample_values():
    for text in (
        "token budget",
        "password policy",
        "credential rotation design",
        "input_tokens",
        "API_KEY=your-api-key",
        "TOKEN=${TOKEN}",
    ):
        assert contains_secret_material(text, env={}) is False
        assert redact_text(text, env={}) == text


@pytest.mark.parametrize(
    "key",
    (
        "api_key",
        "access_key",
        "auth_token",
        "bearer_token",
        "credential",
        "credentials",
        "client_secret",
        "password",
        "token",
        "authorization",
        "private_key",
    ),
)
def test_redact_artifact_replaces_opaque_values_for_secret_mapping_keys(key):
    value = "opaque-value-with-no-token-shape"
    assert redact_artifact({key: value}, env={}) == {key: REDACTED_VALUE}


def test_redact_artifact_preserves_non_secret_metric_keys():
    value = {"input_tokens": 12, "token_budget": 2048, "credential_policy": "rotate quarterly"}
    assert redact_artifact(value, env={}) == value


@pytest.mark.parametrize(
    "text",
    (
        '{"api_key":"opaque-json-value"}',
        '{"apiKey":"opaque-json-value"}',
        '{"clientSecret":"opaque-json-value"}',
        '{"accessToken":"opaque-json-value"}',
    ),
)
def test_quoted_json_secret_assignment_is_detected_and_redacted(text):
    safe = redact_text(text, env={})
    assert "opaque-json-value" not in safe
    assert contains_secret_material(text, env={}) is True


@pytest.mark.parametrize("key", ("apiKey", "clientSecret", "accessToken", "privateKey"))
def test_camel_case_secret_mapping_key_is_redacted(key):
    assert redact_artifact({key: "opaque-value"}, env={}) == {key: REDACTED_VALUE}
```

- [ ] **Step 2: Run the focused tests and confirm red**

Run: `uv run pytest tests/test_security.py -q`

Expected: failures showing embedded known values and concrete token/assignment patterns are not yet redacted and `contains_secret_material` is missing.

- [ ] **Step 3: Implement the minimal concrete-pattern pipeline**

Add two separate pattern sets—broad prose detection stays in `looks_secret_shaped_text`; only the following high-confidence patterns participate in replacement:

```python
_PLACEHOLDER_VALUE_RE = re.compile(
    r"(?i)^(?:example|dummy|changeme|replace[-_ ]?me|your[-_ ]?(?:api[-_ ]?)?key|x{3,}|\$\{[^}]+\}|<[^>]+>)$"
)
_CONCRETE_TOKEN_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_ -]?key|access[_ -]?(?:key|token)|auth[_ -]?token|client[_ -]?secret|credential|secret|password|token)\b[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}]+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)([^\s]+)")
_SECRET_FLAG_RE = re.compile(
    r"(?i)(--(?:api[-_]?key|access[-_]?key|auth[-_]?token|credential|secret|password|token)(?:=|\s+))([^\s]+)"
)
_URL_USERINFO_RE = re.compile(r"(?i)(https?://[^/@\s:]+:)([^/@\s]+)(@)")
_URL_SECRET_RE = re.compile(r"(?i)([?&](?:api[_-]?key|token|secret|password)=)([^&#\s]+)")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.DOTALL,
)
_SECRET_MAPPING_KEYS = {
    "api_key",
    "access_key",
    "access_token",
    "auth_token",
    "bearer_token",
    "credential",
    "credentials",
    "secret",
    "client_secret",
    "password",
    "token",
    "authorization",
    "private_key",
}


def _is_secret_mapping_key(key):
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    normalized = re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")
    return normalized in _SECRET_MAPPING_KEYS or any(
        normalized.endswith("_" + item)
        for item in _SECRET_MAPPING_KEYS
    )


def _replace_assignment(match):
    value = match.group(2).strip("\"'")
    if _PLACEHOLDER_VALUE_RE.fullmatch(value):
        return match.group(0)
    return match.group(1) + REDACTED_VALUE


def redact_text(text, env=None, secret_env_names=None):
    text = str(text)
    for _, value in sorted(
        detected_secret_env_items(env=env, secret_env_names=secret_env_names),
        key=lambda item: len(item[1]),
        reverse=True,
    ):
        if len(value) >= MIN_SECRET_SUBSTRING_REDACTION_LENGTH:
            text = text.replace(value, REDACTED_VALUE)
        elif text == value:
            text = REDACTED_VALUE
    text = _PRIVATE_KEY_RE.sub(REDACTED_VALUE, text)
    text = _AUTH_HEADER_RE.sub(lambda match: match.group(1) + REDACTED_VALUE, text)
    text = _SECRET_FLAG_RE.sub(lambda match: match.group(1) + REDACTED_VALUE, text)
    text = _URL_USERINFO_RE.sub(lambda match: match.group(1) + REDACTED_VALUE + match.group(3), text)
    text = _SECRET_ASSIGNMENT_RE.sub(_replace_assignment, text)
    text = _URL_SECRET_RE.sub(lambda match: match.group(1) + REDACTED_VALUE, text)
    for pattern in _CONCRETE_TOKEN_RES:
        text = pattern.sub(REDACTED_VALUE, text)
    return text


def contains_secret_material(text, env=None, secret_env_names=None):
    original = str(text or "")
    return redact_text(original, env=env, secret_env_names=secret_env_names) != original
```

Keep `redact_artifact()` recursive and idempotent. For `_is_secret_mapping_key(key)`, replace the complete value before recursive inspection. Do not treat `input_tokens`, `token_budget`, or `credential_policy` as secret keys.

- [ ] **Step 4: Run focused tests and the existing safety invariants**

Run: `uv run pytest tests/test_security.py tests/test_safety_invariants.py -q`

Expected: all pass. Update the old embedded-token assertion to expect redaction; keep the short-substring false-positive test green.

- [ ] **Step 5: Run Ruff and commit**

Run: `uv run ruff check pico/security.py tests/test_security.py`

Expected: no diagnostics.

```bash
git add pico/security.py tests/test_security.py
git commit -m "feat(security): strengthen secret detection and redaction"
```

### Task 2: Sensitive Paths and Private No-Follow Files

**Files:**
- Modify: `pico/security.py`
- Modify: `tests/test_security.py`
- Create: `tests/test_private_paths.py`

**Interfaces:**
- Produces: `sensitive_path_reason()`, `is_sensitive_path()`, `require_regular_no_symlink()`, `ensure_private_dir()`, `ensure_private_file()`.
- Consumes: Task 1 redaction helpers only for error text; path classification itself is pure and does not read file content.
- A2 uses the no-follow/private helpers for checkpoint directories, blobs, locks, temp files, and quarantine.

- [ ] **Step 1: Add failing path-classification and no-follow tests**

```python
@pytest.mark.parametrize(
    "path",
    (
        ".env",
        ".env.local",
        ".envrc",
        ".ssh/id_ed25519",
        ".aws/credentials",
        ".docker/config.json",
        "certs/client.pem",
        "keys/signing.key",
        ".pico/sessions/s.json",
        ".pico/runs/r/trace.jsonl",
        ".pico/checkpoints/blobs/aa/value",
    ),
)
def test_sensitive_path_matrix(path):
    assert is_sensitive_path(path)
    assert sensitive_path_reason(path) == "sensitive_path"


@pytest.mark.parametrize("path", (".env.example", ".env.sample", ".env.template", "certs/ca.crt", "id_ed25519.pub"))
def test_sensitive_path_templates_and_public_material_are_allowed(path):
    assert not is_sensitive_path(path)
    assert sensitive_path_reason(path) == ""


def test_private_hardening_refuses_symlink_without_chmodding_target(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_text("sentinel", encoding="utf-8")
    before = stat.S_IMODE(outside.stat().st_mode)
    linked = tmp_path / "linked"
    linked.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        ensure_private_file(linked)
    assert stat.S_IMODE(outside.stat().st_mode) == before


def test_private_hardening_refuses_symlinked_parent(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside.mkdir()
    linked_parent = tmp_path / "private"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ensure_private_dir(linked_parent / "nested")
    assert not (outside / "nested").exists()


def test_regular_file_guard_refuses_symlinked_parent(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-read"
    outside.mkdir()
    (outside / "note.txt").write_text("outside", encoding="utf-8")
    (tmp_path / "docs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        require_regular_no_symlink(tmp_path / "docs" / "note.txt")


def test_private_modes_are_owner_only(tmp_path):
    directory = ensure_private_dir(tmp_path / "private")
    target = directory / "artifact.json"
    target.write_text("{}", encoding="utf-8")
    ensure_private_file(target)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
```

- [ ] **Step 2: Run the tests and confirm red**

Run: `uv run pytest tests/test_security.py tests/test_private_paths.py -q`

Expected: import failures for the four new functions.

- [ ] **Step 3: Implement exact classification and lstat-based hardening**

Add lowercase POSIX normalization without resolving symlinks. Pin the complete first-version rules in a parametrized test: `.env`, `.env.*`, `.envrc`, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `credentials.json`, `auth.json`, `service-account*.json`, `secrets.json/yaml/yml/toml`, `.ssh/**`, `.gnupg/**`, `.aws/credentials`, `.docker/config.json`, `.kube/config`, `.pem/.key/.p12/.pfx/.jks/.keystore`, and `.pico/sessions/**`, `.pico/runs/**`, `.pico/checkpoints/**`. Pin `.env.example/.sample/.template`, `.pub`, and `.crt` as allowed exceptions.

Every file helper must inspect the complete lexical component chain with `lstat()` before opening or chmodding. Do not call `resolve()` on the candidate, because that loses evidence that a component was a symlink. Use this concrete internal shape:

```python
def _lexical_absolute(path):
    return Path(os.path.abspath(os.fspath(path)))


def _lstat_chain(path, *, allow_missing_leaf=False):
    target = _lexical_absolute(path)
    current = Path(target.anchor)
    parts = target.parts[1:]
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if allow_missing_leaf and index == len(parts) - 1:
                return target
            raise
        if stat.S_ISLNK(mode):
            raise ValueError(f"refusing symlink component: {current}")
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise ValueError(f"parent component is not a directory: {current}")
    return target


def require_regular_no_symlink(path, *, allow_missing=False):
    path = _lstat_chain(path, allow_missing_leaf=allow_missing)
    if allow_missing and not path.exists():
        return path
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"path is not a regular file: {path}")
    return path


def ensure_private_dir(path):
    path = _lexical_absolute(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            mode = current.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError(f"private directory has unsafe component: {current}")
    path.chmod(0o700, follow_symlinks=False)
    return path


def ensure_private_file(path):
    path = require_regular_no_symlink(path)
    path.chmod(0o600, follow_symlinks=False)
    return path
```

`ensure_private_file()` uses the full-chain `require_regular_no_symlink()`. Directory creation is one component at a time and never uses `parents=True`; only the requested owned directory and newly created descendants receive mode 0700. Never chmod an existing ancestor outside the requested owned path.

- [ ] **Step 4: Run focused tests and Ruff**

Run: `uv run pytest tests/test_security.py tests/test_private_paths.py -q`

Expected: all pass on POSIX. Gate mode assertions with `os.name == "posix"` only where the platform cannot represent them.

Run: `uv run ruff check pico/security.py tests/test_security.py tests/test_private_paths.py`

Expected: no diagnostics.

- [ ] **Step 5: Commit**

```bash
git add pico/security.py tests/test_security.py tests/test_private_paths.py
git commit -m "feat(security): classify sensitive paths and private files"
```

### Task 3: Trusted Executables and Hardened Git/rg

**Files:**
- Create: `pico/safe_subprocess.py`
- Create: `tests/test_safe_subprocess.py`
- Modify: `pico/workspace.py`
- Modify: `tests/test_safety_invariants.py`

**Interfaces:**
- Produces: the four `pico.safe_subprocess` functions frozen in the cross-plan interface block.
- Consumes: Task 2 `require_regular_no_symlink()`.
- Later tasks store the returned `dict[str, str]` on `Pico.trusted_executables` and `ToolContext.trusted_executables`.

- [ ] **Step 1: Write failing lexical-root, PATH-spoof, and Git-config tests**

```python
def test_discover_lexical_repo_root_never_executes_git(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    child = repo / "src"
    child.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git executed")))
    assert discover_lexical_repo_root(child) == repo.resolve()


def test_trusted_executables_ignore_relative_and_workspace_path(tmp_path):
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake.chmod(0o755)
    trusted = build_trusted_executables(tmp_path, env={"PATH": f".:{tmp_path}:/usr/bin"}, names=("git",))
    assert trusted.get("git") != str(fake)
    assert trusted.get("git", "").startswith("/")


def test_hardened_git_disables_repo_fsmonitor(tmp_path, monkeypatch):
    captured = {}
    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    run_hardened_git("/usr/bin/git", ["status", "--short"], cwd=tmp_path)
    joined = " ".join(captured["argv"])
    assert "core.fsmonitor=false" in joined
    assert "--no-pager" in captured["argv"]
    git_names = {name for name in captured["env"] if name.startswith("GIT_")}
    assert git_names == {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"}
    assert captured["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert captured["env"]["GIT_CONFIG_GLOBAL"] == os.devnull


def test_external_path_symlink_to_workspace_binary_is_rejected(tmp_path):
    external_bin = tmp_path.parent / f"{tmp_path.name}-bin"
    external_bin.mkdir()
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
    fake.chmod(0o755)
    (external_bin / "git").symlink_to(fake)
    trusted = build_trusted_executables(
        tmp_path,
        env={"PATH": str(external_bin)},
        names=("git",),
    )
    assert "git" not in trusted
```

- [ ] **Step 2: Run the tests and confirm red**

Run: `uv run pytest tests/test_safe_subprocess.py -q`

Expected: module import failure.

- [ ] **Step 3: Implement the frozen executable map and hardened runners**

Use this concrete module shape:

```python
AUTO_TRUSTED_EXECUTABLES = ("git", "pwd", "ls", "stat", "file", "wc")
INTERNAL_TRUSTED_EXECUTABLES = ("rg",)
APPROVAL_TRUSTED_EXECUTABLES = (
    "python",
    "python3",
    "uv",
    "pytest",
    "ruff",
    "mypy",
    "pyright",
    "npm",
    "pnpm",
    "yarn",
    "cargo",
    "go",
    "sudo",
    "doas",
    "pkexec",
    "sh",
    "bash",
    "zsh",
    "node",
    "ruby",
    "perl",
    "php",
)
DEFAULT_TRUSTED_EXECUTABLES = (
    *AUTO_TRUSTED_EXECUTABLES,
    *INTERNAL_TRUSTED_EXECUTABLES,
    *APPROVAL_TRUSTED_EXECUTABLES,
)
_GIT_CONFIG_OVERRIDES = (
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "diff.external=",
    "credential.helper=",
    "protocol.ext.allow=never",
    "pager.status=false",
)


def discover_lexical_repo_root(cwd):
    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        if marker.is_symlink():
            raise ValueError(f"unsafe .git symlink: {marker}")
        if marker.is_dir() or marker.is_file():
            return candidate
    return current


def _safe_path_dirs(workspace_root, env):
    root = Path(workspace_root).resolve()
    result = []
    for raw in str((env or os.environ).get("PATH", "")).split(os.pathsep):
        candidate = Path(raw)
        if not raw or raw == "." or not candidate.is_absolute():
            continue
        resolved = candidate.resolve()
        if resolved == root or root in resolved.parents:
            continue
        mode = resolved.stat().st_mode
        if not stat.S_ISDIR(mode) or mode & (stat.S_IWGRP | stat.S_IWOTH):
            continue
        result.append(str(resolved))
    return result


def build_trusted_executables(workspace_root, *, env=None, names=()):
    root = Path(workspace_root).resolve()
    search_path = os.pathsep.join(_safe_path_dirs(workspace_root, env))
    result = {}
    for name in tuple(names or DEFAULT_TRUSTED_EXECUTABLES):
        found = shutil.which(name, path=search_path)
        if not found:
            continue
        resolved = Path(found).resolve()
        if resolved == root or root in resolved.parents:
            continue
        require_regular_no_symlink(resolved)
        mode = resolved.stat().st_mode
        if mode & (stat.S_IWGRP | stat.S_IWOTH) or not os.access(resolved, os.X_OK):
            continue
        result[name] = str(resolved)
    return result
```

`run_hardened_git()` builds a flat argv beginning with the absolute Git path and `--no-pager`, then appends `-c` plus each fixed hardening config before caller args. It uses a minimal environment containing the filtered PATH plus locale/home essentials, removes all inherited `GIT_*`, then adds only `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=os.devnull`. `run_hardened_rg()` sets `RIPGREP_CONFIG_PATH` to `os.devnull` and never accepts caller-supplied `--pre`.

The runtime freezes all names in `DEFAULT_TRUSTED_EXECUTABLES` before project-env loading. Automatic grammar still uses only `AUTO_TRUSTED_EXECUTABLES`; membership in the larger map never upgrades risk. An approved simple argv command executes only when its bare name was frozen to a trusted absolute path. If absent, ToolExecutor returns `trusted_executable_missing` with runner count zero. This makes `python -m pytest` executable with `shell=False` when a trusted parent-PATH Python existed at startup, while an arbitrary unknown binary remains ask-classified but cannot silently fall back to runtime PATH. Approved complex shell calls `subprocess.run(command, shell=True, executable=trusted_sh)` with the frozen `sh` path; if it is missing, runner count remains zero.

- [ ] **Step 4: Route WorkspaceContext bootstrap through the helper**

Change `WorkspaceContext.build(cwd, repo_root_override=None, executables=None)` so it:

1. uses `discover_lexical_repo_root(cwd)` before any subprocess;
2. builds or consumes the trusted map;
3. confirms the root with `run_hardened_git(git, ["rev-parse", "--show-toplevel"], cwd=lexical_root)` only when a trusted Git exists;
4. removes the non-essential startup `git log --oneline -5` call or executes it only through the helper;
5. stores `trusted_executables` on the context for later runtime wiring.

- [ ] **Step 5: Run focused and workspace tests**

Run: `uv run pytest tests/test_safe_subprocess.py tests/test_safety_invariants.py tests/test_prompt_prefix.py tests/test_context_sources.py -q`

Expected: all pass; fake workspace Git is never invoked.

Run: `uv run ruff check pico/safe_subprocess.py pico/workspace.py tests/test_safe_subprocess.py`

Expected: no diagnostics.

- [ ] **Step 6: Commit**

```bash
git add pico/safe_subprocess.py pico/workspace.py tests/test_safe_subprocess.py tests/test_safety_invariants.py
git commit -m "feat(security): freeze trusted subprocess executables"
```

### Task 4: Exact, Atomic, Private Project Configuration

**Files:**
- Modify: `pico/config.py`
- Modify: `pico/file_lock.py`
- Modify: `pico/cli_commands.py`
- Modify: `pico/cli_diagnostics.py`
- Modify: `pico/cli.py`
- Modify: `pico/providers/anthropic_compatible.py`
- Modify: `pico/providers/openai_compatible.py`
- Modify: `pico/providers/ollama.py`
- Modify: `tests/test_cli_commands.py`
- Modify: `tests/test_cli_diagnostics.py`
- Modify: `tests/test_safety_invariants.py`
- Modify: `tests/test_file_lock.py`
- Modify: `tests/test_provider_clients.py`
- Create: `tests/test_project_env_security.py`

**Interfaces:**
- Produces: `project_env_path()`, exact-root `read_project_env()/load_project_env()`, durable `write_project_env_assignments()`, and `validate_provider_base_url()`.
- Produces: `locked_file(path, *, require_lock=False)` opens a private regular no-follow lock; config passes `require_lock=True`.
- Consumes: Tasks 1–2 redaction/private helpers and Task 3's parent PATH snapshot in CLI assembly.
- A3 uses `write_project_env_assignments()` only in isolated temp repositories; it never passes a secret via argv.

- [ ] **Step 1: Add failing exact-root, denylist, codec, and atomicity tests**

```python
def test_project_env_never_falls_back_to_parent(tmp_path):
    parent = tmp_path / ".env"
    child = tmp_path / "repo"
    child.mkdir()
    parent.write_text("PICO_PROVIDER=anthropic\n", encoding="utf-8")
    assert project_env_path(child) == child.resolve() / ".env"
    assert read_project_env(child, warn=False) == {}


def test_secret_names_cannot_import_execution_control_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "PICO_SECRET_ENV_NAMES=PATH,PYTHONPATH\nPATH=./fake\nPYTHONPATH=./payload\nPICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    original_path = os.environ.get("PATH")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    loaded = load_project_env(tmp_path)
    assert loaded["PICO_PROVIDER"] == "deepseek"
    assert os.environ.get("PATH") == original_path
    assert "PYTHONPATH" not in os.environ


@pytest.mark.parametrize("value", (" a # b ", "quote'\"value", r"back\\slash=value"))
def test_project_env_quoted_codec_round_trips_special_values(tmp_path, value):
    write_project_env_assignments(tmp_path, {"PICO_TEST_SECRET": value})
    assert read_project_env(tmp_path, warn=False)["PICO_TEST_SECRET"] == value


def test_project_env_replace_failure_preserves_original(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"PICO_PROVIDER=deepseek\n")
    monkeypatch.setattr(Path, "replace", lambda self, target: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        write_project_env_assignments(tmp_path, {"PICO_PROVIDER": "anthropic"})
    assert env_path.read_bytes() == b"PICO_PROVIDER=deepseek\n"


def test_project_env_rejects_leaf_symlink(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-env"
    outside.write_text("PICO_PROVIDER=deepseek\n", encoding="utf-8")
    (tmp_path / ".env").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        read_project_env(tmp_path)


def test_project_env_existing_file_is_private_before_read(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX mode assertion")
    env_path = tmp_path / ".env"
    env_path.write_text("PICO_PROVIDER=deepseek\n", encoding="utf-8")
    env_path.chmod(0o644)
    assert read_project_env(tmp_path, warn=False)["PICO_PROVIDER"] == "deepseek"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_project_env_chmod_failure_fails_before_returning_values(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("PICO_API_KEY=opaque-value\n", encoding="utf-8")
    real_chmod = Path.chmod

    def fail_env_chmod(self, *args, **kwargs):
        if self == env_path:
            raise PermissionError("chmod denied")
        return real_chmod(self, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", fail_env_chmod)
    with pytest.raises(PermissionError, match="chmod denied"):
        read_project_env(tmp_path, warn=False)


def test_project_env_rejects_symlinked_private_parent_and_lock(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-config"
    outside.mkdir()
    (tmp_path / ".pico").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        write_project_env_assignments(tmp_path, {"PICO_PROVIDER": "deepseek"})
    assert list(outside.iterdir()) == []

    (tmp_path / ".pico").unlink()
    (tmp_path / ".pico").mkdir(mode=0o700)
    lock_target = outside / "lock-target"
    lock_target.write_text("untouched", encoding="utf-8")
    (tmp_path / ".pico" / "project-env.lock").symlink_to(lock_target)
    with pytest.raises(ValueError, match="symlink"):
        write_project_env_assignments(tmp_path, {"PICO_PROVIDER": "deepseek"})
    assert lock_target.read_text(encoding="utf-8") == "untouched"


def test_project_env_temp_fsync_failure_preserves_original(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    original = b"PICO_PROVIDER=deepseek\n"
    env_path.write_bytes(original)
    real_fsync = os.fsync
    calls = {"count": 0}

    def fail_first_fsync(fd):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("temp fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_fsync)
    with pytest.raises(OSError, match="temp fsync failed"):
        write_project_env_assignments(tmp_path, {"PICO_PROVIDER": "anthropic"})
    assert env_path.read_bytes() == original


@pytest.mark.parametrize(
    "url",
    (
        "https://user:opaque-password@example.test/v1",
        "https://example.test/v1?api_key=opaque-value",
        "https://example.test/v1?token=opaque-value",
    ),
)
@pytest.mark.parametrize(
    "client_factory",
    (
        lambda url: OpenAICompatibleModelClient(
            model="test", base_url=url, api_key="safe-test-key", temperature=0.0, timeout=1,
        ),
        lambda url: AnthropicCompatibleModelClient(
            model="test", base_url=url, api_key="safe-test-key", temperature=0.0, timeout=1,
        ),
        lambda url: OllamaModelClient(
            model="test", host=url, temperature=0.0, top_p=1.0, timeout=1,
        ),
    ),
)
def test_credential_bearing_base_url_is_rejected_at_config_and_client_boundaries(
    tmp_path, url, client_factory, capsys
):
    code = main([
        "--cwd", str(tmp_path), "init", "--provider", "deepseek", "--base-url", url,
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "opaque" not in captured.out + captured.err
    assert not (tmp_path / ".env").exists()
    with pytest.raises(ValueError, match="provider_base_url_credentials"):
        client_factory(url)
```

- [ ] **Step 2: Add failing CLI secret-input tests**

```python
def test_init_rejects_api_key_argv_without_echoing_value(tmp_path, capsys):
    secret = "sk-cli-secret-123456789"
    code = main(["--cwd", str(tmp_path), "init", "--api-key", secret])
    captured = capsys.readouterr()
    assert code == 2
    assert secret not in captured.out + captured.err


def test_config_set_secret_reads_stdin_and_writes_private_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-stdin-secret-123456789\n"))
    code = main(["--cwd", str(tmp_path), "config", "set-secret", "PICO_DEEPSEEK_API_KEY", "--stdin"])
    assert code == 0
    assert read_project_env(tmp_path, warn=False)["PICO_DEEPSEEK_API_KEY"] == "sk-stdin-secret-123456789"
    assert "sk-stdin" not in capsys.readouterr().out
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_init_writes_only_non_secret_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(getpass, "getpass", lambda prompt: (_ for _ in ()).throw(AssertionError("getpass called")))
    code = main(["--cwd", str(tmp_path), "init", "--provider", "deepseek"])
    assert code == 0
    values = read_project_env(tmp_path, warn=False)
    assert values["PICO_PROVIDER"] == "deepseek"
    assert "PICO_DEEPSEEK_API_KEY" not in values
```

- [ ] **Step 3: Run the focused tests and confirm red**

Run: `uv run pytest tests/test_project_env_security.py tests/test_file_lock.py tests/test_cli_commands.py tests/test_cli_diagnostics.py tests/test_provider_clients.py -q`

Expected: failures for parent lookup, unsafe imports, non-atomic write, `--api-key` acceptance, missing `config set-secret`, and credential-bearing base URLs accepted by config/client boundaries.

- [ ] **Step 4: Implement exact-root parsing and reversible encoding**

Use `json.dumps(value, ensure_ascii=False)` for every newly rendered value and `json.loads()` when a value begins/ends with double quotes; retain legacy single-quoted/unquoted parsing for existing files. Introduce an immutable execution-control deny check that runs before import allow checks:

```python
_EXECUTION_ENV_EXACT_DENY = {"PATH", "HOME", "SHELL", "PYTHONPATH", "BASH_ENV", "ENV"}
_PROJECT_ENV_ALLOWED = {
    "PICO_PROVIDER",
    "PICO_SECRET_ENV_NAMES",
    *(name for names in MODEL_ENV_NAMES.values() for name in names),
    *(name for names in BASE_URL_ENV_NAMES.values() for name in names),
    *(name for names in API_KEY_ENV_NAMES.values() for name in names),
}


def _may_import_project_env(name):
    upper = str(name).upper()
    if upper in _EXECUTION_ENV_EXACT_DENY or upper.startswith(("LD_", "DYLD_")):
        return False
    return upper.startswith("PICO_") or upper in _PROJECT_ENV_ALLOWED
```

`read_project_env()` computes only `project_env_path(workspace_root)`, rejects a symlinked component, calls `ensure_private_file()` on an existing `.env` before opening it, and returns no values if permission hardening fails. It never walks to a parent directory.

Harden `locked_file()` with `os.open` flags `O_RDWR | O_CREAT | O_APPEND | O_CLOEXEC` plus `O_NOFOLLOW` when available, explicit mode 0600, `fstat()` regular-file validation, and post-open `fchmod(0600)`. It checks the full parent chain before open and verifies leaf inode/type after open. When `require_lock=True` and `fcntl` is unavailable or `flock` fails, close and raise before yielding; it never silently proceeds unlocked.

`write_project_env_assignments()` uses exact lock path `<root>/.pico/project-env.lock`, calls `ensure_private_dir(root / ".pico")`, and acquires `locked_file(lock_path, require_lock=True)`. Under the lock it rereads the current file, removes duplicate assignments for updated names, creates a same-directory temp with `tempfile.mkstemp(prefix=".pico-env-", dir=root)`, sets mode 0600 before writing, then performs write → flush → fsync → replace → chmod 0600 → root-directory fsync. A failure before replace leaves original bytes unchanged; cleanup removes only the temp inode created by this call.

- [ ] **Step 5: Implement the CLI surface**

Remove `api_key` from `_parse_init_tokens()` and `_init_usage_error()`. `handle_init()` leaves any existing API-key assignment untouched and prints the canonical follow-up command when missing.

Extend `handle_config()` with exact grammar:

```text
config show
config set-secret <ENV_NAME>
config set-secret <ENV_NAME> --stdin
```

Validate the name with `ENV_KEY_PATTERN` and `is_secret_env_name()`. TTY mode calls `getpass.getpass`; `--stdin` reads all input, removes exactly one trailing newline, and rejects empty, NUL, `\r`, or embedded `\n`. Pass only `{name: value}` to `write_project_env_assignments()` and render no value, hash, or length.

- [ ] **Step 6: Sanitize config diagnostics and base URLs**

Implement the shared config validator with no raw URL in its exception:

```python
_SECRET_QUERY_KEYS = {
    "api_key",
    "access_key",
    "access_token",
    "auth_token",
    "token",
    "secret",
    "password",
    "credential",
}


def validate_provider_base_url(value):
    raw = str(value or "")
    parsed = urllib.parse.urlsplit(raw)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider_base_url_credentials")
    if any(key.casefold().replace("-", "_") in _SECRET_QUERY_KEYS for key, _ in query):
        raise ValueError("provider_base_url_credentials")
    if any(contains_secret_material(item, env={}) for _, item in query):
        raise ValueError("provider_base_url_credentials")
    return raw
```

`handle_init()` validates before writing `.env`; invalid input leaves the old file byte-identical. `_build_model_client()` validates the effective CLI/env URL before constructing a client. Each Anthropic/OpenAI-compatible/Ollama client constructor independently validates programmatic input before saving the URL. `collect_config()` reads exact-root env without mutating `os.environ` and calls the same validator. Every boundary reports only stable `provider_base_url_credentials`; it never echoes userinfo, query, value, or full URL.

- [ ] **Step 7: Run config, CLI, and safety tests**

Run: `uv run pytest tests/test_project_env_security.py tests/test_file_lock.py tests/test_cli_commands.py tests/test_cli_diagnostics.py tests/test_provider_clients.py tests/test_safety_invariants.py tests/test_config_context.py -q`

Expected: all pass; no captured output contains any test sentinel.

Run: `uv run ruff check pico/config.py pico/file_lock.py pico/cli_commands.py pico/cli_diagnostics.py pico/cli.py pico/providers tests/test_project_env_security.py tests/test_file_lock.py tests/test_provider_clients.py`

Expected: no diagnostics.

- [ ] **Step 8: Commit**

```bash
git add pico/config.py pico/file_lock.py pico/cli_commands.py pico/cli_diagnostics.py pico/cli.py pico/providers/anthropic_compatible.py pico/providers/openai_compatible.py pico/providers/ollama.py tests/test_project_env_security.py tests/test_file_lock.py tests/test_provider_clients.py tests/test_cli_commands.py tests/test_cli_diagnostics.py tests/test_safety_invariants.py
git commit -m "feat(config): secure project secret input and env writes"
```

### Task 5: Private, Safe Session and Run Persistence

**Files:**
- Modify: `pico/session_store.py`
- Modify: `pico/run_store.py`
- Modify: `pico/agent_loop.py`
- Modify: `pico/runtime.py`
- Modify: `pico/cli.py`
- Modify: `tests/test_session_store_migrator.py`
- Modify: `tests/test_session_store.py`
- Modify: `tests/test_run_store.py`
- Modify: `tests/test_pico.py`
- Modify: `tests/test_agent_loop_digest.py`
- Modify: `tests/test_safety_invariants.py`

**Interfaces:**
- Produces: SessionStore load returns the same sanitized payload it writes; RunStore and raw tool-result files are private; Pico retains an immutable `redaction_env` snapshot used by all later boundaries.
- Consumes: Tasks 1–2 helpers and Task 4 project-env values when assembling the redactor before `load()`.
- A2 injects the same redactor into CheckpointStore; it does not change SessionStore semantics.

- [ ] **Step 1: Add failing pre-load, in-memory parity, raw-result, and permission tests**

```python
def test_v3_load_sanitizes_before_return_and_rewrites_once(tmp_path):
    secret = "github_pat_A123456789012345678901234567890"
    store = SessionStore(tmp_path, redactor=lambda value: redact_artifact(value, env={}))
    session = valid_v3_session("resume-safe")
    session["messages"] = [{"role": "user", "content": secret, "_pico_meta": {}}]
    path = store.path("resume-safe")
    path.write_text(json.dumps(session), encoding="utf-8")
    loaded = store.load("resume-safe")
    assert secret not in json.dumps(loaded)
    assert secret not in path.read_text(encoding="utf-8")
    backups = list((tmp_path / "backup").glob("resume-safe*.json"))
    assert len(backups) == 1
    assert secret in backups[0].read_text(encoding="utf-8")
    store.load("resume-safe")
    assert len(list((tmp_path / "backup").glob("resume-safe*.json"))) == 1


def test_commit_session_keeps_memory_and_disk_on_same_safe_payload(tmp_path):
    secret = "sk-session-secret-123456789"
    agent = build_agent(tmp_path, [])
    agent.memory.set_task_summary(secret)
    agent._sync_working_memory()
    _commit_session(agent, messages=(_plain_message("user", secret),))
    persisted = json.loads(Path(agent.session_path).read_text(encoding="utf-8"))
    assert secret not in json.dumps(agent.session)
    assert agent.session == persisted
    assert secret not in json.dumps(agent.memory.to_dict())


def test_large_tool_result_writes_only_redacted_private_body(tmp_path):
    agent = _stub_agent(tmp_path)
    agent.redact_text.side_effect = lambda value: redact_text(value, env={})
    secret = "github_pat_A123456789012345678901234567890"
    _prepare_tool_result(agent, content=(secret + "\n") * 100, tool_name="read_file", tool_args={"path": "x"})
    raw_file = next((agent.current_run_dir / "tool_results").glob("*.txt"))
    assert secret not in raw_file.read_text(encoding="utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(raw_file.stat().st_mode) == 0o600


def test_programmatic_resume_sanitizes_opaque_process_secret_before_first_request(tmp_path, monkeypatch):
    secret = "opaque-process-value-123456789"
    monkeypatch.setenv("PICO_TEST_API_KEY", secret)
    original = build_agent(tmp_path, [])
    raw = dict(original.session)
    raw["messages"] = [
        {"role": "user", "content": secret, "_pico_meta": {"created_at": "test"}}
    ]
    original.session_store.path(raw["id"]).write_text(json.dumps(raw), encoding="utf-8")
    client = FakeModelClient(["<final>safe</final>"])
    resumed = Pico.from_session(
        model_client=client,
        workspace=original.workspace,
        session_store=original.session_store,
        session_id=raw["id"],
        approval_policy="auto",
    )
    resumed.ask("continue")
    assert secret not in json.dumps(client.prompts)
    assert secret not in json.dumps(resumed.session)
```

- [ ] **Step 2: Run focused tests and confirm red**

Run: `uv run pytest tests/test_session_store_migrator.py tests/test_run_store.py tests/test_agent_loop_digest.py tests/test_pico.py::test_programmatic_resume_sanitizes_opaque_process_secret_before_first_request -q`

Expected: new tests fail because v3 load returns raw, candidate memory stays raw, and raw tool results are written before redaction with default mode.

- [ ] **Step 3: Wire the SessionStore redactor before first load**

In `build_agent()` first read the exact project env without mutating the process. Build one immutable redaction snapshot from process plus project values before the first SessionStore load, while keeping it completely separate from the allowlisted execution environment:

```python
process_env = dict(os.environ)
project_env = read_project_env(workspace.repo_root, warn=True)
merged_redaction_env = dict(process_env)
configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
configured_secret_names.update(str(name).upper() for name in getattr(args, "secret_env_names", ()))
configured_secret_names.update(
    item.strip().upper()
    for source in (process_env, project_env)
    for item in source.get("PICO_SECRET_ENV_NAMES", "").split(",")
    if item.strip()
)
for index, (name, value) in enumerate(project_env.items()):
    if (
        name in merged_redaction_env
        and merged_redaction_env[name] != value
        and securitylib.is_secret_env_name(name, configured_secret_names)
    ):
        merged_redaction_env[f"PICO_REDACTION_COLLISION_{index}_SECRET"] = merged_redaction_env[name]
    merged_redaction_env[name] = value
redaction_env = MappingProxyType(merged_redaction_env)
redactor = lambda value: securitylib.redact_artifact(
    value,
    env=redaction_env,
    secret_env_names=configured_secret_names,
)
store = SessionStore(workspace.repo_root + "/.pico/sessions", redactor=redactor)
```

Store this same mapping as `agent.redaction_env`. Only after the store has its redactor may the runtime call `load_project_env()` for the separately allowlisted process imports. Denied execution-control variables may exist in `redaction_env` so their text can be scrubbed, but they never enter the process/execution environment.

`Pico.from_session()` repeats the complete guard for programmatic callers: derive the workspace root, read exact-root project values, merge a snapshot with the current process environment, combine configured and explicit `secret_env_names`, set the store redactor, and only then call `load()`. It must not create a store with an identity/default redactor and patch it after load.

For v3 load, sanitize and validate under the existing lock. When the payload changes, write a private original backup then durable atomic safe replacement; return the safe payload. A second load is byte-idempotent and creates no backup.

- [ ] **Step 4: Make memory/disk session parity explicit**

In `_commit_session()` sanitize the complete candidate once, append already-sanitized messages to that candidate, save it, assign that exact object to `agent.session`, and rebuild `agent.memory` from `candidate["working_memory"]`. At turn start sanitize before `memory.set_task_summary()` and TaskState creation.

- [ ] **Step 5: Harden run/raw writes**

RunStore constructors call `ensure_private_dir`; every atomic JSON temp and final file is 0600. `append_trace()` opens with `O_APPEND | O_CREAT | O_WRONLY` and explicit mode 0600, then rechecks no-follow type before append.

At the start of `_prepare_tool_result()` use:

```python
safe_content = agent.redact_text(content)
display_content = safe_content
```

Digest, source hash, and raw file all use `safe_content`; create the raw directory 0700 and file 0600. Log only the exception type on write failure.

- [ ] **Step 6: Run session/run/digest/safety tests**

Run: `uv run pytest tests/test_session_store.py tests/test_session_store_migrator.py tests/test_run_store.py tests/test_agent_loop_digest.py tests/test_pico.py tests/test_safety_invariants.py -q`

Expected: all pass; raw backup remains exact but private, every normal observation is safe.

Run: `uv run ruff check pico/session_store.py pico/run_store.py pico/agent_loop.py pico/runtime.py pico/cli.py`

Expected: no diagnostics.

- [ ] **Step 7: Commit**

```bash
git add pico/session_store.py pico/run_store.py pico/agent_loop.py pico/runtime.py pico/cli.py tests/test_session_store.py tests/test_session_store_migrator.py tests/test_run_store.py tests/test_agent_loop_digest.py tests/test_pico.py tests/test_safety_invariants.py
git commit -m "feat(security): sanitize and privatize runtime artifacts"
```

### Task 6: Provider Request, Action, Error, and Working-Memory Boundaries

**Files:**
- Modify: `pico/security.py`
- Modify: `pico/context_manager.py`
- Modify: `pico/context/renderer.py`
- Modify: `pico/context/sources.py`
- Modify: `pico/agent_loop.py`
- Modify: `pico/runtime.py`
- Modify: `pico/cli_start.py`
- Modify: `pico/providers/anthropic_compatible.py`
- Modify: `pico/providers/openai_compatible.py`
- Modify: `pico/providers/ollama.py`
- Modify: `tests/test_context_manager.py`
- Modify: `tests/test_agent_loop.py`
- Modify: `tests/test_provider_clients.py`
- Modify: `tests/test_debug_logging.py`
- Modify: `tests/test_runtime_report.py`
- Create: `tests/test_secret_boundaries.py`

**Interfaces:**
- Produces: `sanitize_provider_payload()` and `SensitiveDataBlockedError`; decoded actions cannot execute secret arguments or print secret text.
- Consumes: Task 1 redaction/detection and Task 5 safe session commit.
- A3's canary fake client captures `request["system"]` and `request["messages"]` to assert this boundary.

- [ ] **Step 1: Add failing Provider and Action boundary tests**

```python
class CapturingClient:
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, response):
        self.response = response
        self.requests = []
    def complete_v2(self, **request):
        self.requests.append(request)
        return self.response


def final_response(text):
    return Response(
        stop_reason=StopReason.END_TURN,
        content=[{"type": "text", "text": text}],
    )


def build_agent_with_client(tmp_path, client):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def all_normal_artifact_bytes(root):
    chunks = []
    for path in (Path(root) / ".pico").rglob("*"):
        if path.is_file() and "/backup/" not in path.as_posix() and "/blobs/" not in path.as_posix():
            chunks.append(path.read_bytes())
    return b"\n".join(chunks)


def test_provider_request_sanitizes_system_messages_and_injection(tmp_path):
    secret = "github_pat_A123456789012345678901234567890"
    client = CapturingClient(final_response("done"))
    agent = build_agent_with_client(tmp_path, client)
    agent.prefix += "\n" + secret
    agent.session["working_memory"]["task_summary"] = secret
    agent.ask("token budget")
    wire = json.dumps(client.requests)
    assert secret not in wire


def test_secret_tool_action_is_rejected_before_runner(tmp_path):
    secret = "sk-tool-action-secret-123456789"
    response = Response(
        stop_reason=StopReason.TOOL_USE,
        content=[{
            "type": "tool_use",
            "id": "toolu_1",
            "name": "write_file",
            "input": {"path": "x.txt", "content": secret},
        }],
    )
    agent = build_agent_with_client(tmp_path, CapturingClient(response))
    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner
    result = agent.ask("write it")
    assert secret not in result
    runner.assert_not_called()


def test_opaque_secret_mapping_value_in_action_is_rejected_before_runner(tmp_path):
    response = Response(
        stop_reason=StopReason.TOOL_USE,
        content=[{
            "type": "tool_use",
            "id": "toolu_opaque",
            "name": "write_file",
            "input": {"path": "x.txt", "content": "safe", "credential": "opaque-value"},
        }],
    )
    agent = build_agent_with_client(tmp_path, CapturingClient(response))
    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner
    agent.ask("write it")
    runner.assert_not_called()


def test_provider_final_and_cli_error_never_print_secret(tmp_path, capsys):
    secret = "github_pat_A123456789012345678901234567890"
    agent = build_agent_with_client(tmp_path, CapturingClient(final_response(secret)))
    run_agent_once(agent, ["answer"])
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_provider_residual_scan_blocks_when_primary_sanitizer_misses(
    tmp_path, monkeypatch, caplog
):
    secret = "github_pat_" + "A" * 32
    client = CapturingClient(final_response("must not run"))
    agent = build_agent_with_client(tmp_path, client)
    agent.prefix = secret
    monkeypatch.setattr(securitylib, "redact_artifact", lambda value, **kwargs: value)
    with pytest.raises(SensitiveDataBlockedError):
        agent.ask("continue")
    assert client.requests == []
    assert secret not in caplog.text
    assert secret.encode() not in all_normal_artifact_bytes(tmp_path)
```

- [ ] **Step 2: Add failing HTTP/log canary tests**

Parameterize the Anthropic- and OpenAI-compatible clients. Mock `urllib.request.urlopen` to raise this error:

```python
secret = "github_pat_" + "B" * 32
credential_url = f"https://user:{secret}@example.test/v1?api_key={secret}"
error = urllib.error.HTTPError(
    credential_url,
    401,
    "unauthorized",
    hdrs={},
    fp=io.BytesIO(f'{{"error":"{secret}"}}'.encode()),
)
monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=error))
```

Call each client once and assert the raised message is exactly its stable backend/status category, not an HTTP body or URL. Then exercise the CLI run-error handler with the same exception and assert `secret` plus `credential_url` are absent from `caplog.text`, captured stderr, trace JSONL, and report JSON. The fake Provider call count is one at the client unit boundary and zero for any later retry; no test logs `repr(error)`.

- [ ] **Step 3: Run tests and confirm red**

Run: `uv run pytest tests/test_secret_boundaries.py tests/test_context_manager.py tests/test_agent_loop.py tests/test_provider_clients.py tests/test_debug_logging.py -q`

Expected: failures show raw system/injection/final/action/error material still crosses at least one boundary.

- [ ] **Step 4: Implement Provider payload sanitation and residual check**

Add `sanitize_provider_payload()` to `pico/security.py`. It deep-copies and redacts system/messages, serializes only the safe copy for a residual high-confidence scan, and raises `SensitiveDataBlockedError` without raw text when material remains. After `build_request_messages()` and before token/cache metrics, call it:

```python
system, messages = securitylib.sanitize_provider_payload(
    system,
    messages,
    env=self.agent.redaction_env,
    secret_env_names=self.agent.secret_env_names,
)
```

Compute cache keys and token metrics from the returned safe values only.

- [ ] **Step 5: Implement the decoded Action boundary**

Immediately after `decode_action()`:

- replace Final/Retry text with `agent.redact_text(text)`;
- for ToolAction, compute `safe_arguments = redact_artifact(arguments, env=agent.redaction_env, secret_env_names=agent.secret_env_names)` and also scan the serialized original for high-confidence text; if `safe_arguments != arguments` or the scan is true, create a rejected ToolExecutionResult-equivalent pair with `tool_error_code=sensitive_content_block`, never execute the redacted argument copy, never call `agent.execute_tool`, and continue the model loop;
- sanitize terminal final before returning and before CLI print.

Ensure `agent.memory` is rebuilt from safe session state after every commit, not only the initial user commit.

- [ ] **Step 6: Stabilize provider and CLI errors**

Provider clients may include only backend family + HTTP status/category in exceptions. Never include HTTP body, SDK error object, base URL userinfo/query, request headers, or response payload. `cli_start` prints `agent.redact_text(str(exc))[:300]`; AgentLoop finalization stores the same bounded safe message and logs label + exception type without `logger.exception()` on security-sensitive boundaries.

- [ ] **Step 7: Run boundary tests and commit**

Run: `uv run pytest tests/test_secret_boundaries.py tests/test_context_manager.py tests/test_agent_loop.py tests/test_provider_clients.py tests/test_debug_logging.py tests/test_runtime_report.py -q`

Expected: all pass; fake ToolAction runner count is zero.

Run: `uv run ruff check pico/context_manager.py pico/context/renderer.py pico/context/sources.py pico/agent_loop.py pico/runtime.py pico/cli_start.py pico/providers tests/test_secret_boundaries.py`

Expected: no diagnostics.

```bash
git add pico/security.py pico/context_manager.py pico/context/renderer.py pico/context/sources.py pico/agent_loop.py pico/runtime.py pico/cli_start.py pico/providers tests/test_secret_boundaries.py tests/test_context_manager.py tests/test_agent_loop.py tests/test_provider_clients.py tests/test_debug_logging.py tests/test_runtime_report.py
git commit -m "feat(security): enforce provider and action boundaries"
```

### Task 7: Non-Following Bootstrap and Index Readers

**Files:**
- Modify: `pico/workspace.py`
- Modify: `pico/repo_map.py`
- Modify: `pico/memory/block_store.py`
- Modify: `pico/memory/refresher.py`
- Modify: `pico/context/sources.py`
- Modify: `pico/workspace_observer.py`
- Modify: `pico/tool_executor.py`
- Create: `tests/test_bootstrap_read_safety.py`
- Modify: `tests/test_context_sources.py`
- Modify: `tests/memory/test_repo_map.py`
- Modify: `tests/memory/test_block_store.py`
- Modify: `tests/test_workspace_observer.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_tool_executor.py`

**Interfaces:**
- Produces: all automatic readers use lexical regular-file checks and frozen Git/rg helpers; no new public class.
- Consumes: Task 2 path guards and Task 3 trusted subprocess functions.
- Later tasks may assume `WorkspaceContext.project_docs`, RepoMap symbols, memory index entries, observer state, and HEAD fallback came only from safe reads/calls.

- [ ] **Step 1: Add failing symlink bootstrap and index tests**

```python
def test_workspace_context_does_not_follow_readme_symlink_to_secret(tmp_path):
    (tmp_path / ".env").write_text("PICO_TOKEN=bootstrap-secret-123456789\n", encoding="utf-8")
    (tmp_path / "README.md").symlink_to(tmp_path / ".env")
    workspace = WorkspaceContext.build(tmp_path)
    assert "bootstrap-secret" not in workspace.stable_text()
    assert "README.md" not in workspace.project_docs


def test_workspace_context_does_not_follow_agents_symlink_outside(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-agents"
    outside.write_text("outside-secret-123456789", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to(outside)
    workspace = WorkspaceContext.build(tmp_path)
    assert "outside-secret" not in workspace.stable_text()


def test_bootstrap_reader_rejects_symlinked_parent_directory(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-docs"
    outside.mkdir()
    (outside / "README.md").write_text("parent-link-secret", encoding="utf-8")
    (tmp_path / "docs").symlink_to(outside, target_is_directory=True)
    assert _safe_index_file(tmp_path, tmp_path / "docs" / "README.md") is None


def test_repo_map_and_memory_index_skip_symlink_files(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-source"
    outside.write_text("def SecretSymbol():\n    pass\n", encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(outside)
    repo_map = RepoMap(tmp_path)
    repo_map.scan()
    assert "SecretSymbol" not in json.dumps(repo_map.lookup("SecretSymbol"))

    memory = tmp_path / ".pico" / "memory" / "notes"
    memory.mkdir(parents=True)
    (memory / "linked.md").symlink_to(outside)
    store = BlockStore(memory.parent, tmp_path / "user-memory")
    assert all("linked.md" not in entry.path for entry in store.list())
```

- [ ] **Step 2: Add failing malicious Git/rg configuration tests**

Use trusted real binaries from `build_trusted_executables()` and skip only when the relevant binary is absent. Initialize a temp Git repository, create an executable fsmonitor script whose only side effect is writing `fsmonitor-ran`, and configure the repository with `core.fsmonitor=<absolute script>`. Call `WorkspaceContext.build()` and `WorkspaceObserver.capture()` with `{"git": trusted_git}` and assert the marker does not exist.

For rg, create `pre.sh` whose body contains `#!/bin/sh`, `touch '<absolute marker path>'`, and `cat "$1"`, plus a config containing `--pre=<absolute pre.sh>` and `--pre-glob=*.txt`. Set inherited `RIPGREP_CONFIG_PATH`, search a normal `.txt` file through the production search tool with `{"rg": trusted_rg}`, and assert the expected match is returned while the marker does not exist. The subprocess-capture assertion also requires child `RIPGREP_CONFIG_PATH == os.devnull` and forbids `--pre`, `--pre-glob`, and `--config` in argv.

- [ ] **Step 3: Run tests and confirm red**

Run: `uv run pytest tests/test_bootstrap_read_safety.py tests/test_context_sources.py tests/memory/test_repo_map.py tests/memory/test_block_store.py tests/test_workspace_observer.py -q`

Expected: current WorkspaceContext/RepoMap/BlockStore follow at least one symlink or use unhardened Git.

- [ ] **Step 4: Implement one lexical file-read predicate at each owner boundary**

Before every automatic read:

```python
def _safe_index_file(root, candidate):
    root = Path(root).resolve(strict=True)
    candidate = Path(candidate)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(os.fspath(candidate)))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    if is_sensitive_path(relative.as_posix()):
        return None
    try:
        return require_regular_no_symlink(candidate)
    except (FileNotFoundError, ValueError, OSError):
        return None
```

Workspace/global AGENTS, README/manifests, RepoMap sources, memory files, and context sources must call this before `read_text/stat`. Global AGENTS uses the same regular non-symlink check and Provider redaction, but is allowed outside the workspace because it is explicit user configuration.

- [ ] **Step 5: Route all production Git/rg callers through Task 3**

`WorkspaceContext`, `WorkspaceObserver`, and ToolExecutor's `git show HEAD:path` call `run_hardened_git()`. The search tool calls `run_hardened_rg()` with fixed args; no caller passes `--pre`, `--pre-glob`, `--config`, or environment-derived flags.

- [ ] **Step 6: Run focused tests and commit**

Run: `uv run pytest tests/test_bootstrap_read_safety.py tests/test_context_sources.py tests/memory/test_repo_map.py tests/memory/test_block_store.py tests/test_workspace_observer.py tests/test_tools.py tests/test_tool_executor.py -q`

Expected: all pass; neither malicious marker exists.

Run: `uv run ruff check pico/workspace.py pico/repo_map.py pico/memory/block_store.py pico/memory/refresher.py pico/context/sources.py pico/workspace_observer.py pico/tool_executor.py tests/test_bootstrap_read_safety.py`

Expected: no diagnostics.

```bash
git add pico/workspace.py pico/repo_map.py pico/memory/block_store.py pico/memory/refresher.py pico/context/sources.py pico/workspace_observer.py pico/tool_executor.py tests/test_bootstrap_read_safety.py tests/test_context_sources.py tests/memory/test_repo_map.py tests/memory/test_block_store.py tests/test_workspace_observer.py tests/test_tools.py tests/test_tool_executor.py
git commit -m "fix(security): stop bootstrap readers following unsafe files"
```

### Task 8: Sensitive Direct Tools, Memory, Search, and Snapshot Inputs

**Files:**
- Modify: `pico/tools.py`
- Modify: `pico/tool_executor.py`
- Modify: `pico/runtime.py`
- Modify: `pico/memory/tools.py`
- Modify: `pico/memory/block_store.py`
- Modify: `pico/recovery_policy.py`
- Modify: `pico/workspace_snapshot.py`
- Create: `tests/test_sensitive_tools.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_tool_executor.py`
- Modify: `tests/memory/test_memory_tools.py`
- Modify: `tests/memory/test_block_store.py`
- Modify: `tests/test_recovery_policy.py`
- Modify: `tests/test_workspace_snapshot.py`

**Interfaces:**
- Produces: stable `sensitive_path_block` / `sensitive_content_block`; `snapshot_eligibility()` adds `secret_env_names=()` and returns `sensitive_path | sensitive_content` without writing a blob.
- Consumes: Tasks 1–3 security and hardened rg, Task 7 safe readers.
- A2 relies on snapshot eligibility being pure with respect to the blob store: it reports a decision but never writes bytes itself.

- [ ] **Step 1: Add failing direct-tool matrix tests**

```python
@pytest.mark.parametrize(
    ("name", "arguments"),
    (
        ("read_file", {"path": ".env"}),
        ("search", {"pattern": "secret", "path": ".ssh"}),
        ("write_file", {"path": "client.pem", "content": "x"}),
        ("patch_file", {"path": ".pico/sessions/s.json", "old_text": "x", "new_text": "y"}),
    ),
)
def test_sensitive_direct_paths_are_rejected_before_runner(tmp_path, name, arguments):
    prepare_existing_target_when_needed(tmp_path, arguments)
    agent = build_agent(tmp_path, [])
    runner = Mock(return_value="must not run")
    agent.tools[name]["run"] = runner
    result = agent.execute_tool(name, arguments)
    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "sensitive_path_block"
    assert result.metadata["security_event_type"] == "sensitive_access_block"
    runner.assert_not_called()
    assert list((tmp_path / ".pico" / "checkpoints" / "blobs").rglob("*")) == []


def test_secret_content_write_is_rejected_but_security_prose_is_allowed(tmp_path):
    agent = build_agent(tmp_path, [])
    secret = "github_pat_A123456789012345678901234567890"
    blocked = agent.execute_tool("write_file", {"path": "notes.txt", "content": secret})
    assert blocked.metadata["tool_error_code"] == "sensitive_content_block"
    allowed = agent.execute_tool("write_file", {"path": "policy.txt", "content": "password policy"})
    assert allowed.metadata["tool_status"] == "ok"


def test_list_files_may_name_sensitive_entries_without_reading_metadata_or_content(tmp_path):
    (tmp_path / ".env").write_text("PICO_API_KEY=opaque-value", encoding="utf-8")
    result = build_agent(tmp_path, []).execute_tool("list_files", {"path": "."})
    sensitive_line = next(line for line in result.content.splitlines() if line.startswith(".env"))
    assert sensitive_line == ".env [sensitive]"
    assert "opaque-value" not in result.content
```

- [ ] **Step 2: Add failing search, memory, and snapshot tests**

Create `.env` and normal source files with the same sentinel. Exercise search once with a frozen trusted `rg` entry and once with no `rg` entry so the Python fallback is selected; monkeypatch runtime `shutil.which` to raise if called in either branch. Assert `.env` never appears while normal source does. Assert `memory_save`, `/save`, `append_agent_note`, and `write_agent_topic` reject a concrete token but accept `password policy`. Monkeypatch `hash_file_bytes` in workspace snapshot to fail if called for `.env`.

Add snapshot cases:

```python
def test_snapshot_excludes_sensitive_path_and_content(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PICO_TOKEN=alpha123456789\n", encoding="utf-8")
    (tmp_path / "source.py").write_text("TOKEN=alpha123456789\n", encoding="utf-8")
    monkeypatch.setenv("PICO_TOKEN", "alpha123456789")
    assert snapshot_eligibility(tmp_path, ".env")["ineligible_reason"] == "sensitive_path"
    assert snapshot_eligibility(tmp_path, "source.py")["ineligible_reason"] == "sensitive_content"
```

- [ ] **Step 3: Run tests and confirm red**

Run: `uv run pytest tests/test_sensitive_tools.py tests/test_tools.py tests/test_tool_executor.py tests/memory/test_memory_tools.py tests/memory/test_block_store.py tests/test_recovery_policy.py tests/test_workspace_snapshot.py -q`

Expected: current tools read/write sensitive paths and snapshot returns eligible.

- [ ] **Step 4: Enforce policy before every runner**

In `validate_tool()` normalize the requested target lexically inside the resolved workspace, reject any symlink component, derive a case-folded POSIX relative path, and call `is_sensitive_path` for read/search/write/patch. For writes/patches/memory saves call `contains_secret_material` on the complete new content. Raise a small `SensitiveToolError(code)` so ToolExecutor can produce stable metadata without parsing strings.

`list_files` does not open or stat a sensitive entry after directory enumeration. It may emit only the normalized basename plus `[sensitive]`; size, mode, target, digest, preview, and content are omitted.

Directory search consults only `ToolContext.trusted_executables.get("rg")`; it never calls `shutil.which` or re-reads PATH after bootstrap. With trusted rg it uses fixed `--glob` exclusions for all static sensitive basename/extension/component patterns and applies `is_sensitive_path()` again to every output line path. With no trusted rg it selects the Python fallback and filters candidates before `read_text`.

- [ ] **Step 5: Make BlockStore and snapshots independently safe**

BlockStore rejects secret material at both append/write entrypoints and never follows note symlinks. `snapshot_eligibility()` checks path before any read, reads the complete bounded file bytes once, rejects binary/size/content, and reports only metadata. Workspace snapshot skips sensitive paths before hash. ToolExecutor's Git HEAD fallback runs the same path check, decodes stdout with UTF-8 replacement, and calls `contains_secret_material(decoded_stdout)` before `write_blob`.

- [ ] **Step 6: Redact every ToolExecutionResult before return**

At the single `Pico.execute_tool()` boundary replace result with a new `ToolExecutionResult(content=self.redact_text(result.content), metadata=self.redact_artifact(result.metadata))`. Checkpoint JSON redaction is added in Task 9; this step protects AgentLoop/programmatic callers.

- [ ] **Step 7: Run focused tests and commit**

Run: `uv run pytest tests/test_sensitive_tools.py tests/test_tools.py tests/test_tool_executor.py tests/memory/test_memory_tools.py tests/memory/test_block_store.py tests/test_recovery_policy.py tests/test_workspace_snapshot.py -q`

Expected: all pass; direct runner count and blob count remain zero for blocked cases.

Run: `uv run ruff check pico/tools.py pico/tool_executor.py pico/runtime.py pico/memory pico/recovery_policy.py pico/workspace_snapshot.py tests/test_sensitive_tools.py`

Expected: no diagnostics.

```bash
git add pico/tools.py pico/tool_executor.py pico/runtime.py pico/memory pico/recovery_policy.py pico/workspace_snapshot.py tests/test_sensitive_tools.py tests/test_tools.py tests/test_tool_executor.py tests/memory/test_memory_tools.py tests/memory/test_block_store.py tests/test_recovery_policy.py tests/test_workspace_snapshot.py
git commit -m "feat(security): block sensitive tool and snapshot inputs"
```

### Task 9: Private Redacted Artifacts and Safe CLI Inspection

**Files:**
- Modify: `pico/checkpoint_store.py`
- Modify: `pico/session_store.py`
- Modify: `pico/run_store.py`
- Modify: `pico/runtime.py`
- Modify: `pico/cli_recovery.py`
- Modify: `pico/cli_diagnostics.py`
- Modify: `pico/cli_output.py`
- Modify: `pico/memory/block_store.py`
- Create: `tests/test_artifact_security.py`
- Modify: `tests/test_checkpoint_store_phase1.py`
- Modify: `tests/test_recovery_cli.py`
- Modify: `tests/test_cli_diagnostics.py`
- Modify: `tests/test_safety_invariants.py`

**Interfaces:**
- Produces: `CheckpointStore(workspace_root, redactor=None)` and `set_redactor(redactor)`; JSON records are safe, exact blobs remain exact and are made private. CLI inspection is read-time sanitized.
- Consumes: Task 1 redactor, Task 2 private/no-follow helpers, Task 4 exact-root project env.
- A2 keeps this constructor and must use an identity redactor only in isolated tests that contain no secrets.

- [ ] **Step 1: Add a failing cross-artifact canary test**

```python
def test_secret_canary_is_absent_from_normal_artifacts_and_inspection(tmp_path, monkeypatch, capsys):
    secret = "github_pat_A123456789012345678901234567890"
    monkeypatch.setenv("PICO_TEST_TOKEN", secret)
    agent = build_agent(tmp_path, [], secret_env_names=("PICO_TEST_TOKEN",))
    state = TaskState.create(run_id="run_canary", task_id="task_canary", user_request=secret)
    agent.run_store.start_run(state)
    agent.emit_trace(state, "canary", {"token": secret})
    agent.run_store.write_report(state, {"token": secret})
    tc = agent.tool_change_recorder.start("", state.task_id, "run_shell", "workspace_write", {"command": secret})
    agent.tool_change_recorder.finalize(tc["tool_change_id"], "error", error={"message": secret})
    record = new_checkpoint_record("ckpt_canary", "turn", "s", state.run_id, state.task_id, "", str(tmp_path))
    record["verification_evidence"] = [{"stdout_tail": secret}]
    agent.checkpoint_store.write_checkpoint_record(record)

    for path in (tmp_path / ".pico").rglob("*"):
        if path.is_file() and "sessions/backup" not in path.as_posix() and "/blobs/" not in path.as_posix():
            assert secret.encode() not in path.read_bytes(), path

    main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "show", "ckpt_canary"])
    assert secret not in capsys.readouterr().out
```

- [ ] **Step 2: Add failing mode and unsafe-type tests**

For session/run/checkpoint/raw-result/lock/backup/memory files assert 0600 and owner dirs 0700 on POSIX. Replace a store directory/trace/record with symlink or FIFO and assert mutation raises a stable error without touching the external target. Add a legacy 0644 inspection fixture and assert output is redacted.

- [ ] **Step 3: Run tests and confirm red**

Run: `uv run pytest tests/test_artifact_security.py tests/test_checkpoint_store_phase1.py tests/test_recovery_cli.py tests/test_cli_diagnostics.py -q`

Expected: checkpoint JSON/inspection and some direct files expose the sentinel or unsafe mode.

- [ ] **Step 4: Add store-boundary redaction and private modes**

CheckpointStore mirrors RunStore/SessionStore redactor injection. `_write_json_atomic()` applies the redactor to a deep copy, uses a 0600 temp, `flush/fsync/replace`, and chmods the final file. `write_blob()` never redacts bytes but uses 0700 dirs/0600 files; A2 adds hash/id validation and full durability.

Store constructors harden only their owned subtree with lstat/no-follow. Do not recursively chmod workspace source files.

- [ ] **Step 5: Sanitize all inspection render paths**

`handle_sessions`, `handle_runs`, and `handle_checkpoints` build a redactor from `read_project_env(root, warn=False)` plus process env and apply it immediately before `print_result`. JSON and text modes receive the same safe data. CLI never opens session backup directly.

- [ ] **Step 6: Run artifact/CLI tests and commit**

Run: `uv run pytest tests/test_artifact_security.py tests/test_checkpoint_store_phase1.py tests/test_recovery_cli.py tests/test_cli_diagnostics.py tests/test_safety_invariants.py tests/test_run_store.py tests/test_session_store.py tests/memory/test_block_store.py -q`

Expected: all pass; exact blob fixture remains byte-equal and all JSON/display observations are safe.

Run: `uv run ruff check pico/checkpoint_store.py pico/session_store.py pico/run_store.py pico/runtime.py pico/cli_recovery.py pico/cli_diagnostics.py pico/cli_output.py pico/memory/block_store.py tests/test_artifact_security.py`

Expected: no diagnostics.

```bash
git add pico/checkpoint_store.py pico/session_store.py pico/run_store.py pico/runtime.py pico/cli_recovery.py pico/cli_diagnostics.py pico/cli_output.py pico/memory/block_store.py tests/test_artifact_security.py tests/test_checkpoint_store_phase1.py tests/test_recovery_cli.py tests/test_cli_diagnostics.py tests/test_safety_invariants.py
git commit -m "fix(storage): make local artifacts private and redacted"
```

### Task 10: Pure Fail-Closed Shell Assessment

**Files:**
- Modify: `pico/recovery_policy.py`
- Create: `tests/test_shell_assessment.py`
- Modify: `tests/test_recovery_policy.py`

**Interfaces:**
- Produces: `assess_command(command, workspace_root, executables=None)` with exactly `risk_class`, `decision`, `reason`, `argv`, and `execution_mode`.
- Preserves: `command_risk_class(command, _depth=0)` as a compatibility wrapper returning only the four-class risk value.
- Internal: `_scan_shell_syntax(command)` returns exactly `parse_error`, `operators`, `redirects`, `has_expansion`, `has_assignment`, and `has_control_keyword`; `redirects` contains `(operator, literal_target)` pairs and tests call it directly.
- Consumes: Task 1 sensitive-path policy and Task 2 no-follow path checks. Assessment has no side effects and never invokes a command.

- [ ] **Step 1: Add the failing exact-grammar matrix**

Create a table-driven test whose expected values are pinned as follows:

| command | risk | decision | mode | reason family |
| --- | --- | --- | --- | --- |
| `pwd` | `read_only` | `allow` | `argv` | `proved_read_only` |
| `ls -1 -a README.md` | `read_only` | `allow` | `argv` | `proved_read_only` |
| `stat README.md` | `read_only` | `allow` | `argv` | `proved_read_only` |
| `file --brief README.md` | `read_only` | `allow` | `argv` | `proved_read_only` |
| `wc -l README.md` | `read_only` | `allow` | `argv` | `proved_read_only` |
| `git status --short --branch` | `read_only` | `allow` | `argv` | `proved_read_only` |
| `git rev-parse --show-toplevel` | `read_only` | `allow` | `argv` | `proved_read_only` |
| `git branch --show-current` | `read_only` | `allow` | `argv` | `proved_read_only` |
| `git worktree list` | `read_only` | `allow` | `argv` | `proved_read_only` |
| `git ls-files` | `read_only` | `allow` | `argv` | `proved_read_only` |
| `python -m pytest` | `external_effect` | `ask` | `argv` | `interpreter_requires_approval` |
| `bash -c 'pwd && ls'` | `external_effect` | `ask` | `argv` | `shell_wrapper_requires_approval` |
| `sudo ls` | `external_effect` | `ask` | `argv` | `privileged_command_requires_approval` |
| `./ls` | `external_effect` | `ask` | `argv` | `executable_path_requires_approval` |
| `unknown-binary --flag` | `external_effect` | `ask` | `argv` | `unknown_command_requires_approval` |
| `pwd && ls` | `external_effect` | `ask` | `shell` | `shell_grammar_requires_approval` |
| `cat README.md > output.txt` | `workspace_write` | `ask` | `shell` | `redirect_requires_approval` |
| `cat .env` | `destructive` | `reject` | `argv` | `sensitive_path` |
| `cat README.md > .env` | `destructive` | `reject` | `shell` | `sensitive_path` |

Pin parse errors, empty input, backticks, `$()`, `$NAME`, `${NAME}`, unquoted `*`, `?`, bracket globs, leading `~`, pipelines, `||`, `;`, background `&`, heredocs, subshells, variable assignments, and `if/then/while/for/case` to `ask + shell`. A syntactically simple `sh/bash/zsh -c <script>` remains `ask + argv`: after approval the frozen wrapper executable receives the argv with outer `shell=False`; Pico never wraps it in another shell. Single-quoted metacharacters/expansions are literal argv text; double quotes keep operators literal but still classify `$`, backtick, and command substitution as expansion. A literal sensitive operand always wins with `reject`, including when shell grammar is also present.

- [ ] **Step 2: Add command-specific bypass tests**

Assert that all of these are non-allow: combined `ls -la`; `ls --color`; `wc -L`; `date` and `date -s`; `rg`, `grep`, `find`, `diff`, `cat`, `head`, `tail`; Git `log/show/diff/blame/config/remote/tag`, `-C`, `--git-dir`, `--work-tree`, `--no-pager`, `--ext-diff`, and unknown global options; `find -delete/-exec/-ok/-fprint`; `rg --pre/--pre-glob/--config`; package, build, network, cloud, container, service, mount, ownership, permission, kill, and shutdown commands.

Create a workspace symlink pointing outside and assert every allowed grammar with that operand becomes `reject` with `unsafe_path`. Assert quoted metacharacters stay inside argv, while unquoted metacharacters select `execution_mode="shell"`.

- [ ] **Step 3: Run the focused tests and confirm red**

Run: `uv run pytest tests/test_shell_assessment.py tests/test_recovery_policy.py -q`

Expected: failures show the current fallback allows unknown/parse-error commands and lacks exact per-command grammar.

- [ ] **Step 4: Implement quote-aware classification and exact grammars**

Implement `_scan_shell_syntax()` as a single left-to-right state machine with states `unquoted`, `single_quoted`, `double_quoted`, and `escaped`. It never expands text. In unquoted state it recognizes longest tokens first: `<<`, `>>`, `&&`, `||`, `$(`, then one-character `|;&<>()` and expansion markers. In double quotes it recognizes backtick, `$(`, `$NAME`, and `${NAME}` but treats `|;&<>` as literal. In single quotes every character is literal. A dangling escape or unclosed quote sets `parse_error`; control keywords and leading assignments are checked only after a successful `shlex.split(posix=True, comments=False)`.

Use this implementation skeleton verbatim, adding only type annotations and formatting during implementation:

```python
_TWO_CHAR_SHELL_TOKENS = ("&&", "||", "<<", ">>")
_ONE_CHAR_SHELL_TOKENS = frozenset("|;&<>()")
_REDIRECT_TOKENS = {"<", ">", "<<", ">>"}
_CONTROL_KEYWORDS = {"if", "then", "elif", "else", "fi", "while", "until", "for", "do", "done", "case", "esac"}
_ASSIGNMENT_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _scan_shell_syntax(command):
    raw = str(command or "")
    operators = []
    redirect_operators = []
    has_expansion = False
    quote = ""
    escaped = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote == "single":
            if char == "'":
                quote = ""
            index += 1
            continue
        if quote == "double":
            if char == '"':
                quote = ""
            elif char == "\\":
                escaped = True
            elif char == "`" or char == "$":
                has_expansion = True
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == "'":
            quote = "single"
            index += 1
            continue
        if char == '"':
            quote = "double"
            index += 1
            continue
        pair = raw[index:index + 2]
        if pair in _TWO_CHAR_SHELL_TOKENS:
            operators.append(pair)
            if pair in _REDIRECT_TOKENS:
                redirect_operators.append(pair)
            index += 2
            continue
        if char in _ONE_CHAR_SHELL_TOKENS:
            operators.append(char)
            if char in _REDIRECT_TOKENS:
                redirect_operators.append(char)
            index += 1
            continue
        if char in "`$*?[" or (char == "~" and (index == 0 or raw[index - 1].isspace())):
            has_expansion = True
        index += 1

    parse_error = bool(quote or escaped)
    argv = []
    if not parse_error:
        try:
            argv = shlex.split(raw, comments=False, posix=True)
        except ValueError:
            parse_error = True
    redirects = []
    if not parse_error and redirect_operators:
        lexer = shlex.shlex(raw, posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        grammar_tokens = list(lexer)
        for token_index, token in enumerate(grammar_tokens):
            if token in _REDIRECT_TOKENS:
                target = grammar_tokens[token_index + 1] if token_index + 1 < len(grammar_tokens) else ""
                redirects.append((token, target))
    has_assignment = bool(argv and _ASSIGNMENT_TOKEN_RE.match(argv[0]))
    has_control_keyword = bool(argv and argv[0].casefold() in _CONTROL_KEYWORDS)
    return {
        "parse_error": parse_error,
        "operators": tuple(operators),
        "redirects": tuple(redirects),
        "has_expansion": has_expansion,
        "has_assignment": has_assignment,
        "has_control_keyword": has_control_keyword,
    }
```

Implement path and exact-command proof with these helpers:

```python
_LS_OPTIONS = {"-1", "-a", "-A", "-d", "-F", "-l"}
_FILE_OPTIONS = {"-b", "--brief"}
_WC_OPTIONS = {"-c", "-l", "-w"}
_GIT_STATUS_OPTIONS = {"--short", "--porcelain", "--porcelain=v1", "--branch"}
_AUTO_HEADS = {"pwd", "ls", "stat", "file", "wc", "git"}
_SHELL_WRAPPERS = {"sh", "bash", "zsh"}
_INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "php"}
_PRIVILEGED = {"sudo", "doas", "pkexec"}
_DESTRUCTIVE_HEADS = {"shutdown", "reboot", "mount", "umount", "chown", "chmod", "kill"}


def _assessment(risk_class, decision, reason, argv, execution_mode):
    return {
        "risk_class": risk_class,
        "decision": decision,
        "reason": reason,
        "argv": list(argv),
        "execution_mode": execution_mode,
    }


def _path_operand_reason(workspace_root, raw_path):
    root = Path(workspace_root).resolve(strict=True)
    raw = str(raw_path or "")
    if not raw or "\x00" in raw:
        return "unsafe_path"
    source = Path(raw)
    candidate = Path(os.path.abspath(os.fspath(source if source.is_absolute() else root / source)))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return "outside_path"
    if is_sensitive_path(relative.as_posix()):
        return "sensitive_path"
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return "" if index == len(relative.parts) - 1 else "unsafe_path"
        if stat.S_ISLNK(mode):
            return "unsafe_path"
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(mode):
            return "unsafe_path"
    return ""


def _paths_reason(workspace_root, operands):
    for operand in operands:
        reason = _path_operand_reason(workspace_root, operand)
        if reason:
            return reason
    return ""


def _git_grammar_reason(argv):
    args = tuple(argv[1:])
    if not args:
        return "unknown_git_grammar"
    subcommand, rest = args[0], args[1:]
    if subcommand == "status":
        return "" if len(rest) == len(set(rest)) and set(rest) <= _GIT_STATUS_OPTIONS else "unknown_git_grammar"
    if subcommand == "rev-parse":
        accepted = {
            ("--show-toplevel",),
            ("--is-inside-work-tree",),
            ("--abbrev-ref", "HEAD"),
            ("HEAD",),
        }
        return "" if rest in accepted else "unknown_git_grammar"
    if subcommand == "branch":
        return "" if rest in {("--show-current",), ("--list",)} else "unknown_git_grammar"
    if subcommand == "worktree":
        return "" if rest == ("list",) else "unknown_git_grammar"
    if subcommand == "ls-files":
        return "" if not rest else "unknown_git_grammar"
    return "unknown_git_grammar"


def _automatic_grammar_reason(argv, workspace_root):
    head, args = argv[0], list(argv[1:])
    if head == "pwd":
        return "" if not args else "unknown_option"
    if head == "ls":
        options = [item for item in args if item.startswith("-")]
        paths = [item for item in args if not item.startswith("-")]
        if any(item not in _LS_OPTIONS for item in options):
            return "unknown_option"
        return _paths_reason(workspace_root, paths)
    if head == "stat":
        if not args:
            return "missing_path"
        if any(item.startswith("-") for item in args):
            return "unknown_option"
        return _paths_reason(workspace_root, args)
    if head == "file":
        if args and args[0] in _FILE_OPTIONS:
            args = args[1:]
        if not args or any(item.startswith("-") for item in args):
            return "unknown_option_or_missing_path"
        return _paths_reason(workspace_root, args)
    if head == "wc":
        if args and args[0] in _WC_OPTIONS:
            args = args[1:]
        if not args or any(item.startswith("-") for item in args):
            return "unknown_option_or_missing_path"
        return _paths_reason(workspace_root, args)
    if head == "git":
        return _git_grammar_reason(argv)
    return "unknown_command"


def _grammar_words(command):
    lexer = shlex.shlex(str(command or ""), posix=True, punctuation_chars="|&;<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _literal_sensitive_reason(command, workspace_root):
    try:
        words = _grammar_words(command)
    except ValueError:
        words = re.split(r"[\s|&;<>]+", str(command or ""))
    for word in words:
        stripped = word.strip("\"'(){}")
        if is_sensitive_path(stripped):
            return "sensitive_path"
    return ""
```

Use this complete classifier body. `_depth` is internal and capped so wrapper inspection cannot recurse without bound:

```python
def _assess_command(command, workspace_root, executables, _depth=0):
    raw = str(command or "").strip()
    scan = _scan_shell_syntax(raw)
    literal_reason = _literal_sensitive_reason(raw, workspace_root)
    if literal_reason:
        return _assessment("destructive", "reject", literal_reason, [], "shell" if scan["parse_error"] or scan["operators"] else "argv")
    if scan["parse_error"]:
        return _assessment("external_effect", "ask", "shell_parse_error", [], "shell")
    argv = shlex.split(raw, comments=False, posix=True)
    if scan["redirects"]:
        if scan["has_expansion"]:
            return _assessment("destructive", "ask", "dynamic_redirect", [], "shell")
        redirect_reasons = [
            _path_operand_reason(workspace_root, target)
            for _, target in scan["redirects"]
        ]
        if "sensitive_path" in redirect_reasons:
            return _assessment("destructive", "reject", "sensitive_path", [], "shell")
        if any(reason in {"outside_path", "unsafe_path"} for reason in redirect_reasons):
            return _assessment("destructive", "ask", "unsafe_redirect", [], "shell")
        return _assessment("workspace_write", "ask", "redirect_requires_approval", [], "shell")
    if (
        scan["operators"]
        or scan["has_expansion"]
        or scan["has_assignment"]
        or scan["has_control_keyword"]
    ):
        return _assessment("external_effect", "ask", "shell_grammar_requires_approval", [], "shell")
    if not argv:
        return _assessment("external_effect", "ask", "empty_command", [], "shell")
    head = argv[0]
    if "/" in head or "\\" in head:
        return _assessment("external_effect", "ask", "executable_path_requires_approval", argv, "argv")
    if head in _SHELL_WRAPPERS and len(argv) >= 3 and argv[1] == "-c":
        nested = _assess_command(argv[2], workspace_root, executables, _depth=_depth + 1) if _depth < 2 else None
        if nested is not None and nested["decision"] == "reject":
            return _assessment("destructive", "reject", nested["reason"], argv, "argv")
        return _assessment("external_effect", "ask", "shell_wrapper_requires_approval", argv, "argv")
    if head in _AUTO_HEADS:
        reason = _automatic_grammar_reason(argv, workspace_root)
        if reason:
            decision = "reject" if reason == "sensitive_path" else "ask"
            risk = "destructive" if decision == "reject" else "external_effect"
            return _assessment(risk, decision, reason, argv, "argv")
        if executables is not None and head not in executables:
            return _assessment("read_only", "ask", "trusted_executable_missing", argv, "argv")
        return _assessment("read_only", "allow", "proved_read_only", argv, "argv")
    if head in _INTERPRETERS:
        reason = "interpreter_requires_approval"
    elif head in _PRIVILEGED:
        reason = "privileged_command_requires_approval"
    elif head in _DESTRUCTIVE_HEADS:
        return _assessment("destructive", "ask", "system_command_requires_approval", argv, "argv")
    else:
        reason = "unknown_command_requires_approval"
    return _assessment("external_effect", "ask", reason, argv, "argv")


def assess_command(command, workspace_root, executables=None):
    return _assess_command(command, workspace_root, executables, _depth=0)
```

Do not inspect the executable map to upgrade an unknown command. `executables` only lets assessment produce a stable `trusted_executable_missing` non-allow reason for an otherwise automatically allowed command.

- [ ] **Step 5: Keep the compatibility wrapper honest**

`command_risk_class()` calls `_assess_command(command, Path.cwd(), None, _depth=_depth)` and returns `assessment["risk_class"]`; `_depth` remains accepted for wrapper recursion compatibility. Update legacy tests so they verify the same four values without depending on old allow/deny heuristics.

- [ ] **Step 6: Run focused tests and commit**

Run: `uv run pytest tests/test_shell_assessment.py tests/test_recovery_policy.py -q`

Expected: all matrix and bypass cases pass; no test invokes a subprocess.

Run: `uv run ruff check pico/recovery_policy.py tests/test_shell_assessment.py tests/test_recovery_policy.py`

Expected: no diagnostics.

```bash
git add pico/recovery_policy.py tests/test_shell_assessment.py tests/test_recovery_policy.py
git commit -m "feat(shell): assess commands with exact fail-closed grammar"
```

### Task 11: Single-Gate Shell Approval and Execution

**Files:**
- Modify: `pico/tool_context.py`
- Modify: `pico/tools.py`
- Modify: `pico/tool_executor.py`
- Modify: `pico/runtime.py`
- Modify: `pico/safe_subprocess.py`
- Create: `tests/test_shell_execution_security.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_tool_executor.py`
- Modify: `tests/test_safety_invariants.py`
- Modify: `tests/test_agent_loop.py`

**Interfaces:**
- `ToolContext` gains immutable `trusted_executables: Mapping[str, str]`.
- `run_shell()` returns structured `stdout`, `stderr`, and `exit_code` to ToolExecutor; ToolExecutor creates user-facing text after redaction.
- Every shell result contains complete `command_risk_class` and `command_approval` metadata, including early rejection.
- `Pico.execute_tool()` and AgentLoop's registered ToolExecutor are the only Pico-level execution paths.

- [ ] **Step 1: Add the four-mode runner/prompt matrix**

For each row, inject `runner` and `approve` spies and assert exact counts and metadata:

| mode | assessment | prompt count | runner count | outcome/error |
| --- | --- | ---: | ---: | --- |
| `read_only=True` | any shell | 0 | 0 | `blocked/read_only_block` |
| `approval=never` | any shell | 0 | 0 | `denied/approval_denied` |
| `approval=auto`, executable frozen | allow argv | 0 | 1 | `allowed` |
| `approval=auto`, executable missing | allow argv | 0 | 0 | `blocked/trusted_executable_missing` |
| `approval=auto` | ask/reject | 0 | 0 | `blocked/command_approval_required` or hard reject |
| `approval=ask`, yes, executable frozen | allow/ask argv | 1 | 1 | `approved` |
| `approval=ask`, yes, executable missing | allow/ask argv | 1 | 0 | `blocked/trusted_executable_missing` |
| `approval=ask`, no/EOF | allow/ask | 1 | 0 | `denied/approval_denied` |
| `approval=ask`, yes, frozen `sh` present | complex shell | 1 | 1 | `approved`, `execution_mode=shell` |
| `approval=ask`, yes, frozen `sh` missing | complex shell | 1 | 0 | `blocked/trusted_executable_missing` |
| `approval=ask`, yes | sensitive literal | 0 | 0 | `blocked/sensitive_path_block` |

Each result must contain:

```python
{
    "command_risk_class": "read_only",
    "command_approval": {
        "decision": "allow",
        "reason": "proved_read_only",
        "mode": "auto",
        "outcome": "allowed",
        "runner_executed": True,
        "execution_mode": "argv",
    },
}
```

Tests substitute the row-specific values and also assert a structured integer `exit_code` exists only after runner execution.

- [ ] **Step 2: Add execution-shape and prompt-redaction tests**

Monkeypatch `subprocess.run` and assert automatic `pwd`, approved `python -m pytest`, approved `bash -c 'pwd && ls'`, and other syntactically simple commands receive `[trusted_absolute_executable, *argv[1:]]`, `shell=False`, the safe execution environment, and the workspace cwd. Assert only an approved outer command containing real unquoted shell grammar receives the original string, `shell=True`, and `executable=trusted_executables["sh"]`.

Create a repository-local executable fsmonitor marker, configure `core.fsmonitor` to that marker with setup Git, and execute `git status --short` through the production `run_shell` tool in `approval=auto`. Assert the result is successful, `runner_executed=True`, the marker does not exist, and captured argv contains `--no-pager`, `-c`, and `core.fsmonitor=false`. This test must exercise ToolExecutor dispatch, not call `run_hardened_git()` directly.

Put a concrete token in a rejected ToolAction and in an approval payload. Assert the first is rejected before prompt/runner, and the second prompt contains `<redacted>` but not the token. Assert runner stdout/stderr and the returned metadata are redacted before they leave `Pico.execute_tool()`.

- [ ] **Step 3: Add raw-proxy and executable-spoof invariants**

Assert `Pico` has no callable `tool_list_files`, `tool_read_file`, `tool_search`, `tool_run_shell`, `tool_write_file`, `tool_patch_file`, or `tool_delegate`. Put fake executable files in the workspace and set runtime PATH to the workspace after construction; assert the frozen absolute map is still used. For approved `python -m pytest` with no frozen `python`, assert `tool_status="rejected"`, `tool_error_code="trusted_executable_missing"`, approval count one, runner count zero, and no bare-name fallback. For an approved complex command with no frozen `sh`, assert the same missing-executable outcome and runner count zero.

- [ ] **Step 4: Run tests and confirm red**

Run: `uv run pytest tests/test_shell_execution_security.py tests/test_tools.py tests/test_tool_executor.py tests/test_safety_invariants.py -q`

Expected: current raw proxies remain callable, approval behavior is duplicated, and shell execution does not preserve the assessment/execution distinction.

- [ ] **Step 5: Route shell execution through one ToolExecutor gate**

Build the trusted executable map before project env loading and store an immutable copy on ToolContext. ToolExecutor performs decoded-action secret rejection, assessment, mode policy, at-most-once approval, runner dispatch, outcome completion, redaction, and audit metadata in that order.

For `execution_mode="argv"`, require the bare name in the frozen map. If the name is `git`, dispatch every automatic or approved Git argv through `run_hardened_git(trusted_git, argv[1:], cwd=workspace_root, text=True)` so repo/user configuration cannot re-enable fsmonitor, pager, external diff, credential helper, or extension protocol. All other names replace argv[0] with the frozen absolute executable and call `subprocess.run` with `shell=False` and the safe environment. For `execution_mode="shell"`, require `approval=ask`, an affirmative response, and frozen `sh`, then call `subprocess.run(command, shell=True, executable=trusted_executables["sh"])` with the safe environment. A missing executable finalizes blocked metadata and never calls the runner. Never use `shell=True` for a syntactically simple approved command. Remove the public raw proxy methods from `Pico`; keep module-level runners private to the registry/executor path.

- [ ] **Step 6: Persist final shell outcomes**

Tool Change input summary stores the redacted command and initial assessment. Finalization stores the exact final approval outcome, `runner_executed`, `execution_mode`, and structured exit code. A rejected or denied command has no verification evidence and records no workspace mutation.

- [ ] **Step 7: Run focused tests and commit**

Run: `uv run pytest tests/test_shell_execution_security.py tests/test_tools.py tests/test_tool_executor.py tests/test_safety_invariants.py tests/test_agent_loop.py -q`

Expected: all pass; ask prompts are at most one per command and every non-executed row has runner count zero.

Run: `uv run ruff check pico/tool_context.py pico/tools.py pico/tool_executor.py pico/runtime.py pico/safe_subprocess.py tests/test_shell_execution_security.py`

Expected: no diagnostics.

```bash
git add pico/tool_context.py pico/tools.py pico/tool_executor.py pico/runtime.py pico/safe_subprocess.py tests/test_shell_execution_security.py tests/test_tools.py tests/test_tool_executor.py tests/test_safety_invariants.py tests/test_agent_loop.py
git commit -m "feat(shell): enforce one approval and execution gate"
```

### Task 12: Exact Verification Evidence and A1 Integration Gate

**Files:**
- Modify: `pico/verification.py`
- Modify: `pico/tool_executor.py`
- Modify: `pico/cli_commands.py`
- Modify: `pico/cli_diagnostics.py`
- Modify: `README.md`
- Create: `tests/test_verification_security.py`
- Create: `tests/test_a1_security_integration.py`
- Modify: `tests/test_verification_evidence.py`
- Modify: `tests/test_cli_commands.py`
- Modify: `tests/test_cli_diagnostics.py`

**Interfaces:**
- Produces: `is_verification_argv(argv) -> bool`; evidence creation also requires `runner_executed=True`, `execution_mode="argv"`, and an integer exit code.
- Produces: `collect_doctor(cwd, args=None, offline=False)["security"]` with safe status-only metadata for project env, private storage, and trusted executables.
- Leaves recovery-review doctor counts to A2/A3.

- [ ] **Step 1: Add failing verification evidence tests**

Pin accepted argv shapes to this exact prefix table; each prefix may be followed by ordinary option/path operands that contain no shell token, NUL, newline, executable path, or sensitive path:

| accepted prefix |
| --- |
| `pytest` |
| `python -m pytest` |
| `python3 -m pytest` |
| `ruff check` |
| `python -m ruff check` |
| `python3 -m ruff check` |
| `uv run pytest` |
| `uv run ruff check` |
| `uv run python -m pytest` |
| `uv run python3 -m pytest` |
| `mypy` |
| `pyright` |
| `npm test` |
| `pnpm test` |
| `yarn test` |
| `cargo test` |
| `go test` |

Reject a slash-containing executable, `python -c`, any other `uv` subcommand, arbitrary `tool run pytest`, and every token equal to or containing a shell operator/redirect. For every accepted shape, assert evidence is still absent when `runner_executed=False`, `execution_mode="shell"`, or `exit_code` is missing.

Explicitly assert `pytest || true`, `pytest | tee out`, `pytest > out`, `sh -c pytest`, an approval denial, a read-only block, and an auto rejection produce zero verification records. Assert status derives from `exit_code == 0`, not stdout text, and captured command/stdout/stderr are bounded and redacted.

- [ ] **Step 2: Add an offline A1 canary integration test**

Construct `secret = "ghp_" + "A" * 32` at runtime, inject it through user request, session candidate, working memory, raw tool result, Provider error, Final/Retry action, approval payload, Tool Change input/error, checkpoint verification, trace, report, and legacy CLI fixture. Assert:

1. Provider spy system/messages contain no original value;
2. blocked ToolAction prompt/provider/runner counts are zero;
3. in-memory session and normal `.pico` JSON/text artifacts contain no original value;
4. CLI `sessions`, `runs`, `checkpoints`, and `doctor` output contains no original value;
5. the secret canary never reaches a recovery blob, a separate non-secret blob remains byte-exact, and only the private migration backup may retain the canary, with no CLI exposure;
6. all owned active files are 0600 and directories are 0700 on POSIX.

- [ ] **Step 3: Run tests and confirm red**

Run: `uv run pytest tests/test_verification_security.py tests/test_a1_security_integration.py tests/test_verification_evidence.py tests/test_cli_commands.py tests/test_cli_diagnostics.py -q`

Expected: verification currently accepts composite/string markers or lacks execution facts, and at least one canary observation fails.

- [ ] **Step 4: Implement exact evidence admission and security doctor data**

Replace string marker matching with explicit argv-shape predicates. ToolExecutor calls evidence creation only after runner completion and supplies the structured execution facts. Redact and bound all evidence fields before CheckpointStore sees them.

Add stable doctor data containing only status, permission mode, and missing executable names; never include env values, executable search paths, secret lengths, hashes, or raw exceptions. CLI help names `config set-secret`, shell approval, and checkpoint inspection without promising a sandbox.

- [ ] **Step 5: Document the A1 trust boundary**

Update README with these exact operational facts:

- `config set-secret NAME [--stdin]` is the sole CLI secret-write path and `init` accepts no API key argument;
- direct tools and automatic shell reject sensitive paths/content;
- automatic shell is an exact `shell=False` argv allowlist;
- approved complex shell is a human-authorized escape hatch, not an OS sandbox;
- owned secret/artifact files use 0600 and directories use 0700 on POSIX;
- Pico does not provide encryption, Vault integration, a container, or an OS sandbox.

- [ ] **Step 6: Run A1 focused and full local gates**

Run: `uv run pytest tests/test_security.py tests/test_a1_security_integration.py tests/test_shell_assessment.py tests/test_shell_execution_security.py tests/test_verification_security.py tests/test_safety_invariants.py -q`

Expected: all pass; canary scan and shell runner-count assertions are green.

Run: `uv run ruff check pico tests/test_security.py tests/test_a1_security_integration.py tests/test_shell_assessment.py tests/test_shell_execution_security.py tests/test_verification_security.py`

Expected: no diagnostics.

Run: `./scripts/check.sh`

Expected: Ruff exits 0 and the complete pytest suite has no failed or errored tests.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 7: Commit the A1 integration gate**

```bash
git add pico/verification.py pico/tool_executor.py pico/cli_commands.py pico/cli_diagnostics.py README.md tests/test_verification_security.py tests/test_a1_security_integration.py tests/test_verification_evidence.py tests/test_cli_commands.py tests/test_cli_diagnostics.py
git commit -m "test(security): close A1 trust boundaries"
```
