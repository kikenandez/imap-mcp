"""Resolve secret references on demand; never cache or log secret values."""

import os
import re
import subprocess
import sys


class MissingCredentialError(RuntimeError):
    """The configured secret source is missing or unavailable."""


def validate_reference(reference: str, key: str) -> None:
    if not isinstance(reference, str):
        raise ValueError("password_ref must be a string using env: or keychain:")
    if reference.startswith("env:"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", reference[4:]):
            raise ValueError("env: requires an environment variable name")
    elif reference.startswith("keychain:"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError("keychain account keys must contain only letters, digits, _ or -")
        if reference != f"keychain:pkma-mail-{key}/pkma":
            raise ValueError("keychain references must use keychain:pkma-mail-<key>/pkma")
    else:
        raise ValueError("password_ref supports only env: and keychain:")


def resolve_password(reference: str, key: str) -> str | None:
    validate_reference(reference, key)
    if reference.startswith("env:"):
        return os.environ.get(reference[4:]) or None
    if sys.platform != "darwin":
        raise MissingCredentialError(f"no-credential: {reference} (requires macOS)")
    service, account = reference[len("keychain:") :].split("/")
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            shell=False,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        # Raise outside the handler so even the exception context has no backend output.
        result = None
    if result is None:
        raise MissingCredentialError(f"no-credential: {reference}")
    if result.returncode == 44:  # errSecItemNotFound
        return None
    if result.returncode != 0:
        # stderr/stdout can contain sensitive data. Never include them in errors.
        raise MissingCredentialError(f"no-credential: {reference}")
    return result.stdout.removesuffix("\n") or None
