import os
import shlex
import subprocess

import pytest

from pico import recovery_policy


def assess_command(*args, **kwargs):
    return recovery_policy.assess_command(*args, **kwargs)


def _scan_shell_syntax(command):
    return recovery_policy._scan_shell_syntax(command)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "pwd",
            {
                "risk_class": "read_only",
                "decision": "allow",
                "reason": "proved_read_only",
                "argv": ["pwd"],
                "execution_mode": "argv",
            },
        ),
        (
            "ls -1 -a README.md",
            {
                "risk_class": "read_only",
                "decision": "allow",
                "reason": "proved_read_only",
                "argv": ["ls", "-1", "-a", "README.md"],
                "execution_mode": "argv",
            },
        ),
        (
            "stat README.md",
            {
                "risk_class": "read_only",
                "decision": "allow",
                "reason": "proved_read_only",
                "argv": ["stat", "README.md"],
                "execution_mode": "argv",
            },
        ),
        (
            "file --brief README.md",
            {
                "risk_class": "read_only",
                "decision": "allow",
                "reason": "proved_read_only",
                "argv": ["file", "--brief", "README.md"],
                "execution_mode": "argv",
            },
        ),
        (
            "wc -l README.md",
            {
                "risk_class": "read_only",
                "decision": "allow",
                "reason": "proved_read_only",
                "argv": ["wc", "-l", "README.md"],
                "execution_mode": "argv",
            },
        ),
        (
            "git status --short --branch",
            {
                "risk_class": "read_only",
                "decision": "allow",
                "reason": "proved_read_only",
                "argv": ["git", "status", "--short", "--branch"],
                "execution_mode": "argv",
            },
        ),
        (
            "git rev-parse --show-toplevel",
            {
                "risk_class": "read_only",
                "decision": "allow",
                "reason": "proved_read_only",
                "argv": ["git", "rev-parse", "--show-toplevel"],
                "execution_mode": "argv",
            },
        ),
        (
            "git branch --show-current",
            {
                "risk_class": "read_only",
                "decision": "allow",
                "reason": "proved_read_only",
                "argv": ["git", "branch", "--show-current"],
                "execution_mode": "argv",
            },
        ),
        (
            "git worktree list",
            {
                "risk_class": "read_only",
                "decision": "allow",
                "reason": "proved_read_only",
                "argv": ["git", "worktree", "list"],
                "execution_mode": "argv",
            },
        ),
        (
            "git ls-files",
            {
                "risk_class": "read_only",
                "decision": "allow",
                "reason": "proved_read_only",
                "argv": ["git", "ls-files"],
                "execution_mode": "argv",
            },
        ),
        (
            "python -m pytest",
            {
                "risk_class": "external_effect",
                "decision": "ask",
                "reason": "interpreter_requires_approval",
                "argv": ["python", "-m", "pytest"],
                "execution_mode": "argv",
            },
        ),
        (
            "bash -c 'pwd && ls'",
            {
                "risk_class": "external_effect",
                "decision": "ask",
                "reason": "shell_wrapper_requires_approval",
                "argv": ["bash", "-c", "pwd && ls"],
                "execution_mode": "argv",
            },
        ),
        (
            "sudo ls",
            {
                "risk_class": "external_effect",
                "decision": "ask",
                "reason": "privileged_command_requires_approval",
                "argv": ["sudo", "ls"],
                "execution_mode": "argv",
            },
        ),
        (
            "./ls",
            {
                "risk_class": "external_effect",
                "decision": "ask",
                "reason": "executable_path_requires_approval",
                "argv": ["./ls"],
                "execution_mode": "argv",
            },
        ),
        (
            "unknown-binary --flag",
            {
                "risk_class": "external_effect",
                "decision": "ask",
                "reason": "unknown_command_requires_approval",
                "argv": ["unknown-binary", "--flag"],
                "execution_mode": "argv",
            },
        ),
        (
            "pwd && ls",
            {
                "risk_class": "external_effect",
                "decision": "ask",
                "reason": "shell_grammar_requires_approval",
                "argv": [],
                "execution_mode": "shell",
            },
        ),
        (
            "cat README.md > output.txt",
            {
                "risk_class": "workspace_write",
                "decision": "ask",
                "reason": "redirect_requires_approval",
                "argv": [],
                "execution_mode": "shell",
            },
        ),
        (
            "cat .env",
            {
                "risk_class": "destructive",
                "decision": "reject",
                "reason": "sensitive_path",
                "argv": [],
                "execution_mode": "argv",
            },
        ),
        (
            "cat README.md > .env",
            {
                "risk_class": "destructive",
                "decision": "reject",
                "reason": "sensitive_path",
                "argv": [],
                "execution_mode": "shell",
            },
        ),
    ],
)
def test_exact_command_grammar_matrix(workspace, command, expected):
    assert assess_command(command, workspace) == expected


