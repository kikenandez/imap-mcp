import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from imap_mcp import accounts, credentials


@pytest.mark.parametrize(
    "ref",
    [
        "file:/tmp/secret",
        "op:item",
        "exec:command",
        "https://example.com",
        "",
        "env:",
        "env:WITH SPACE",
        "env:X\n",
        "keychain:other/pkma",
        "keychain:pkma-mail-alpha/other",
        "keychain:pkma-mail-other/pkma",
        42,
    ],
)
def test_invalid_references_are_rejected_without_echoing_value(config, ref):
    with pytest.raises(ValueError):
        replace(accounts.get_account("alpha"), password_env=None, password_ref=ref)


def test_env_reference_resolved_each_time(config, monkeypatch):
    account = replace(
        accounts.get_account("alpha"), password_env=None, password_ref="env:TEST_IMAP_PASSWORD"
    )
    monkeypatch.delenv("TEST_IMAP_PASSWORD", raising=False)
    assert account.password() is None
    monkeypatch.setenv("TEST_IMAP_PASSWORD", "fake-first")
    assert account.password() == "fake-first"
    monkeypatch.setenv("TEST_IMAP_PASSWORD", "fake-second")
    assert account.password() == "fake-second"


def test_keychain_naming_and_no_secret_cache(config, monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    results = iter(["fake-first\n", " fake-second \n"])
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=next(results))

    monkeypatch.setattr(credentials.subprocess, "run", run)
    account = replace(
        accounts.get_account("alpha"),
        password_env=None,
        password_ref="keychain:pkma-mail-alpha/pkma",
    )
    assert account.password() == "fake-first"
    assert account.password() == " fake-second "
    assert len(calls) == 2
    assert calls[0] == (
        ["/usr/bin/security", "find-generic-password", "-s", "pkma-mail-alpha", "-a", "pkma", "-w"],
        {"capture_output": True, "shell": False, "text": True, "timeout": 5, "check": False},
    )


@pytest.mark.parametrize("failure", [44, 1, "timeout", "oserror"])
def test_keychain_errors_never_leak_output(config, monkeypatch, failure):
    monkeypatch.setattr(credentials.sys, "platform", "darwin")

    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                "security", 5, output="FAKE_SECRET", stderr="FAKE_SECRET"
            )
        if failure == "oserror":
            raise OSError("FAKE_SECRET")
        return SimpleNamespace(returncode=failure, stdout="FAKE_SECRET", stderr="FAKE_SECRET")

    monkeypatch.setattr(credentials.subprocess, "run", run)
    if failure == 44:
        assert credentials.resolve_password("keychain:pkma-mail-alpha/pkma", "alpha") is None
    else:
        with pytest.raises(RuntimeError) as exc:
            credentials.resolve_password("keychain:pkma-mail-alpha/pkma", "alpha")
        assert "FAKE_SECRET" not in str(exc.value)
        assert exc.value.__suppress_context__ or exc.value.__context__ is None


def test_keychain_non_macos_fails_without_subprocess(monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="requires macOS"):
        credentials.resolve_password("keychain:pkma-mail-alpha/pkma", "alpha")


@pytest.mark.parametrize("in_root", [False, True])
def test_plaintext_password_config_rejected(config, in_root):
    raw = config.read_text()
    if in_root:
        raw = 'password = "FAKE_SECRET"\n' + raw
    else:
        raw = raw.replace("port = 1993", 'port = 1993\npassword = "FAKE_SECRET"')
    config.write_text(raw)
    with pytest.raises(ValueError, match="password fields are forbidden") as exc:
        accounts.all_accounts()
    assert "FAKE_SECRET" not in str(exc.value)


def test_ambiguous_credential_sources_rejected(config):
    config.write_text(
        config.read_text().replace("port = 1993", 'port = 1993\npassword_ref = "env:TEST"')
    )
    with pytest.raises(ValueError, match="not both"):
        accounts.all_accounts()


def test_config_does_not_read_keychain(config, monkeypatch):
    config.write_text(
        config.read_text().replace(
            'password_env = "ALPHA_PW"', 'password_ref = "keychain:pkma-mail-alpha/pkma"'
        )
    )
    monkeypatch.setattr(
        accounts, "resolve_password", lambda *a: pytest.fail("resolved during load")
    )
    assert accounts.get_account("alpha").credential_ref == "keychain:pkma-mail-alpha/pkma"
