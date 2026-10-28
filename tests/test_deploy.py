"""Deployment files: the engine port stays on Tailscale, and .env stays private."""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

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
# BROKEN file exists (a commit that does not start), a push fails while PUSH_FAILS does,
# and a prune and `system df` fail while DOCKER_FAILS does.
echo "$*" >>"$DOCKER_LOG"
if [[ ( "$2" == prune || "$*" == "system df"* ) && -f DOCKER_FAILS ]]; then
  exit 1
fi
if [[ "$*" == "system df"* ]]; then
  printf 'Images|1.37GB\\nContainers|0B\\nLocal Volumes|0B\\nBuild Cache|2.5GB\\n'
fi
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


PRUNES = ["builder prune -af --filter until=72h", "image prune -af --filter until=72h"]


def test_an_update_prunes_what_docker_kept_for_3_days_and_says_what_is_left(server: Path) -> None:
    """#379: the build cache and the unused images, all tags, after the new engine started and
    its image went to ECR; the notice ends with what Docker keeps and the disk's free space."""
    repository = "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/krx-quant"
    (server / ".env").write_text(f"KRX_IMAGE_REPOSITORY={repository}\n")
    push(server, "Markov reads the IV")

    result = update(server)

    assert result.returncode == 0, result.stderr
    calls = docker_calls(server)
    builder, image = (calls.index(c) for c in PRUNES)
    notice = next(i for i, c in enumerate(calls) if " notify " in c)
    assert calls.index("compose up -d --build") < calls.index(f"push -q {repository}:latest") < builder
    assert builder < image < calls.index("system df --format {{.Type}}|{{.Size}}") < notice
    assert "docker images 1.37GB, build cache 2.5GB; disk free " in result.stdout
    last = (server.parent / "docker.log").read_text().rstrip("\n").splitlines()[-1]
    assert re.fullmatch(r"디스크 여유 \S+ / \S+ · Docker 이미지 1\.37GB, 빌드 캐시 2\.5GB", last), last


def test_a_prune_that_fails_leaves_the_update_done(server: Path) -> None:
    (server / "DOCKER_FAILS").write_text("x")
    new = push(server, "Granger learns")

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert (server / ".auto-update-deployed").read_text().strip() == new
    assert "could not prune the build cache" in result.stderr
    assert "could not prune the images" in result.stderr
    log = (server.parent / "docker.log").read_text()
    assert "자동 업데이트 오류" not in log
    assert "Docker 이미지 ?, 빌드 캐시 ?" in log, "what could not be read is said so"


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
    assert not any("prune" in c for c in docker_calls(server)), "a rollback prunes nothing (#379)"
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


# Commands kernel-recovery.sh may call, faked: each records its arguments. docker is here so a
# call to it would show.
RECORDED = ("sysctl", "systemctl", "systemd-tmpfiles", "journalctl", "docker")


def kernel_recovery(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """deploy/kernel-recovery.sh with its files under tmp_path/root and the commands it calls."""
    bin_dir, calls = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir(exist_ok=True)
    for name in RECORDED:
        (bin_dir / name).write_text(f'#!/bin/sh\necho "{name} $*" >>"{calls}"\n')
        (bin_dir / name).chmod(0o755)
    before = calls.read_text().splitlines() if calls.exists() else []
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "KRX_SYSROOT": str(tmp_path / "root")}
    done = subprocess.run([ROOT / "deploy" / "kernel-recovery.sh"], env=env, capture_output=True, text=True)
    return done, calls.read_text().splitlines()[len(before) :]


def settings(path: Path) -> dict[str, str]:
    lines = [line for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]
    return dict(line.replace(" ", "").split("=", 1) for line in lines if "=" in line)


def test_kernel_recovery_sets_the_self_reboot_without_hung_task_panic_and_a_journal_on_disk(
    tmp_path: Path,
) -> None:
    done, calls = kernel_recovery(tmp_path)

    assert done.returncode == 0, done.stderr
    conf = tmp_path / "root" / "etc" / "sysctl.d" / "91-krx-quant-recovery.conf"
    assert settings(conf) == {
        "kernel.panic": "10",
        "kernel.panic_on_oops": "1",
        "kernel.softlockup_panic": "1",
    }
    journald = tmp_path / "root" / "etc" / "systemd" / "journald.conf.d" / "90-krx-quant.conf"
    assert settings(journald) == {"Storage": "persistent", "SystemMaxUse": "200M"}
    assert (tmp_path / "root" / "var" / "log" / "journal").is_dir()
    assert calls == [
        f"sysctl -q -p {conf}",
        "systemd-tmpfiles --create --prefix /var/log/journal",
        "systemctl restart systemd-journald",
        "journalctl --flush",
    ]


def test_kernel_recovery_run_again_applies_the_settings_and_restarts_nothing(tmp_path: Path) -> None:
    kernel_recovery(tmp_path)

    done, calls = kernel_recovery(tmp_path)

    assert done.returncode == 0, done.stderr
    assert calls == [f"sysctl -q -p {tmp_path / 'root' / 'etc' / 'sysctl.d' / '91-krx-quant-recovery.conf'}"]
    assert "journal: kept on disk already" in done.stdout


def test_setup_server_applies_the_kernel_recovery_and_leaves_a_running_engine_and_docker_alone() -> None:
    script = (ROOT / "deploy" / "setup-server.sh").read_text()
    assert '"$repo/deploy/kernel-recovery.sh"' in script
    # The packages are installed only when missing (an upgrade of docker.io restarts Docker), and a
    # running engine is not built again (its image would differ from the 06:00 update's).
    assert "dpkg-query" in script and 'apt-get install -y -q "${missing[@]}"' in script
    start = script.split('echo "== start"', 1)[1].split('echo "== daily update"', 1)[0]
    assert "docker compose ps -q --status running engine" in start
    assert "systemctl restart docker" not in script.split('echo "== Docker starts after Tailscale"', 1)[0]


