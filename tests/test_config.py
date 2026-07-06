from pico.config import load_pico_toml, project_max_blob_size
from pico.recovery_policy import DEFAULT_MAX_BLOB_SIZE, snapshot_eligibility


def test_load_pico_toml_reads_simple_project_overrides(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[policy]\nmax_blob_size = 2048\n",
        encoding="utf-8",
    )

    assert load_pico_toml(tmp_path)["policy"]["max_blob_size"] == 2048


def test_project_max_blob_size_falls_back_to_default_when_missing(tmp_path):
    # 没有 pico.toml 时，project_max_blob_size 必须给出默认值，
    # 保证调用方可以无条件把返回值传给 snapshot_eligibility。
    assert project_max_blob_size(tmp_path) == DEFAULT_MAX_BLOB_SIZE


def test_pico_toml_max_blob_size_overrides_snapshot_eligibility(tmp_path):
    # 一份 300 字节的文本文件：默认阈值下 eligible；把上限压到 100 后变 ineligible。
    file_rel = "notes/large.md"
    file_abs = tmp_path / file_rel
    file_abs.parent.mkdir(parents=True, exist_ok=True)
    file_abs.write_text("x" * 300, encoding="utf-8")

    baseline = snapshot_eligibility(tmp_path, file_rel)
    assert baseline["snapshot_eligible"] is True

    (tmp_path / "pico.toml").write_text(
        "[policy]\nmax_blob_size = 100\n",
        encoding="utf-8",
    )

    override_limit = project_max_blob_size(tmp_path)
    assert override_limit == 100
    tightened = snapshot_eligibility(tmp_path, file_rel, max_blob_size=override_limit)
    assert tightened["snapshot_eligible"] is False
    assert tightened["ineligible_reason"] == "file_too_large"
