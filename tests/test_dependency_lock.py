"""Executable installed-dependency drift checks for TASK-076."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "check_dependency_lock.py"


def _run(*args):
    return subprocess.run([sys.executable, str(CHECKER), *map(str, args)],
                          capture_output=True, text=True, check=False)


def test_actual_test_environment_matches_complete_lock():
    checked = _run()
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "Dependency lock matched" in checked.stdout


def test_changed_installed_version_is_rejected(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"Starlette": "9999.0", "pip": "26.2.1", "salon-api": "0.1.0"}))
    checked = _run("--inventory", inventory)
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "starlette" in checked.stdout.lower() and "9999.0" in checked.stdout


def test_unlisted_dependency_is_rejected(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"unlisted-runtime-package": "1.0"}))
    checked = _run("--inventory", inventory)
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "unlisted-runtime-package" in checked.stdout


@pytest.mark.parametrize("pins", ["Starlette>=1.7.0", """Starlette==1.7.0
starlette==1.8.0"""])
def test_nonexact_or_conflicting_pin_is_rejected(tmp_path, pins):
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(pins)
    checked = _run("--constraints", constraints)
    assert checked.returncode == 2, checked.stdout + checked.stderr
    assert "Invalid exact pin" in checked.stdout



@pytest.mark.parametrize("position", [0, -1])
@pytest.mark.parametrize("name", ["starlette", "Starlette"])
def test_duplicate_installed_distribution_is_rejected(tmp_path, position, name):
    metadata_dir = tmp_path / "shadow_starlette-1.8.0.dist-info"
    metadata_dir.mkdir()
    (metadata_dir / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.8.0\n")
    script = (
        "import runpy,sys;"
        f"sys.path.insert({position}, {str(tmp_path)!r});"
        f"runpy.run_path({str(CHECKER)!r}, run_name='__main__')"
    )
    checked = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, check=False)
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "starlette" in checked.stdout.lower()
