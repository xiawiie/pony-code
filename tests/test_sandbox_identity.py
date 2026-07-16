import base64
import hashlib
import subprocess
import sys
import venv
import zipfile

import pytest

from pico.sandbox import identity as identity


def test_installed_tree_digest_is_stable_and_rejects_unsupported_entries(tmp_path):
    package = tmp_path / "pico"
    package.mkdir()
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    data = package / "data.json"
    data.write_text("{}\n", encoding="utf-8")
    data.chmod(0o644)

    first = identity.installed_tree_digest(package)
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"ignored")
    assert identity.installed_tree_digest(package) == first

    data.write_text('{"changed":true}\n', encoding="utf-8")
    assert identity.installed_tree_digest(package) != first

    (package / "linked").symlink_to("module.py")
    with pytest.raises(identity.SandboxIdentityError) as caught:
        identity.installed_tree_digest(package)
    assert caught.value.code == "installed_distribution_invalid"


def test_installed_tree_only_ignores_generated_cache_bytecode(tmp_path):
    package = tmp_path / "pico"
    cache = package / "__pycache__"
    cache.mkdir(parents=True)
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    first = identity.installed_tree_digest(package)

    (cache / "module.pyc").write_bytes(b"generated")
    assert identity.installed_tree_digest(package) == first

    (cache / "payload.so").write_bytes(b"not bytecode")
    assert identity.installed_tree_digest(package) != first
    (cache / "payload.so").unlink()

    (package / "payload.pyc").write_bytes(b"root bytecode is not generated cache")
    assert identity.installed_tree_digest(package) != first


def _write_installed_distribution(root, version="0.1.0"):
    package = root / "pico"
    package.mkdir()
    module = package / "module.py"
    module.write_text("value = 1\n", encoding="utf-8")
    dist_info = root / f"pico-{version}.dist-info"
    dist_info.mkdir()
    files = {
        module: "pico/module.py",
        dist_info / "METADATA": f"pico-{version}.dist-info/METADATA",
        dist_info / "WHEEL": f"pico-{version}.dist-info/WHEEL",
        dist_info / "entry_points.txt": f"pico-{version}.dist-info/entry_points.txt",
        dist_info / "top_level.txt": f"pico-{version}.dist-info/top_level.txt",
    }
    for path in files:
        if path != module:
            path.write_text(path.name + "\n", encoding="utf-8")
    rows = []
    for path, relative in files.items():
        raw = path.read_bytes()
        encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        rows.append(
            f"{relative},sha256={encoded.rstrip(b'=').decode('ascii')},{len(raw)}"
        )
    rows.append(f"pico-{version}.dist-info/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return package, dist_info


def test_installed_distribution_digest_binds_record_and_dist_info(tmp_path):
    package, dist_info = _write_installed_distribution(tmp_path)

    digest = identity.installed_tree_digest(package, "0.1.0")

    assert digest.startswith("sha256:")
    (dist_info / "METADATA").write_text("changed\n", encoding="utf-8")
    with pytest.raises(identity.SandboxIdentityError) as caught:
        identity.installed_tree_digest(package, "0.1.0")
    assert caught.value.code == "installed_distribution_invalid"


def _write_test_wheel(root):
    wheel = root / "pico-0.1.0-py3-none-any.whl"
    dist_info = "pico-0.1.0.dist-info"
    files = {
        "pico/__init__.py": b"def main():\n    return None\n",
        f"{dist_info}/METADATA": (
            b"Metadata-Version: 2.1\nName: pico\nVersion: 0.1.0\n"
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": b"[console_scripts]\npico = pico:main\n",
        f"{dist_info}/top_level.txt": b"pico\n",
    }
    rows = []
    for relative, raw in files.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        rows.append(
            f"{relative},sha256={encoded.rstrip(b'=').decode('ascii')},{len(raw)}"
        )
    rows.append(f"{dist_info}/RECORD,,")
    with zipfile.ZipFile(wheel, "w") as archive:
        for relative, raw in files.items():
            archive.writestr(relative, raw)
        archive.writestr(f"{dist_info}/RECORD", "\n".join(rows) + "\n")
    return wheel


def test_installed_distribution_accepts_a_real_pip_console_script(tmp_path):
    wheel = _write_test_wheel(tmp_path)
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-index",
            str(wheel),
        ],
        check=True,
        capture_output=True,
    )
    installed = subprocess.run(
        [
            str(python),
            "-c",
            "from pathlib import Path; import pico; "
            "root=Path(pico.__file__).resolve().parent; "
            "record=root.parent/'pico-0.1.0.dist-info'/'RECORD'; "
            "print(root); print(record.read_text())",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    ).stdout.splitlines()
    package = installed[0]

    assert any(line.startswith("../../../bin/pico,") for line in installed[1:])
    assert any("pico/__pycache__/" in line for line in installed[1:])
    assert identity.installed_tree_digest(package, "0.1.0").startswith("sha256:")


def test_installed_distribution_rejects_unhashed_non_cache_package_record(tmp_path):
    package, dist_info = _write_installed_distribution(tmp_path)
    with (dist_info / "RECORD").open("a", encoding="utf-8") as record:
        record.write("pico/unhashed.py,,\n")

    with pytest.raises(identity.SandboxIdentityError) as caught:
        identity.installed_tree_digest(package, "0.1.0")

    assert caught.value.code == "installed_distribution_invalid"


@pytest.mark.parametrize(
    "path",
    (
        "/bin/pico",
        "..",
        "../../../bin/../pico",
        "../../../bin\\pico",
        "../../../bin/pico\0suffix",
        "../../../bin/pico\x7fsuffix",
        "pico//module.py",
        "pico/./module.py",
    ),
)
def test_installed_record_rejects_unsafe_paths(path):
    with pytest.raises(identity.SandboxIdentityError) as caught:
        identity._installed_record_rows(f"{path},,\n".encode())

    assert caught.value.code == "installed_distribution_invalid"


def test_installed_record_rejects_duplicate_external_path():
    with pytest.raises(identity.SandboxIdentityError) as caught:
        identity._installed_record_rows(
            b"../../../bin/pico,,\n../../../bin/pico,,\n"
        )

    assert caught.value.code == "installed_distribution_invalid"


def test_installed_record_accepts_standard_external_console_script():
    assert identity._installed_record_rows(b"../../../bin/pico,,\n") == {
        "../../../bin/pico": ("", "")
    }


@pytest.mark.parametrize("relative", ("__pycache__/payload.so", "payload.pyc"))
def test_installed_distribution_does_not_ignore_other_cache_like_files(
    tmp_path,
    relative,
):
    package, _dist_info = _write_installed_distribution(tmp_path)
    original = identity.installed_tree_digest(package, "0.1.0")
    path = package / relative
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"payload")

    with pytest.raises(identity.SandboxIdentityError) as caught:
        identity.installed_tree_digest(package, "0.1.0")

    assert caught.value.code == "installed_distribution_invalid"
    path.unlink()
    assert identity.installed_tree_digest(package, "0.1.0") == original