@pytest.mark.parametrize(
    "command",
    [
        "ls -A README.md",
        "ls -d README.md",
        "ls -F README.md",
        "ls -l README.md",
        "file -b README.md",
        "wc -c README.md",
        "wc -w README.md",
        "git status",
        "git status --short",
        "git status --porcelain",
        "git status --porcelain=v1",
        "git status --branch",
        "git status --short --porcelain --porcelain=v1 --branch",
        "git rev-parse --show-toplevel",
        "git rev-parse --is-inside-work-tree",
        "git rev-parse --abbrev-ref HEAD",
        "git rev-parse HEAD",
        "git branch --show-current",
        "git branch --list",
    ],
)
def test_declared_read_only_grammar_variants_are_allowed(workspace, command):
    result = assess_command(command, workspace)

    assert result["risk_class"] == "read_only"
    assert result["decision"] == "allow"
    assert result["reason"] == "proved_read_only"
    assert result["execution_mode"] == "argv"


@pytest.mark.parametrize(
    "command",
    [
        "git status --short --short",
        "git status --verbose",
        "file -b -b README.md",
        "file -b --brief README.md",
        "file --mime README.md",
        "wc -l -l README.md",
        "wc -l -w README.md",
        "wc -L README.md",
    ],
)
def test_duplicate_or_unknown_read_only_options_are_not_allowed(workspace, command):
    assert assess_command(command, workspace)["decision"] != "allow"


@pytest.mark.parametrize(
    "command",
    [
        'echo "unterminated',
        "echo dangling\\",
        "",
        "echo `pwd`",
        "echo $(pwd)",
        "echo $NAME",
        "echo ${NAME}",
        "ls *",
        "ls ?",
        "ls README[0]",
        "ls ~",
        "pwd | ls",
        "pwd || ls",
        "pwd; ls",
        "pwd &",
        "cat << EOF",
        "(pwd)",
        "NAME=value pwd",
        "if true",
        "then true",
        "while true",
        "for item",
        "case item",
    ],
)
def test_shell_grammar_requires_approval(workspace, command):
    result = assess_command(command, workspace)

    assert result["decision"] == "ask"
    assert result["execution_mode"] == "shell"


@pytest.mark.parametrize(
    "command",
    [
        "ls README.md\npwd",
        "wc -l README.md\r\npwd",
        "git\nstatus --short",
        "{ pwd\n}",
        "ls\n{ pwd\n}",
    ],
)
def test_unquoted_line_separators_require_shell_approval(workspace, command):
    result = assess_command(command, workspace)

    assert result["decision"] == "ask"
    assert result["execution_mode"] == "shell"


@pytest.mark.parametrize("quote", ["'", '"'])
def test_quoted_newline_remains_literal_argv_text(workspace, quote):
    result = assess_command(f"ls {quote}literal\nname{quote}", workspace)

    assert result["decision"] == "allow"
    assert result["execution_mode"] == "argv"
    assert result["argv"] == ["ls", "literal\nname"]


