import pytest

from imap_mcp import accounts


def test_load_reads_fields_and_defaults(config):
    accts = {a.key: a for a in accounts.all_accounts()}
    assert set(accts) == {"alpha", "gmailish", "off"}

    alpha = accts["alpha"]
    assert alpha.email == "alpha@example.com"
    assert alpha.host == "imap.example.com"
    assert alpha.port == 1993
    assert alpha.password_env == "ALPHA_PW"
    assert alpha.enabled

    # defaults: port 993, label falls back to email, enabled true
    gmailish = accts["gmailish"]
    assert gmailish.port == 993
    assert gmailish.label == "beta@gmail.com"
    assert gmailish.enabled


def test_enabled_accounts_skips_disabled(config):
    assert [a.key for a in accounts.enabled_accounts()] == ["alpha", "gmailish"]


def test_get_account_unknown_key(config):
    with pytest.raises(KeyError, match="unknown account 'nope'"):
        accounts.get_account("nope")


def test_password_from_env(config, monkeypatch):
    alpha = accounts.get_account("alpha")
    assert alpha.password() is None
    monkeypatch.setenv("ALPHA_PW", "hunter2")
    assert alpha.password() == "hunter2"
    monkeypatch.setenv("ALPHA_PW", "")
    assert alpha.password() is None  # empty counts as unset


def test_missing_config_error_mentions_example(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(tmp_path / "nope.toml"))
    accounts._load.cache_clear()
    try:
        with pytest.raises(FileNotFoundError, match="accounts.example.toml"):
            accounts.all_accounts()
    finally:
        accounts._load.cache_clear()


def test_example_config_is_loadable(monkeypatch):
    example = accounts.DEFAULT_CONFIG_PATH.parent / "accounts.example.toml"
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(example))
    accounts._load.cache_clear()
    try:
        accts = accounts.all_accounts()
        assert accts, "example config should define at least one account"
        assert all(a.password_env for a in accts)
    finally:
        accounts._load.cache_clear()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout(config, timeout):
    from dataclasses import replace

    with pytest.raises(ValueError, match="timeout_seconds"):
        replace(accounts.get_account("alpha"), timeout_seconds=timeout)


def test_timeout_config(config):
    config.write_text(config.read_text().replace("port = 1993", "port = 1993\ntimeout_seconds = 8"))
    assert accounts.get_account("alpha").timeout_seconds == 8
    assert accounts.get_account("gmailish").timeout_seconds == 30


def test_folder_config(config):
    config.write_text(config.read_text() + '\n[account.folders]\ndrafts = "Custom"\n')
    assert accounts.get_account("off").folders == {"drafts": "Custom"}
    assert accounts.get_account("alpha").folders == {}


@pytest.mark.parametrize("folders", [{"unknown": "X"}, {"drafts": ""}, {"drafts": 1}])
def test_invalid_folder_config(config, folders):
    from dataclasses import replace

    with pytest.raises(ValueError, match="folders"):
        replace(accounts.get_account("alpha"), folders=folders)
