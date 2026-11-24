from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from labpilot.errors import ErrorCode, LabPilotError
from labpilot.procutil import scrubbed_env
from labpilot.security import (
    OLE_MAGIC,
    check_docx_container,
    enforce_code_policy,
    redact,
    safe_join,
    scan_code,
)


def test_safe_join_rejects_traversal_and_absolute(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "a/b.csv") == (tmp_path / "a" / "b.csv").resolve()
    for bad in ("../x.csv", "a/../../x", str(tmp_path.anchor) + "Windows", "/etc/passwd"):
        with pytest.raises(LabPilotError) as exc:
            safe_join(tmp_path, bad)
        assert exc.value.code == ErrorCode.PATH_ESCAPE


def test_safe_join_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")
    with pytest.raises(LabPilotError):
        safe_join(root, "link/secret.txt")


def test_redaction_removes_tokens_emails_and_home() -> None:
    text = (f"token=ya29.abcdefghijklmnopqrstuv key AIza{'x' * 35} sk-{'a' * 24} "
            f"me@example.com {Path.home() / 'secret'} Authorization: Bearer abcdefghijklmnop")
    out = redact(text)
    assert "ya29." not in out and "AIza" not in out and "sk-aaaa" not in out
    assert "me@example.com" not in out and str(Path.home()) not in out
    assert "abcdefghijklmnop" not in out
    assert str(Path.home()) in redact(str(Path.home()), home=False)


def test_scrubbed_env_drops_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("MY_SECRET", "x")
    monkeypatch.setenv("HARMLESS_SETTING", "ok")
    env = scrubbed_env()
    assert "OPENAI_API_KEY" not in env and "GITHUB_TOKEN" not in env and "MY_SECRET" not in env
    assert env["HARMLESS_SETTING"] == "ok"


def test_docx_container_checks(tmp_path: Path) -> None:
    doc = tmp_path / "a.doc"
    doc.write_bytes(b"x")
    with pytest.raises(LabPilotError) as e1:
        check_docx_container(doc)
    assert e1.value.code == ErrorCode.DOCX_UNSUPPORTED

    enc = tmp_path / "enc.docx"
    enc.write_bytes(OLE_MAGIC + b"\x00" * 100)
    with pytest.raises(LabPilotError) as e2:
        check_docx_container(enc)
    assert e2.value.code == ErrorCode.DOCX_ENCRYPTED

    junk = tmp_path / "junk.docx"
    junk.write_bytes(b"not a zip at all")
    with pytest.raises(LabPilotError) as e3:
        check_docx_container(junk)
    assert e3.value.code == ErrorCode.DOCX_CORRUPT

    nodoc = tmp_path / "nodoc.docx"
    with zipfile.ZipFile(nodoc, "w") as zf:
        zf.writestr("hello.txt", "hi")
    with pytest.raises(LabPilotError):
        check_docx_container(nodoc)

    bomb = tmp_path / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", "<x/>")
        zf.writestr("word/media/big.bin", b"\x00" * (20 * 1024 * 1024))
    with pytest.raises(LabPilotError) as e4:
        check_docx_container(bomb)
    assert "suspicious" in e4.value.message


@pytest.mark.parametrize(
    ("code", "rule", "severity"),
    [
        ("from google.colab import files\nfiles.upload()", "interactive-upload", "deny"),
        ("name = input('x')", "interactive-input", "deny"),
        ("open(os.path.expanduser('~/.ssh/id_rsa'))", "credential-access", "deny"),
        ("print(os.environ)", "env-secrets", "deny"),
        ("from google.colab import drive\ndrive.mount('/content/drive')", "drive-mount", "deny"),
        ("import shutil\nshutil.rmtree('/')", "destructive-fs", "deny"),
        ("!pip install foo", "shell", "warn"),
        ("import requests\nrequests.get('http://x')", "network", "warn"),
        ("pd.read_csv('/content/a.csv')", "absolute-path", "warn"),
    ],
)
def test_code_rules(code: str, rule: str, severity: str) -> None:
    findings = scan_code("c1", code)
    hit = [f for f in findings if f.rule == rule]
    assert hit and hit[0].severity == severity
    denied, _ = enforce_code_policy(findings, set())
    assert bool([d for d in denied if d.rule == rule]) == (severity == "deny")
    denied2, _ = enforce_code_policy(findings, {rule})
    assert not [d for d in denied2 if d.rule == rule]


@pytest.mark.parametrize(
    ("code", "denied"),
    [
        ("rm -rf /", True),
        ("rm -rf / --no-preserve-root", True),
        ("rm -rf ~", True),
        ("rm -rf $HOME/x", True),
        ("rm -rf /home/user", True),
        ("rm -rf /etc", True),
        ("import shutil\nshutil.rmtree('/')", True),
        ("rm -rf /content/hdfs /content/hadoop-tmp", False),  # Colab VM lab workspace
        ("rm -rf /content/hadoop-3.3.6 /content/hadoop-3.3.6.tar.gz", False),
        ("rm -rf /tmp/build", False),
        ("rm -f hadoop.tar.gz", False),
    ],
)
def test_destructive_rule_targets_system_paths_not_lab_workspace(code: str, denied: bool) -> None:
    hits = [f for f in scan_code("c", code) if f.rule == "destructive-fs"]
    assert bool(hits) == denied, code


def test_plain_code_is_clean() -> None:
    assert scan_code("c", "df = pd.read_csv('sales.csv')\nprint(df.head())\nmy_input_value = 3") == []
