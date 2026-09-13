"""Explicit Codex connection selects the published catalog, with reversible ownership."""
import json
import tomllib

import pytest

from config_overlay import apply_overlay, main, restore_overlay


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("CODEXHUB_ROLLBACK_PROVENANCE_DIR", str(tmp_path / "provenance"))


@pytest.mark.parametrize("old_exists", [True, False])
def test_explicit_connection_selects_catalog_and_restores_original(tmp_path, old_exists):
    old = tmp_path / "old.json"
    if old_exists:
        old.write_text('{"models": []}')
    managed = tmp_path / "team's-model-catalogs" / "codexhub-model-catalog.json"
    managed.parent.mkdir()
    managed.write_text('{"models": [{"slug": "external/test-model"}]}')
    config = tmp_path / "config.toml"
    backup = tmp_path / "backup.toml"
    original = 'model = "official-test"\nmodel_catalog_json = ' + json.dumps(str(old)) + "\n"
    config.write_text(original)
    args = ["apply", "--config", str(config), "--backup", str(backup),
            "--catalog", str(managed), "--use-managed-catalog",
            "--base-url", "http://127.0.0.1:19099"]
    for _ in range(2):
        assert main(args) == 0
        selected = tomllib.loads(config.read_text())["model_catalog_json"]
        assert selected == str(managed)
        assert backup.read_text() == original
    assert old.exists() == old_exists
    if old_exists:
        assert old.read_text() == '{"models": []}'
    restore_overlay(config, backup)
    assert config.read_text() == original


@pytest.mark.parametrize("reconnect", [False, True])
def test_user_catalog_edit_survives_disconnect_even_after_reconnect(tmp_path, reconnect):
    config = tmp_path / "config.toml"
    backup = tmp_path / "backup.toml"
    managed = tmp_path / "catalog.json"
    managed.write_text('{"models": []}')
    config.write_text('model = "official-test"\n')
    apply_overlay(config, backup, managed, "http://127.0.0.1:19099", use_managed_catalog=True)
    edited = tmp_path / "edited.json"
    text = config.read_text().replace(str(managed), str(edited))
    config.write_text(text)
    if reconnect:
        apply_overlay(config, backup, managed, "http://127.0.0.1:19099", use_managed_catalog=True)
    restore_overlay(config, backup)
    assert tomllib.loads(config.read_text())["model_catalog_json"] == str(edited)


@pytest.mark.parametrize("reconnect", [False, True])
@pytest.mark.parametrize("replacement", [None, ""])
def test_user_catalog_removal_survives_disconnect_and_reconnect(tmp_path, reconnect, replacement):
    config = tmp_path / "config.toml"
    backup = tmp_path / "backup.toml"
    managed = tmp_path / "catalog.json"
    managed.write_text('{"models": []}')
    config.write_text('model_catalog_json = "user.json"\nmodel = "official-test"\n')
    apply_overlay(config, backup, managed, "http://127.0.0.1:19099", use_managed_catalog=True)
    edited = "".join(
        ('model_catalog_json = ""\n' if replacement == "" else "")
        if line.startswith("model_catalog_json =") else line
        for line in config.read_text().splitlines(keepends=True)
    )
    config.write_text(edited)
    if reconnect:
        apply_overlay(config, backup, managed, "http://127.0.0.1:19099", use_managed_catalog=True)
    restore_overlay(config, backup)
    restored = tomllib.loads(config.read_text())
    assert restored.get("model_catalog_json") == replacement
    assert restored["model"] == "official-test"


def test_missing_config_still_restores_saved_catalog(tmp_path):
    config = tmp_path / "config.toml"
    backup = tmp_path / "backup.toml"
    managed = tmp_path / "catalog.json"
    original = 'model_catalog_json = "user.json"\n'
    config.write_text(original)
    apply_overlay(config, backup, managed, "http://127.0.0.1:19099", use_managed_catalog=True)
    config.unlink()
    restore_overlay(config, backup)
    assert config.read_text() == original


@pytest.mark.parametrize("name", ["team's.json", 'team"s.json', "团队#目录.json", "back\\slash.json", "del\x7f.json", "line\nfeed.json"])
def test_catalog_paths_roundtrip_through_real_toml_parser(tmp_path, name):
    config = tmp_path / "config.toml"
    backup = tmp_path / "backup.toml"
    catalog = tmp_path / name
    original = "model_catalog_json = " + json.dumps(str(catalog)) + "\n"
    config.write_text(original)
    apply_overlay(config, backup, None, "http://127.0.0.1:19099")
    assert tomllib.loads(config.read_text())["model_catalog_json"] == str(catalog)
    restore_overlay(config, backup)
    assert config.read_text() == original


def test_missing_catalog_argument_fails_before_writing(tmp_path):
    config = tmp_path / "config.toml"
    backup = tmp_path / "backup.toml"
    original = 'model = "official-test"\n'
    config.write_text(original)
    with pytest.raises(ValueError, match="requires --catalog"):
        apply_overlay(config, backup, None, "http://127.0.0.1:19099", use_managed_catalog=True)
    assert config.read_text() == original
    assert not backup.exists()


def test_cross_channel_catalog_takeover_restores_previous_connection(tmp_path):
    config = tmp_path / "config.toml"
    stable = tmp_path / "stable.json"
    beta = tmp_path / "beta.json"
    stable.write_text('{"models": []}')
    beta.write_text('{"models": []}')
    apply_overlay(config, tmp_path / "stable.backup", stable, "http://127.0.0.1:19099",
                  use_managed_catalog=True)
    previous = config.read_text()
    for _ in range(2):
        apply_overlay(config, tmp_path / "beta.backup", beta, "http://127.0.0.1:19100",
                      owner="beta", takeover=True, use_managed_catalog=True)
    restore_overlay(config, tmp_path / "beta.backup")
    assert config.read_text() == previous
