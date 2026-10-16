import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_hygiene.py"


def scan(tmp_path, files):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "add", "--all"], cwd=tmp_path, check=True, capture_output=True)
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    "name, content, rule",
    [
        ("README.md", "someone@" + "invalid.test", "non-example email"),
        ("sample.toml", 'value = "' + "abcd" * 4 + '"', "app-password-shaped"),
        ("tests/sample.py", 'value = "' + " ".join(["abcd"] * 4) + '"', "app-password-shaped"),
        ("sample.json", '{"value":"' + "abcd" * 4 + '"}', "app-password-shaped"),
        ("README.md", "```text\n" + "abcd" * 4 + "\n```", "app-password-shaped"),
        ("accounts.toml", "", "forbidden tracked path"),
        ("nested/accounts.toml", "", "forbidden tracked path"),
        ("sample.eml", "", "forbidden tracked path"),
        ("sample.mbox", "", "forbidden tracked path"),
        ("docs/integration/brief.md", "", "forbidden tracked path"),
    ],
)
def test_each_violation_fails_without_echoing_content(tmp_path, name, content, rule):
    result = scan(tmp_path, {name: content})
    assert result.returncode == 1
    assert rule in result.stdout
    if content:
        assert content not in result.stdout + result.stderr


def test_clean_tree_and_explicit_fence_exemption(tmp_path):
    result = scan(
        tmp_path,
        {
            "README.md": "test@example.com\nother@example.org\n"
            "1+user@users.noreply.github.com\n"
            "<!-- hygiene: allow -->\n```text\n" + "abcd" * 4 + "\n```\n",
            "accounts.example.toml": 'password_ref = "env:EXAMPLE_PASSWORD"\n',
            "tests/test_sample.py": 'password = "synthetic-canary"\n',
        },
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("suffix", ["outside", "email", "unmarked", "distant"])
def test_fence_exemption_is_scoped(tmp_path, suffix):
    candidate = "abcd" * 4
    prefix = "<!-- hygiene: allow -->\n```text\n" + candidate + "\n```\n"
    tail = {
        "outside": candidate,
        "email": "<!-- hygiene: allow -->\n```\nperson@" + "invalid.test\n```",
        "unmarked": "```\n" + candidate + "\n```",
        "distant": "<!-- hygiene: allow -->\nordinary text\n```\n" + candidate + "\n```",
    }[suffix]
    assert scan(tmp_path, {"README.md": prefix + tail}).returncode == 1


def test_author_attribution_is_read_from_metadata_and_scoped(tmp_path):
    address = "upstream@" + "invalid.test"
    result = scan(
        tmp_path,
        {
            "LICENSE": "Copyright (c) Upstream <" + address + ">\n",
            "pyproject.toml": '[project]\nauthors = [{name="Upstream", email="' + address + '"}]',
        },
    )
    assert result.returncode == 0
    result = scan(tmp_path, {"README.md": address})
    assert result.returncode == 1


def test_untracked_files_are_not_inspected(tmp_path):
    (tmp_path / "accounts.toml").write_text("local only")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True)
    assert result.returncode == 0


def test_index_is_checked_even_if_worktree_is_cleaned(tmp_path):
    assert scan(tmp_path, {"README.md": "abcd" * 4}).returncode == 1
    (tmp_path / "README.md").write_text("clean worktree")
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True)
    assert result.returncode == 1


def test_symlink_is_rejected_without_reading_target(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "link").symlink_to("/nonexistent-target")
    subprocess.run(["git", "add", "link"], cwd=tmp_path, check=True, capture_output=True)
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True)
    assert result.returncode == 1 and b"unsupported index entry" in result.stdout
