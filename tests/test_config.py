from pico.config import load_pico_toml


def test_load_pico_toml_reads_simple_project_overrides(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[policy]\nmax_blob_size = 2048\n",
        encoding="utf-8",
    )

    assert load_pico_toml(tmp_path)["policy"]["max_blob_size"] == 2048
