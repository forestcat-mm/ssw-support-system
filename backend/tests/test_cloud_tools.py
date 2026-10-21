"""クラウドのセッションの道具（tools/cloud/。docs/cloud-session.md）と、手元の設定がずれていないか。

環境の設定画面に貼る setup script（tools/cloud/setup.sh）の版を、Docker のイメージと pyproject の
版と照らす（版を変えたら setup.sh も変え、設定画面に貼り直す）。
"""

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
CLOUD = REPO / "tools" / "cloud"


def _setup_value(name: str) -> str:
    match = re.search(rf"^{name}=(\S+)", (CLOUD / "setup.sh").read_text("utf-8"), re.M)
    assert match, name
    return match.group(1)


def test_setup_installs_the_same_uv_as_the_images() -> None:
    for dockerfile in ("Dockerfile", "Dockerfile.e2e"):
        text = (BACKEND / dockerfile).read_text("utf-8")
        assert f"ghcr.io/astral-sh/uv:{_setup_value('UV_VERSION')} " in text, dockerfile


def test_setup_installs_the_chromium_of_the_pinned_playwright() -> None:
    version = _setup_value("PLAYWRIGHT_VERSION")
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text("utf-8"))
    assert f"playwright=={version}" in pyproject["dependency-groups"]["e2e"]
    assert f"playwright/python:v{version}-" in (BACKEND / "Dockerfile.e2e").read_text("utf-8")


def test_python_is_the_same_minor_version_as_the_web_image() -> None:
    version = _setup_value("PYTHON_VERSION")
    assert (BACKEND / ".python-version").read_text("utf-8").strip() == version
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text("utf-8"))
    assert pyproject["project"]["requires-python"] == f"=={version}.*"
    assert f"FROM python:{version}-" in (BACKEND / "Dockerfile").read_text("utf-8")


def test_env_example_reads_the_same_in_bash() -> None:
    """RUNNER=host は .env を bash で読む。空白・#・引用符・JSON を含む値は '…' で囲んである。"""
    example = REPO / ".env.example"
    written = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", example.read_text("utf-8"), re.M))
    result = subprocess.run(
        ["bash", "-c", 'set -a; . "$1"; set +a; env -0', "_", str(example)],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.stderr == ""
    read = dict(item.split("=", 1) for item in result.stdout.split("\0") if "=" in item)
    for key, value in written.items():
        if re.search(r"[\s#\"'{]", value):
            assert value.startswith("'") and value.endswith("'"), key
            value = value[1:-1]
        assert read.get(key) == value, key


def test_session_start_hook_runs_the_cloud_script_and_does_nothing_locally() -> None:
    settings = json.loads((REPO / ".claude" / "settings.json").read_text("utf-8"))
    commands = [
        hook["command"] for entry in settings["hooks"]["SessionStart"] for hook in entry["hooks"]
    ]
    assert commands == ['bash "$CLAUDE_PROJECT_DIR"/tools/cloud/session_start.sh']
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_REMOTE"}
    result = subprocess.run(
        ["bash", str(CLOUD / "session_start.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={**env, "CLAUDE_PROJECT_DIR": str(REPO)},
        timeout=30,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