# A made-up account and instance: the script must not print them.
ACCOUNT = "000000000000"
INSTANCE_ARN = f"arn:aws:lightsail:ap-southeast-2:{ACCOUNT}:Instance/00000000-0000-4000-8000-000000000000"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/krx-quant-watchdog"
# aws and gh as deploy/create-watchdog-role.sh calls them: IAM keeps what was made in STATE, and
# every call, with the documents it carried, goes to LOG as one JSON line.
FAKE_AWS_AND_GH = f"""#!/usr/bin/env python3
import json, os, sys
state, log = os.environ["STATE"], os.environ["LOG"]
args = sys.argv[1:]
tool = os.path.basename(sys.argv[0])
stdin = sys.stdin.read() if tool == "gh" and args[:2] == ["secret", "set"] else None
with open(log, "a") as f:
    f.write(json.dumps({{"tool": tool, "args": args, "stdin": stdin}}) + "\\n")
made = lambda name: os.path.exists(os.path.join(state, name))
make = lambda name: open(os.path.join(state, name), "w").close()
command = " ".join(args[:2])
if tool == "gh":
    sys.exit(0)
if command == "configure get":
    print("ap-southeast-2")
elif command == "sts get-caller-identity":
    print("{ACCOUNT}")
elif command == "lightsail get-instance":
    print("{INSTANCE_ARN}")
elif command == "iam get-open-id-connect-provider":
    if not made("provider"):
        sys.exit(254)
    print("sts.amazonaws.com")
elif command == "iam create-open-id-connect-provider":
    make("provider")
elif command == "iam get-role":
    if not made("role"):
        sys.exit(254)
    print("{ROLE_ARN}")
elif command == "iam create-role":
    make("role")
"""


def create_watchdog_role(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    bin_dir, state, log = tmp_path / "bin", tmp_path / "state", tmp_path / "calls.jsonl"
    for folder in (bin_dir, state):
        folder.mkdir(exist_ok=True)
    for tool in ("aws", "gh"):
        (bin_dir / tool).write_text(FAKE_AWS_AND_GH)
        (bin_dir / tool).chmod(0o755)
    before = len(log.read_text().splitlines()) if log.exists() else 0
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STATE": str(state), "LOG": str(log), "HOME": str(tmp_path)}
    script = ROOT / "deploy" / "create-watchdog-role.sh"
    done = subprocess.run([script], env=env, capture_output=True, text=True)
    return done, [json.loads(line) for line in log.read_text().splitlines()[before:]]


def document(calls: list[dict[str, Any]], command: str, option: str) -> Any:
    [args] = [c["args"] for c in calls if " ".join(c["args"][:2]) == command]
    return json.loads(args[args.index(option) + 1])


def test_the_watchdog_role_trusts_only_main_of_this_repository_and_may_reboot_only_the_server(
    tmp_path: Path,
) -> None:
    done, calls = create_watchdog_role(tmp_path)

    assert done.returncode == 0, done.stderr
    trust = document(calls, "iam create-role", "--assume-role-policy-document")
    [statement] = trust["Statement"]
    issuer = "token.actions.githubusercontent.com"
    assert statement["Principal"] == {"Federated": f"arn:aws:iam::{ACCOUNT}:oidc-provider/{issuer}"}
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Condition"] == {
        "StringEquals": {
            f"{issuer}:aud": "sts.amazonaws.com",
            f"{issuer}:sub": "repo:codeyoma/krx-quant:ref:refs/heads/main",
        }
    }
    policy = document(calls, "iam put-role-policy", "--policy-document")
    allowed = {(action, s["Resource"]) for s in policy["Statement"] for action in s["Action"]}
    assert allowed == {
        ("lightsail:GetInstanceState", "*"),
        ("lightsail:GetInstanceMetricData", "*"),
        ("lightsail:GetOperationsForResource", "*"),
        ("lightsail:RebootInstance", INSTANCE_ARN),
    }
    assert all(s["Effect"] == "Allow" for s in policy["Statement"])
    [reads] = [s for s in policy["Statement"] if s["Resource"] == "*"]
    assert reads["Condition"] == {"StringEquals": {"aws:RequestedRegion": "ap-southeast-2"}}
    [secret] = [c for c in calls if c["tool"] == "gh" and c["args"][:2] == ["secret", "set"]]
    assert secret["args"] == ["secret", "set", "AWS_WATCHDOG_ROLE_ARN", "--repo", "codeyoma/krx-quant"]
    assert secret["stdin"] == ROLE_ARN
    assert ACCOUNT not in done.stdout + done.stderr, "the ARNs hold the account ID"


def test_the_watchdog_role_script_run_again_keeps_the_provider_and_role_and_resets_their_documents(
    tmp_path: Path,
) -> None:
    create_watchdog_role(tmp_path)

    done, calls = create_watchdog_role(tmp_path)

    assert done.returncode == 0, done.stderr
    commands = [" ".join(c["args"][:2]) for c in calls if c["tool"] == "aws"]
    assert "iam create-open-id-connect-provider" not in commands and "iam create-role" not in commands
    trust = document(calls, "iam update-assume-role-policy", "--policy-document")
    assert trust["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"] == (
        "repo:codeyoma/krx-quant:ref:refs/heads/main"
    )
    assert "iam put-role-policy" in commands