@pytest.mark.parametrize(
    "command",
    [
        "ls READ\\\nME.md",
        "ls READ\\\r\nME.md",
    ],
)
def test_line_continuations_are_shell_grammar(workspace, command):
    result = assess_command(command, workspace)

    assert result["decision"] == "ask"
    assert result["execution_mode"] == "shell"


@pytest.mark.parametrize(
    "command",
    [
        "wc .e\\\nnv",
        "wc .e\\\r\nnv",
        'wc ".e\\\nnv"',
        "cat README.md > .e\\\nnv",
    ],
)
def test_line_continuation_cannot_hide_sensitive_path(workspace, command):
    result = assess_command(command, workspace)

    assert result["risk_class"] == "destructive"
    assert result["decision"] == "reject"
    assert result["reason"] == "sensitive_path"
    assert result["execution_mode"] == "shell"


def test_single_quoted_line_continuation_is_literal(workspace):
    command = "ls '.e\\\nnv'"

    result = assess_command(command, workspace)

    assert result["decision"] == "allow"
    assert result["execution_mode"] == "argv"
    assert result["argv"] == ["ls", ".e\\\nnv"]


@pytest.mark.parametrize(
    "command",
    ["wc {,.}env", "wc secret.{pem,txt}"],
)
def test_unquoted_brace_expansion_requires_shell_approval(workspace, command):
    result = assess_command(command, workspace)

    assert result["decision"] == "ask"
    assert result["execution_mode"] == "shell"


@pytest.mark.parametrize(
    "command",
    ["ls '{safe,other}'", r"ls \{safe,other\}"],
)
def test_quoted_or_escaped_braces_remain_literal(workspace, command):
    result = assess_command(command, workspace)

    assert result["decision"] == "allow"
    assert result["execution_mode"] == "argv"
    assert result["argv"] == ["ls", "{safe,other}"]


def test_shell_scanner_reports_exact_shape_and_literal_redirect_targets():
    assert _scan_shell_syntax("cat input < source > output >> log << EOF") == {
        "parse_error": False,
        "operators": ("<", ">", ">>", "<<"),
        "redirects": (
            ("<", "source"),
            (">", "output"),
            (">>", "log"),
            ("<<", "EOF"),
        ),
        "has_expansion": False,
        "has_assignment": False,
        "has_control_keyword": False,
    }


@pytest.mark.parametrize(
    ("command", "operator"),
    [
        ("cat input 2>&1", ">&"),
        ("cat input <> output", "<>"),
        ("cat input >>> output", ">>>"),
    ],
)
def test_unsupported_redirect_runs_are_explicit_and_fail_closed(
    workspace,
    command,
    operator,
):
    scan = _scan_shell_syntax(command)
    result = assess_command(command, workspace)

    assert scan["redirects"] == ((operator, ""),)
    assert result["risk_class"] == "destructive"
    assert result["decision"] == "ask"
    assert result["reason"] == "unsafe_redirect"
    assert result["execution_mode"] == "shell"


def test_command_substitution_is_scanned_as_longest_token():
    scan = _scan_shell_syntax("echo $(pwd)")

    assert scan["operators"] == ("$(", ")")
    assert scan["has_expansion"] is True


def test_quotes_and_escapes_keep_literal_argv_text(workspace):
    literal_commands = {
        "ls 'literal|$HOME*?[~'": ["ls", "literal|$HOME*?[~"],
        'ls "literal|&;<>[]"': ["ls", "literal|&;<>[]"],
        r"ls a\|b": ["ls", "a|b"],
        r"ls \$HOME": ["ls", "$HOME"],
    }

    for command, argv in literal_commands.items():
        result = assess_command(command, workspace)
        assert result["decision"] == "allow"
        assert result["execution_mode"] == "argv"
        assert result["argv"] == argv

    expanded = assess_command('ls "$HOME"', workspace)
    assert expanded["decision"] == "ask"
    assert expanded["execution_mode"] == "shell"


