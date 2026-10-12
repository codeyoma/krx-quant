"""Deployment files: the engine port stays on Tailscale, and .env stays private."""

from __future__ import annotations

import re
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


def spot_start_script(path: Path) -> str:
    """The Spot instance's start script in deploy/training.yaml, as CloudFormation fills it in."""
    text = path.read_text().split("Fn::Base64: !Sub |\n", 1)[1].split("\n\n", 1)[0]
    script = "\n".join(line[12:] for line in text.splitlines()) + "\n"
    values = {
        "MaxMinutes": "300",
        "Bucket": "a-bucket",
        "Prefix": "engine",
        "Repository.RepositoryUri": "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/krx-quant",
        "AWS::Region": "ap-southeast-2",
    }
    script = re.sub(r"\$\{(?!!)([A-Za-z:.]+)\}", lambda m: values[m.group(1)], script)
    return script.replace("${!", "${")


def test_the_spot_instance_s_start_script_parses(tmp_path: Path) -> None:
    script = tmp_path / "start.sh"
    script.write_text(spot_start_script(ROOT / "deploy" / "training.yaml"))

    subprocess.run(["bash", "-n", script], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", script], check=True)
    text = script.read_text()
    assert not re.search(r"\$\{[A-Za-z]+(::|\.)", text), "every CloudFormation value is filled in"
    assert "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/krx-quant:latest" in text
    assert "trap 'shutdown -h now' EXIT" in text, "it terminates itself however the training ends"
    assert "train --from-store" in text


FAKE_DOCKER = """#!/usr/bin/env bash
# Stands in for docker: logs each call; the engine answers unless the checkout's
# BROKEN file exists (a commit that does not start), and a push fails while PUSH_FAILS does.
echo "$*" >>"$DOCKER_LOG"
# The commit a compose build is given (KRX_COMMIT), one per build.
if [[ "$*" == "compose up -d --build" ]]; then
  echo "${KRX_COMMIT:-}" >>"$DOCKER_LOG.commits"
fi
if [[ "$1" == push && -f PUSH_FAILS ]]; then
  exit 1
fi
if [[ "$*" == *"exec -T engine"* ]]; then
  [[ ! -f BROKEN ]]
fi
"""


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


@pytest.fixture
def server(tmp_path: Path) -> Path:
    """A checkout of a bare origin, with the deploy scripts and a fake docker on PATH."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    for name in ("user.email", "user.name"):
        git(work, "config", name, "test")
    shutil.copytree(ROOT / "deploy", work / "deploy")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "first")
    git(work, "push", "-q", "origin", "main")
    checkout = tmp_path / "server"
    git(tmp_path, "clone", "-q", str(origin), str(checkout))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    return checkout


def push(server: Path, message: str, broken: bool = False) -> str:
    """A new commit on origin's main, made from another checkout."""
    work = server.parent / "work"
    marker = work / "BROKEN"
    if broken:
        marker.write_text("x")
    elif marker.exists():
        marker.unlink()
    (work / "change.txt").write_text(message)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message)
    git(work, "push", "-q", "origin", "main")
    return git(work, "rev-parse", "HEAD")


def update(server: Path, now: str = "6 0600") -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{server.parent / 'bin'}:/usr/bin:/bin",
        "HOME": str(server.parent),
        "DOCKER_LOG": str(server.parent / "docker.log"),
        "KRX_UPDATE_NOW": now,
        "KRX_UPDATE_TRIES": "2",
        "KRX_UPDATE_PAUSE": "0",
    }
    return subprocess.run([server / "deploy" / "auto-update.sh"], env=env, capture_output=True, text=True)


def docker_calls(server: Path) -> list[str]:
    log = server.parent / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def test_the_server_updates_to_a_new_main_and_says_so(server: Path) -> None:
    assert update(server).returncode == 0
    assert docker_calls(server) == [], "nothing new: nothing restarted"

    new = push(server, "Granger learns")
    result = update(server)

    assert result.returncode == 0, result.stderr
    assert git(server, "rev-parse", "HEAD") == new
    calls = docker_calls(server)
    assert calls[0] == "compose up -d --build"
    notice = (server.parent / "docker.log").read_text().split(" notify ", 1)[1]
    assert notice.startswith("자동 업데이트") and new[:7] in notice and "- Granger learns" in notice


def built_commits(server: Path) -> list[str]:
    log = server.parent / "docker.log.commits"
    return log.read_text().splitlines() if log.exists() else []


def test_each_build_is_given_the_commit_it_runs(server: Path) -> None:
    before = git(server, "rev-parse", "HEAD")
    new = push(server, "Granger learns")
    assert update(server).returncode == 0
    bad = push(server, "a bug", broken=True)
    assert update(server).returncode != 0

    # The new main, then the bad one that did not start and the one it went back to.
    assert built_commits(server) == [new, bad, new]
    assert before != new


