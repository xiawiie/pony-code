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


def test_command_approval_is_risk_class_driven():
    assert evaluate_command_approval("read_only")["decision"] == "allow"
    assert evaluate_command_approval("workspace_write")["decision"] == "allow"
    assert evaluate_command_approval("destructive")["decision"] == "ask"
    assert evaluate_command_approval("external_effect")["decision"] == "ask"
