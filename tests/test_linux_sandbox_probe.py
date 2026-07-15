import importlib.util
import json
from pathlib import Path


def _module():
    spec = importlib.util.spec_from_file_location(
        "linux_sandbox_probe_script",
        Path("scripts/linux_sandbox_probe.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_linux_probe_passes_verified_managed_identity(monkeypatch, capsys):
    module = _module()
    identity = object()
    observed = {}

    class Report:
        platform = "Linux"
        status = "not_ready"

        @staticmethod
        def to_dict():
            return {"platform": "Linux", "status": "not_ready"}

    def probe(**kwargs):
        observed.update(kwargs)
        return Report()

    monkeypatch.setattr(module, "_managed_identity", lambda: identity)
    monkeypatch.setattr(module, "probe", probe)

    assert module.main(["--format", "json", "--timeout", "1"]) == 2
    assert observed == {"timeout": 1.0, "sandbox_identity": identity}
    assert json.loads(capsys.readouterr().out)["status"] == "not_ready"
