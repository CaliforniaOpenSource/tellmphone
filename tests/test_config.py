import json

from tellmphone.config import CALL_ID_ENV, HOP_ENV, load_config


def test_load_config_reads_file_and_call_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    raw_project = tmp_path / "nested" / ".." / "project"
    (home / "config.toml").write_text(
        "[defaults]\n"
        "timeout_s = 42\n"
        "max_hops = 3\n"
        f"[permissions.{json.dumps(str(raw_project))}]\n"
        "write = true\n"
        "[agents.codex]\n"
        'model = "gpt-test"\n'
    )
    monkeypatch.setenv(HOP_ENV, "1")
    monkeypatch.setenv(CALL_ID_ENV, "call-1a2b3c4d")

    config = load_config(i_am="claude", home=home)

    assert config.timeout_s == 42
    assert config.max_hops == 3
    assert config.hop_count == 1
    assert config.call_id == "call-1a2b3c4d"
    assert config.permissions_for(raw_project).write
    assert config.default_model_for("codex") == "gpt-test"
    assert config.default_model_for("claude") is None
