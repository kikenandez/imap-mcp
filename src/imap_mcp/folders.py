"""Folder roles adapted from bgalmes/rubit-mcp-mail (MIT).

Copyright (c) 2026 Bernat Galmés Rubert. See licenses/rubit-mcp-mail-MIT.txt.
Source: src/rubit_mcp_mail/backends/imap.py (folder_role, NAME_PATTERNS, _fold).
"""

import re
import unicodedata

SPECIAL_USE = {
    "\\inbox": "inbox",
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\junk": "junk",
    "\\trash": "trash",
    "\\archive": "archive",
    "\\all": "archive",
}
ROLES = set(SPECIAL_USE.values())
NAME_PATTERNS = [
    (
        r"^inbox$|bandeja de entrada|safata d.entrada|boite de reception|"
        r"posteingang|caixa de entrada|posta in arrivo",
        "inbox",
    ),
    (r"sent|enviad|enviat|envoy|gesendete|inviata|itens enviados", "sent"),
    (r"draft|borrador|esborrany|brouillon|entwurf|entwurfe|bozze|rascunho", "drafts"),
    (r"junk|spam|bulk|no deseado|brossa|indesirable|indesiderata|lixo|unerwunscht", "junk"),
    (
        r"trash|deleted|elimin|suprimit|supprim|gelosch|papierkorb|papelera|excluid|cestino|bin$",
        "trash",
    ),
    (r"archive|archivo|arxiu|archiv|arquivo|archivio|all mail", "archive"),
]


def _fold(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(ch)
    )


def _flag(flag) -> str:
    return (flag.decode("ascii", "replace") if isinstance(flag, bytes) else flag).lower()


def _role_match(info, overrides):
    """Return role and precedence: SPECIAL-USE, leaf name, config fallback."""
    for flag in info.flags:
        if role := SPECIAL_USE.get(_flag(flag)):
            return role, 0
    delimiter = info.delim
    leaf = info.name.rsplit(delimiter, 1)[-1] if delimiter and delimiter != "NIL" else info.name
    for pattern, role in NAME_PATTERNS:
        if re.search(pattern, _fold(leaf.strip())):
            return role, 1
    for role, name in overrides.items():
        if name == info.name:
            return role, 2
    return None, 3


def folder_role(info, overrides=None) -> str | None:
    return _role_match(info, overrides or {})[0]


def folder_rows(box, account) -> list[dict]:
    return [
        {
            "name": f.name,
            "delimiter": None if f.delim == "NIL" else f.delim,
            "flags": list(f.flags),
            "role": folder_role(f, account.folders),
        }
        for f in box.folder.list()
    ]


def resolve_folder(box, account, folder: str | None) -> str:
    if folder is None or folder.lower() == "inbox":
        return "INBOX"
    folders = [f for f in box.folder.list() if "\\noselect" not in map(_flag, f.flags)]
    if folder.lower() in ROLES:
        matches = [
            (rank, i, f.name)
            for i, f in enumerate(folders)
            for role, rank in [_role_match(f, account.folders)]
            if role == folder.lower()
        ]
        if matches:
            return min(matches)[2]
    else:
        for f in folders:
            if f.name == folder:
                return f.name
    raise ValueError(
        f"no selectable folder matching {folder!r} for {account.key}; "
        "check list_folders and account.folders configuration"
    )
