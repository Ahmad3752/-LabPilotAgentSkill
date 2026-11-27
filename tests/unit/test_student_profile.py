"""The student name/ID is asked once, saved in the local user config and reused by later runs."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from conftest import load_example
from labpilot.cli import app
from labpilot.config import config_path, load_config, remember_student, saved_student


def _init(manual: Path, *extra: str) -> dict:
    res = CliRunner().invoke(app, ["init", "--lab", str(manual), "--json", *extra])
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def test_remember_student_saves_overwrites_and_keeps_missing_parts() -> None:
    assert saved_student(load_config()) is None
    assert remember_student(None, None) == {}
    assert not config_path().exists()  # nothing given, nothing written
    assert remember_student("Ali Khan", "123456") == {"name": "Ali Khan", "id": "123456"}
    assert remember_student(" Ali K. ", None) == {"name": "Ali K.", "id": "123456"}
    assert saved_student(load_config()) == {"name": "Ali K.", "id": "123456"}


def test_init_saves_once_and_reuses_but_never_for_synthetic(tmp_path: Path) -> None:
    manual = load_example("python", "make_lab").make(tmp_path / "lab")
    assert _init(manual, "--synthetic", "--student-name", "Demo")["student"] == {"name": "Demo"}
    assert saved_student(load_config()) is None  # a synthetic demo never saves details
    assert _init(manual)["student"] == {}
    assert _init(manual, "--student-name", "Sara", "--student-id", "999")["student"] == {"name": "Sara", "id": "999"}
    assert _init(manual)["student"] == {"name": "Sara", "id": "999"}  # reused without asking again
    assert _init(manual, "--student-id", "1000")["student"] == {"name": "Sara", "id": "1000"}
    assert _init(manual, "--synthetic")["student"] == {}
