import builtins

from pico.recovery import policy as recovery_policy
from pico.recovery.policy import (
    command_risk_class,
    evaluate_command_approval,
    snapshot_bytes_eligibility,
    snapshot_eligibility,
)


def test_snapshot_eligibility_is_conservative(tmp_path):
    text_file = tmp_path / "src" / "app.py"
    text_file.parent.mkdir()
    text_file.write_text("print('hi')\n", encoding="utf-8")
    binary_file = tmp_path / "image.bin"
    binary_file.write_bytes(b"\x00\x01")

    assert snapshot_eligibility(tmp_path, "src/app.py")["snapshot_eligible"] is True
    assert snapshot_eligibility(tmp_path, "image.bin")["ineligible_reason"] == "binary_file"


def test_snapshot_rejects_sensitive_path_before_resolution_or_read(
    tmp_path,
    monkeypatch,
):
    (tmp_path / ".env").write_text("PICO_TOKEN=opaque\n", encoding="utf-8")
    monkeypatch.setattr(
        recovery_policy.Path,
        "resolve",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("sensitive path resolved")
        ),
    )

    result = snapshot_eligibility(tmp_path, ".env")

    assert result["snapshot_eligible"] is False
    assert result["ineligible_reason"] == "sensitive_path"


def test_snapshot_rejects_sensitive_descendant_before_read(tmp_path, monkeypatch):
    target = tmp_path / ".env" / "child.txt"
    target.parent.mkdir()
    target.write_text("must not read\n", encoding="utf-8")
    real_lstat = target.__class__.lstat

    def guarded_lstat(self, *args, **kwargs):
        if self == target:
            raise AssertionError("sensitive descendant read")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(
        target.__class__,
        "lstat",
        guarded_lstat,
    )

    result = snapshot_eligibility(tmp_path, ".env/child.txt")

    assert result["snapshot_eligible"] is False
    assert result["ineligible_reason"] == "sensitive_path"


def test_snapshot_classifies_env_template_directory_as_sensitive(tmp_path):
    (tmp_path / ".env.example").mkdir()

    result = snapshot_eligibility(tmp_path, ".env.example")

    assert result["snapshot_eligible"] is False
    assert result["ineligible_reason"] == "sensitive_path"


def test_snapshot_allows_regular_env_template_file(tmp_path):
    (tmp_path / ".env.example").write_text(
        "PUBLIC_SETTING=demo\n",
        encoding="utf-8",
    )

    result = snapshot_eligibility(tmp_path, ".env.example")

    assert result["snapshot_eligible"] is True


def test_snapshot_rejects_symlink_even_when_target_stays_in_workspace(tmp_path):
    target = tmp_path / "safe.txt"
    target.write_text("safe\n", encoding="utf-8")
    (tmp_path / "alias.txt").symlink_to(target)

    result = snapshot_eligibility(tmp_path, "alias.txt")

    assert result["snapshot_eligible"] is False
    assert result["ineligible_reason"] == "symlink"


def test_snapshot_scans_complete_bounded_content_with_custom_secret_name(tmp_path):
    secret = "opaque-custom-value-123456789"
    target = tmp_path / "source.py"
    target.write_text("x" * 5000 + secret, encoding="utf-8")

    result = snapshot_eligibility(
        tmp_path,
        "source.py",
        env={"CUSTOM_CREDENTIAL": secret},
        secret_env_names=("CUSTOM_CREDENTIAL",),
    )

    assert result["snapshot_eligible"] is False
    assert result["ineligible_reason"] == "sensitive_content"
    assert not (tmp_path / ".pico").exists()


def test_snapshot_size_check_is_bounded(tmp_path):
    target = tmp_path / "large.txt"
    target.write_bytes(b"x" * 65)

    result = snapshot_eligibility(tmp_path, "large.txt", max_blob_size=64)

    assert result["snapshot_eligible"] is False
    assert result["ineligible_reason"] == "file_too_large"


def test_snapshot_bytes_eligibility_blocks_sensitive_content(tmp_path, monkeypatch):
    sentinel = "sk-sensitive-recovery-value"
    monkeypatch.setenv("PICO_OPENAI_API_KEY", sentinel)

    result = snapshot_bytes_eligibility(
        tmp_path,
        "src/config.py",
        f'KEY = "{sentinel}"\n'.encode(),
        max_blob_size=1024,
    )

    assert result["snapshot_eligible"] is False
    assert result["ineligible_reason"] == "sensitive_content"


def test_snapshot_bytes_eligibility_reuses_exact_binary_and_size_policy(tmp_path):
    binary = snapshot_bytes_eligibility(
        tmp_path,
        "artifact.dat",
        b"text-prefix\x00text-suffix",
        max_blob_size=1024,
    )
    oversized = snapshot_bytes_eligibility(
        tmp_path,
        "note.txt",
        b"x" * 65,
        max_blob_size=64,
    )

    assert binary["ineligible_reason"] == "binary_file"
    assert oversized["ineligible_reason"] == "file_too_large"


def test_snapshot_reads_safe_candidate_once_with_bounded_size(tmp_path, monkeypatch):
    target = tmp_path / "safe.txt"
    target.write_bytes(b"safe\n")
    real_open = builtins.open
    read_sizes = []

    class CountingReader:
        def __init__(self, *args, **kwargs):
            self.handle = real_open(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def read(self, size):
            read_sizes.append(size)
            return self.handle.read(size)

    def counting_open(path, *args, **kwargs):
        if path == target:
            return CountingReader(path, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)

    result = snapshot_eligibility(tmp_path, "safe.txt", max_blob_size=64)

    assert result["snapshot_eligible"] is True
    assert read_sizes == [65]


def test_snapshot_root_path_remains_a_directory_decision(tmp_path):
    result = snapshot_eligibility(tmp_path, ".")

    assert result["snapshot_eligible"] is False
    assert result["ineligible_reason"] == "directory"


def test_command_policy_compatibility_wrapper_returns_all_four_risk_classes():
    assert command_risk_class("git status --short") == "read_only"
    assert command_risk_class("cat README.md > output.txt") == "workspace_write"
    assert command_risk_class("cat .env") == "destructive"
    assert command_risk_class("python -m black pico") == "external_effect"


def test_command_approval_is_risk_class_driven():
    assert evaluate_command_approval("read_only")["decision"] == "allow"
    assert evaluate_command_approval("workspace_write")["decision"] == "allow"
    assert evaluate_command_approval("destructive")["decision"] == "ask"
    assert evaluate_command_approval("external_effect")["decision"] == "ask"
