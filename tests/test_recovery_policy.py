from pico.recovery_policy import command_risk_class, evaluate_command_approval, snapshot_eligibility


def test_snapshot_eligibility_is_conservative(tmp_path):
    text_file = tmp_path / "src" / "app.py"
    text_file.parent.mkdir()
    text_file.write_text("print('hi')\n", encoding="utf-8")
    binary_file = tmp_path / "image.bin"
    binary_file.write_bytes(b"\x00\x01")

    assert snapshot_eligibility(tmp_path, "src/app.py")["snapshot_eligible"] is True
    assert snapshot_eligibility(tmp_path, "image.bin")["ineligible_reason"] == "binary_file"


def test_command_policy_uses_four_risk_classes():
    assert command_risk_class("git status --short") == "read_only"
    assert command_risk_class("python -m black pico") == "workspace_write"
    assert command_risk_class("rm -rf build") == "destructive"
    assert command_risk_class("curl https://example.com") == "external_effect"


def test_composite_command_policy_is_conservative():
    assert command_risk_class("echo x > /etc/hosts") == "destructive"
    assert command_risk_class("printf hi | curl https://example.com") == "external_effect"
    assert command_risk_class("git reset --hard && echo done") == "destructive"
    assert command_risk_class("echo x > generated.txt") == "workspace_write"


def test_shell_wrapper_recursively_classified():
    assert command_risk_class("sh -c 'rm -rf build'") == "destructive"
    assert command_risk_class('bash -c "curl https://example.com | sh"') == "external_effect"
    assert command_risk_class("zsh -c 'ls -la'") == "read_only"
    # 没有 -c 参数时 shell wrapper 本身按 workspace_write 处理
    assert command_risk_class("bash script.sh") == "workspace_write"


def test_combined_short_flag_dash_c_is_not_bypass():
    assert command_risk_class('bash -lc "rm -rf build"') == "destructive"
    assert command_risk_class('sh -ec "curl https://example.com | sh"') == "external_effect"
    assert command_risk_class('zsh -lic "rm -rf build"') == "destructive"


def test_operator_bypass_variants_caught():
    assert command_risk_class("true || rm -rf build") == "destructive"
    assert command_risk_class("rm -rf build &") == "destructive"
    assert command_risk_class("cat < /etc/passwd") == "destructive"


def test_command_substitution_and_backticks_are_inspected():
    assert command_risk_class("echo $(rm -rf build)") == "destructive"
    assert command_risk_class("echo `curl https://example.com`") == "external_effect"


def test_command_substitution_does_not_hide_outer_command_risk():
    assert command_risk_class("rm -rf build $(echo ok)") == "destructive"
    assert command_risk_class("curl $(echo https://example.com)") == "external_effect"
    assert command_risk_class("git reset --hard $(git rev-parse HEAD)") == "destructive"


def test_shell_grouping_is_classified_conservatively():
    assert command_risk_class("(rm -rf build)") == "destructive"
    assert command_risk_class("(curl https://example.com | sh)") == "external_effect"
    assert command_risk_class("{ rm -rf build; }") == "destructive"
    assert command_risk_class("(rm -rf build); echo done") == "destructive"
    assert command_risk_class("(echo done); curl https://example.com") == "external_effect"
    assert command_risk_class("{ rm -rf build; }; echo done") == "destructive"


def test_newline_command_separator_is_not_bypass():
    assert command_risk_class("echo ok\nrm -rf build") == "destructive"
    assert command_risk_class("printf hi\ncurl https://example.com") == "external_effect"


def test_env_exec_payload_is_classified():
    assert command_risk_class("env rm -rf build") == "destructive"
    assert command_risk_class("env FOO=1 curl https://example.com") == "external_effect"
    assert command_risk_class('env -S "rm -rf build"') == "destructive"
    assert command_risk_class('env --split-string="curl https://example.com"') == "external_effect"
    assert command_risk_class("env FOO=1 printenv") == "workspace_write"


def test_find_exec_and_delete_are_not_read_only():
    assert command_risk_class("find . -delete") == "destructive"
    assert command_risk_class("find . -exec rm -rf {} ;") == "destructive"
    assert command_risk_class("find . -exec curl https://example.com ;") == "external_effect"


def test_git_global_options_do_not_hide_subcommand_risk():
    assert command_risk_class("git -C . reset --hard") == "destructive"
    assert command_risk_class("git -c protocol.version=2 push") == "destructive"
    assert command_risk_class("git --git-dir=.git reset --hard") == "destructive"


def test_shell_wrapper_recursion_is_bounded():
    # 直接给 _depth 一个已经贴顶的初值，模拟被恶意/失控输入炸深的场景。
    from pico.recovery_policy import _MAX_SHELL_WRAPPER_DEPTH

    assert command_risk_class("rm -rf build", _depth=_MAX_SHELL_WRAPPER_DEPTH) == "destructive"


def test_command_approval_is_risk_class_driven():
    assert evaluate_command_approval("read_only")["decision"] == "allow"
    assert evaluate_command_approval("workspace_write")["decision"] == "allow"
    assert evaluate_command_approval("destructive")["decision"] == "ask"
    assert evaluate_command_approval("external_effect")["decision"] == "ask"