@pytest.mark.parametrize(
    ("command", "literal"),
    [
        (r"ls \(.env\)", "(.env)"),
        ("ls '{.env}'", "{.env}"),
        (r"ls \{.env\}", "{.env}"),
        (r"ls .env\)", ".env)"),
    ],
)
def test_literal_metacharacters_do_not_create_sensitive_false_positive(
    workspace,
    command,
    literal,
):
    result = assess_command(command, workspace)

    assert result["decision"] == "allow"
    assert result["argv"] == ["ls", literal]


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "ls --color",
        "wc -L README.md",
        "date",
        "date -s tomorrow",
        "rg pattern",
        "grep pattern README.md",
        "find .",
        "diff README.md README.md",
        "cat README.md",
        "head README.md",
        "tail README.md",
        "git log",
        "git show HEAD",
        "git diff",
        "git blame README.md",
        "git config --list",
        "git remote -v",
        "git tag",
        "git -C . status",
        "git --git-dir=.git status",
        "git --work-tree=. status",
        "git --no-pager status",
        "git --ext-diff status",
        "git --unknown status",
        "find . -delete",
        "find . -exec echo {} ;",
        "find . -ok echo {} ;",
        "find . -fprint output",
        "rg --pre cat pattern",
        "rg --pre-glob '*.py' pattern",
        "rg --config config pattern",
        "npm install",
        "make build",
        "curl https://example.com",
        "aws sts get-caller-identity",
        "docker ps",
        "systemctl status service",
        "mount",
        "chown user README.md",
        "chmod 600 README.md",
        "kill 1",
        "shutdown now",
    ],
)
def test_command_specific_bypasses_are_never_automatic(workspace, command):
    assert assess_command(command, workspace)["decision"] != "allow"


@pytest.mark.parametrize(
    "command",
    ["ls escape", "stat escape", "file escape", "wc escape"],
)
def test_allowed_path_grammars_do_not_follow_symlinks(tmp_path, command):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    result = assess_command(command, workspace)

    assert result["risk_class"] == "destructive"
    assert result["decision"] == "reject"
    assert result["reason"] == "unsafe_path"


@pytest.mark.parametrize(
    "command",
    ["ls ../outside", "stat ../outside", "file ../outside", "wc ../outside"],
)
def test_allowed_path_grammars_reject_outside_operands(tmp_path, command):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside").write_text("outside\n", encoding="utf-8")

    result = assess_command(command, workspace)

    assert result["risk_class"] == "destructive"
    assert result["decision"] == "reject"
    assert result["reason"] == "outside_path"


@pytest.mark.parametrize(
    "command",
    [
        "ls link/../secret.txt",
        "stat link/../secret.txt",
        "file link/../secret.txt",
        "wc link/../secret.txt",
    ],
)
def test_path_grammar_rejects_internal_parent_components_before_collapse(
    tmp_path,
    command,
):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
    workspace.mkdir()
    (workspace / "link").symlink_to(outside / "inner", target_is_directory=True)

    result = assess_command(command, workspace)

    assert result["risk_class"] == "destructive"
    assert result["decision"] == "reject"
    assert result["reason"] == "unsafe_path"


@pytest.mark.parametrize(
    ("name", "command", "kind"),
    [
        (".env.example", "ls .env.example", "directory"),
        (".env.sample", "wc .env.sample", "fifo"),
        (".env.template", "ls .env.template", "missing"),
    ],
)
def test_env_template_exception_requires_regular_leaf(
    workspace,
    name,
    command,
    kind,
):
    target = workspace / name
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)

    result = assess_command(command, workspace)

    assert result["risk_class"] == "destructive"
    assert result["decision"] == "reject"
    assert result["reason"] == "sensitive_path"


def test_regular_env_template_leaf_remains_read_only(workspace):
    (workspace / ".env.template").write_text(
        "PUBLIC_SETTING=demo\n",
        encoding="utf-8",
    )

    result = assess_command("wc .env.template", workspace)

    assert result["decision"] == "allow"
    assert result["reason"] == "proved_read_only"


