from dataclasses import replace
from types import SimpleNamespace

import pytest

from imap_mcp import accounts
from imap_mcp.folders import folder_role, resolve_folder


def folder(name, delim="/", flags=()):
    return SimpleNamespace(name=name, delim=delim, flags=flags)


@pytest.mark.parametrize(
    "name, delim, role",
    [
        ("INBOX.Drafts", ".", "drafts"),
        ("INBOX/Drafts", "/", "drafts"),
        ("INBOX|Brouillons", "|", "drafts"),
        ("Draft", "NIL", "drafts"),
        ("Entwürfe", "/", "drafts"),
        ("Bozze", "/", "drafts"),
        ("Borradores", "/", "drafts"),
        ("Éléments envoyés", "/", "sent"),
        ("Éléments supprimés", "/", "trash"),
        ("Spam", "/", "junk"),
        ("All Mail", "/", "archive"),
        ("INBOX", "/", "inbox"),
        ("Projects", "/", None),
        ("Projects.Drafts/Other", "/", None),
    ],
)
def test_localized_leaf_roles(name, delim, role):
    assert folder_role(folder(name, delim)) == role


def test_role_precedence():
    assert folder_role(folder("Drafts", flags=("\\sEnT",)), {"junk": "Drafts"}) == "sent"
    assert folder_role(folder("Drafts"), {"junk": "Drafts"}) == "drafts"
    assert folder_role(folder("Custom"), {"drafts": "Custom"}) == "drafts"


def test_resolution_prioritizes_special_use_and_skips_noselect(config):
    box = SimpleNamespace(
        folder=SimpleNamespace(
            list=lambda: [
                folder("Drafts"),
                folder("Container", flags=("\\Drafts", "\\Noselect")),
                folder("Special", flags=("\\DRAFTS",)),
                folder("CaseSensitive"),
            ]
        )
    )
    account = accounts.get_account("alpha")
    assert resolve_folder(box, account, "drafts") == "Special"
    assert resolve_folder(box, account, "CaseSensitive") == "CaseSensitive"
    assert resolve_folder(box, account, None) == "INBOX"
    with pytest.raises(ValueError, match="no selectable folder"):
        resolve_folder(box, account, "casesensitive")


def test_resolution_config_fallback_requires_existing_folder(config):
    box = SimpleNamespace(folder=SimpleNamespace(list=lambda: [folder("Custom")]))
    account = replace(accounts.get_account("alpha"), folders={"drafts": "Custom"})
    assert resolve_folder(box, account, "drafts") == "Custom"
    with pytest.raises(ValueError):
        resolve_folder(box, replace(account, folders={"drafts": "Missing"}), "drafts")
