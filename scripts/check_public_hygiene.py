"""Check the Git index without printing matched addresses or secret candidates."""

import re
import subprocess
import sys
import tomllib
from pathlib import PurePosixPath

EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@(?:[\w-]+\.)+[\w-]{2,}")
APP_PASSWORD = re.compile(r"(?<![A-Za-z0-9_])[a-z]{4}(?: ?[a-z]{4}){3}(?![A-Za-z0-9_])")
ALLOWED_DOMAINS = {"example.com", "example.org", "users.noreply.github.com"}
MARKER = "<!-- hygiene: allow -->"


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, shell=False).stdout


def upstream_addresses(files):
    """Only attribution fields qualify; arbitrary text in metadata cannot grant exemptions."""
    addresses = set()
    if "LICENSE" in files:
        for line in files["LICENSE"].splitlines():
            if line.lower().startswith("copyright"):
                addresses.update(address.lower() for address in EMAIL.findall(line))
    if "pyproject.toml" in files:
        try:
            project = tomllib.loads(files["pyproject.toml"]).get("project", {})
            for author in project.get("authors", []):
                if isinstance(author, dict) and isinstance(author.get("email"), str):
                    addresses.add(author["email"].lower())
        except tomllib.TOMLDecodeError:
            pass  # Invalid TOML grants no exemption; the package checks also reject it.
    return addresses


def scan_text(path, text, attribution):
    check_passwords = path.suffix in {".toml", ".json", ".md"} or (
        path.suffix == ".py" and "tests" in path.parts[:-1]
    )
    pending = False
    fence = None
    exempt = False
    for number, line in enumerate(text.splitlines(), 1):
        # Only a marked Markdown code fence exempts password-shaped educational examples.
        # It never exempts addresses or forbidden paths.
        opening = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line) if path.suffix == ".md" else None
        if fence:
            if re.fullmatch(
                r"\s{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line
            ):
                fence, exempt = None, False
        elif opening:
            fence = opening[1]
            exempt = pending or MARKER in opening[2]
            pending = False
        else:
            pending = path.suffix == ".md" and line.strip() == MARKER
        for address in EMAIL.findall(line):
            domain = address.rsplit("@", 1)[1].lower()
            if domain not in ALLOWED_DOMAINS and not (
                str(path) in {"LICENSE", "pyproject.toml"} and address.lower() in attribution
            ):
                yield number, "non-example email address"
        if check_passwords and not exempt and APP_PASSWORD.search(line):
            yield number, "app-password-shaped text"


def check():
    files = {}
    failures = []
    entries = git("ls-files", "--stage", "-z").decode().split("\0")
    for entry in filter(None, entries):
        metadata, name = entry.split("\t", 1)
        mode, blob, stage = metadata.split()
        path = PurePosixPath(name)
        if (
            path.name == "accounts.toml"
            or path.suffix.lower() in {".eml", ".mbox"}
            or path.parts[:2] == ("docs", "integration")
        ):
            failures.append((name, 0, "forbidden tracked path"))
        if mode not in {"100644", "100755"} or stage != "0":
            failures.append((name, 0, "unsupported index entry (symlink, submodule or conflict)"))
            continue
        files[name] = git("cat-file", "blob", blob).decode("utf-8", errors="replace")
    attribution = upstream_addresses(files)
    for name, text in files.items():
        failures.extend(
            (name, line, rule) for line, rule in scan_text(PurePosixPath(name), text, attribution)
        )
    for name, line, rule in failures:
        # Never print the matching content; CI logs are public too.
        print(f"{name}:{line}: {rule}")
    if not failures:
        print("Public hygiene checks passed.")
    return bool(failures)


if __name__ == "__main__":
    try:
        sys.exit(check())
    except subprocess.CalledProcessError:
        sys.exit("Unable to inspect Git index; hygiene check failed.")
