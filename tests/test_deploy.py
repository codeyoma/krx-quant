"""Deployment files: the engine port stays on Tailscale, and .env stays private."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "deploy").glob("*.sh"))


def published_ports(compose: str) -> list[str]:
    """Every item of every ``ports:`` list in a compose file."""
    ports: list[str] = []
    lines = compose.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "ports:":
            continue
        indent = len(line) - len(line.lstrip())
        for item in lines[i + 1 :]:
            if item.strip() and len(item) - len(item.lstrip()) <= indent:
                break
            if item.strip().startswith("- "):
                ports.append(item.strip()[2:].strip("\"'"))
    return ports


def test_the_engine_port_is_published_on_the_tailscale_address_only() -> None:
    compose = (ROOT / "compose.yaml").read_text()

    ports = published_ports(compose)

    assert ports, "the engine port is published"
    assert all(p.startswith("${KRX_BIND_ADDRESS:?") for p in ports), ports
    assert "network_mode" not in compose, "host networking would expose every listening port"


def test_secure_env_makes_the_env_file_readable_by_its_owner_only(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("KIS_APP_KEY=x\n")
    env.chmod(0o644)

    subprocess.run([ROOT / "deploy" / "secure-env.sh", env], check=True, capture_output=True)

    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_secure_env_refuses_a_missing_env_file(tmp_path: Path) -> None:
    result = subprocess.run(
        [ROOT / "deploy" / "secure-env.sh", tmp_path / ".env"], capture_output=True, text=True
    )

    assert result.returncode != 0
    assert ".env.example" in result.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_deploy_scripts_parse(script: Path) -> None:
    subprocess.run(["bash", "-n", script], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", script], check=True)