def test_path_probe_oserror_fails_closed(workspace, monkeypatch):
    blocked = workspace / "blocked"
    blocked.write_text("blocked\n", encoding="utf-8")
    real_lstat = recovery_policy.Path.lstat

    def guarded_lstat(self, *args, **kwargs):
        if self == blocked:
            raise PermissionError("blocked")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(recovery_policy.Path, "lstat", guarded_lstat)

    result = assess_command("ls blocked", workspace)

    assert result["risk_class"] == "destructive"
    assert result["decision"] == "reject"
    assert result["reason"] == "unsafe_path"


@pytest.mark.parametrize(
    "command",
    ["ls .env", "stat .env", "file .env", "wc .env", "pwd && cat .env"],
)
def test_sensitive_operand_always_rejects(workspace, command):
    result = assess_command(command, workspace)

    assert result["risk_class"] == "destructive"
    assert result["decision"] == "reject"
    assert result["reason"] == "sensitive_path"


@pytest.mark.parametrize(
    "command",
    ["cat .env $HOME", "NAME=x cat .env", "if cat .env"],
)
def test_sensitive_precedence_preserves_shell_execution_mode(workspace, command):
    result = assess_command(command, workspace)

    assert result["decision"] == "reject"
    assert result["reason"] == "sensitive_path"
    assert result["execution_mode"] == "shell"


def test_shell_wrapper_propagates_nested_sensitive_rejection(workspace):
    result = assess_command("bash -c 'cat .env'", workspace)

    assert result == {
        "risk_class": "destructive",
        "decision": "reject",
        "reason": "sensitive_path",
        "argv": [],
        "execution_mode": "argv",
    }


@pytest.mark.parametrize(
    "command",
    [
        "bash -lc 'cat .env'",
        "bash --noprofile -c 'cat .env'",
        "bash --rcfile /dev/null -c 'cat .env'",
        "bash --init-file /dev/null -c 'cat .env'",
        "bash -O extglob -c 'cat .env'",
        "zsh -O -c 'cat .env'",
        "zsh +O -c 'cat .env'",
        "env bash -c 'cat .env'",
        "FOO=1 bash -c 'cat .env'",
        "pwd && bash -c 'cat .env'",
    ],
)
def test_shell_like_wrappers_cannot_hide_sensitive_literal(workspace, command):
    result = assess_command(command, workspace)

    assert result["decision"] == "reject"
    assert result["reason"] == "sensitive_path"


@pytest.mark.parametrize(
    "command",
    ["bash -lc 'pwd'", "bash -c -- 'cat .env'"],
)
def test_shell_wrapper_option_grammar_stays_on_wrapper_approval(workspace, command):
    result = assess_command(command, workspace)

    assert result["risk_class"] == "external_effect"
    assert result["decision"] == "ask"
    assert result["reason"] == "shell_wrapper_requires_approval"
    assert result["execution_mode"] == "argv"


def test_sensitive_literal_scan_is_not_limited_by_wrapper_assessment_depth(workspace):
    command = "cat .env"
    for _ in range(3):
        command = f"bash -c {shlex.quote(command)}"

    result = assess_command(command, workspace)

    assert result["decision"] == "reject"
    assert result["reason"] == "sensitive_path"


def test_trusted_executable_map_can_only_downgrade_automatic_commands(workspace):
    missing = assess_command("pwd", workspace, executables={"ls": "/bin/ls"})
    unknown = assess_command(
        "unknown-binary",
        workspace,
        executables={"unknown-binary": "/trusted/unknown-binary"},
    )

    assert missing["decision"] == "ask"
    assert missing["reason"] == "trusted_executable_missing"
    assert unknown["decision"] == "ask"
    assert unknown["reason"] == "unknown_command_requires_approval"


def test_assessment_never_invokes_a_subprocess(workspace, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("assessment invoked subprocess")
        ),
    )

    assert assess_command("pwd", workspace)["decision"] == "allow"