def test_with_the_training_on_spot_an_update_pushes_the_image_and_forgets_the_login(server: Path) -> None:
    repository = "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/krx-quant"
    (server / ".env").write_text(f"KRX_IMAGE_REPOSITORY={repository}\n")
    push(server, "Markov reads the IV")

    result = update(server)

    assert result.returncode == 0, result.stderr
    calls = docker_calls(server)
    pushed = calls.index(f"push -q {repository}:latest")
    login = calls.index("compose run --rm --no-deps -T engine registry-password")
    assert calls.index("compose up -d --build") < login < pushed, "the new image, once it started"
    assert calls[pushed - 1] == f"tag krx-quant:latest {repository}:latest"
    assert calls[pushed + 1] == f"logout {repository.split('/')[0]}", "the registry password is not kept"


def test_an_image_that_could_not_be_pushed_is_pushed_on_the_next_run(server: Path) -> None:
    repository = "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/krx-quant"
    (server / ".env").write_text(f"KRX_IMAGE_REPOSITORY={repository}\n")
    (server / "PUSH_FAILS").write_text("x")
    push(server, "Markov reads the IV")

    assert update(server).returncode == 0
    assert "이미지를 올리지 못했습니다" in (server.parent / "docker.log").read_text()

    (server / "PUSH_FAILS").unlink()
    (server.parent / "docker.log").unlink()
    assert update(server).returncode == 0, "nothing new on main"

    calls = docker_calls(server)
    assert f"push -q {repository}:latest" in calls
    assert "compose up -d --build" not in calls
    assert "이미지를 올렸습니다" in (server.parent / "docker.log").read_text()


def test_without_an_env_file_there_is_no_image_to_push(server: Path) -> None:
    env = {"PATH": f"{server.parent / 'bin'}:/usr/bin:/bin", "DOCKER_LOG": str(server.parent / "docker.log")}
    done = subprocess.run([server / "deploy" / "push-image.sh"], env=env, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert "nothing to push" in done.stdout
    assert docker_calls(server) == []


def test_a_main_that_does_not_start_is_rolled_back_and_not_tried_again(server: Path) -> None:
    before = git(server, "rev-parse", "HEAD")
    bad = push(server, "a bug", broken=True)

    result = update(server)

    assert result.returncode != 0
    assert git(server, "rev-parse", "HEAD") == before
    assert (server / ".auto-update-failed").read_text().strip() == bad
    assert any("자동 업데이트 실패" in c and bad[:7] in c for c in docker_calls(server))
    (server.parent / "docker.log").unlink()

    assert update(server).returncode == 0
    assert docker_calls(server) == [], "the failed main is skipped until a newer one"
    fixed = push(server, "the fix")
    assert update(server).returncode == 0
    assert git(server, "rev-parse", "HEAD") == fixed


@pytest.mark.parametrize("now", ["1 0750", "3 1200", "5 2009"])
def test_the_server_never_updates_while_the_market_or_nxt_trades(server: Path, now: str) -> None:
    push(server, "anything")

    result = update(server, now)

    assert result.returncode != 0 and "market hours" in result.stderr
    assert docker_calls(server) == []


def test_weekends_and_nights_are_fine(server: Path) -> None:
    for now in ("6 1200", "7 0900", "1 0600", "5 2100"):
        push(server, f"at {now}")
        assert update(server, now).returncode == 0, now


def test_an_update_that_cannot_fetch_is_reported_and_changes_nothing(server: Path) -> None:
    before = git(server, "rev-parse", "HEAD")
    shutil.rmtree(server.parent / "origin.git")  # GitHub unreachable

    result = update(server)

    assert result.returncode != 0
    assert git(server, "rev-parse", "HEAD") == before
    assert "자동 업데이트 오류" in (server.parent / "docker.log").read_text()
    assert "compose up" not in (server.parent / "docker.log").read_text()


def test_a_long_backlog_of_commits_is_deployed_with_the_latest_ten_listed(server: Path) -> None:
    for i in range(12):
        newest = push(server, f"change {i}")

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert git(server, "rev-parse", "HEAD") == newest
    notice = (server.parent / "docker.log").read_text().split(" notify ", 1)[1]
    assert "- change 11" in notice and "- change 2" in notice and "- change 1\n" not in notice


def test_a_run_cut_short_after_moving_the_checkout_is_done_again(server: Path) -> None:
    assert update(server).returncode == 0
    new = push(server, "half deployed")
    git(server, "pull", "-q", "--ff-only")  # the checkout moved; the engine was never restarted

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert "compose up -d --build" in docker_calls(server)
    assert (server / ".auto-update-deployed").read_text().strip() == new
