"""Deployment files: the engine port stays on Tailscale, and .env stays private."""

from __future__ import annotations

import json
import re
import shutil
import socket
import stat
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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


def services(compose: str) -> dict[str, str]:
    """Each service of a compose file and its block's text."""
    found: dict[str, str] = {}
    lines = compose.splitlines()
    start = lines.index("services:")
    name: str | None = None
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break  # the next top-level key
        if line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":"):
            if name is not None:
                found[name] = "\n".join(block)
            name, block = line.strip()[:-1], []
        elif name is not None:
            block.append(line)
    if name is not None:
        found[name] = "\n".join(block)
    return found


def test_the_quote_stream_is_not_published_only_the_engine_api_is() -> None:
    """#394: the collector streams the quotes on 8081 inside its container; compose publishes the
    Engine API's 8080 alone, so the Tailscale address never reaches the quotes. The trader listens
    on nothing and reads them over the compose network, and the cost map from the Engine API there
    (#395)."""
    compose = (ROOT / "compose.yaml").read_text()
    found = services(compose)

    assert set(found) == {"engine", "trader"}
    assert [p.rsplit(":", 1)[-1] for p in published_ports(found["engine"])] == ["8080"], "the Engine API"
    assert 'KRX_API_PORT: "8080"' in found["engine"] and 'KRX_QUOTES_PORT: "8081"' in found["engine"]
    assert "ports:" not in found["trader"] and "expose:" not in compose
    assert "KRX_TRADER_QUOTES_URL: ws://engine:8081/v1/quotes" in found["trader"]
    assert "KRX_TRADER_ENGINE_URL: http://engine:8080" in found["trader"]
    assert "KRX_TRADER_ENGINE_URL: http://engine:8080" in found["engine"], (
        "the nightly policy re-fit (#399) asks its own container's Engine API, by the service's name"
    )
    assert 'command: ["trade"]' in found["trader"] and "image: krx-quant:latest" in found["trader"]
    assert "network_mode" not in compose


def test_the_image_keeps_blas_and_openmp_on_the_thread_that_calls_them() -> None:
    """#589: every process of the image (the engine, the training and jobs it starts, the trader,
    the commands) has these before NumPy and LightGBM start; compose does not set them otherwise."""
    lines = [line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("ENV ")]
    threads = dict(re.findall(r"\b((?:OPENBLAS|OMP|MKL)_NUM_THREADS)=(\S+)", "\n".join(lines)))

    assert threads == {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    assert "NUM_THREADS" not in (ROOT / "compose.yaml").read_text()


def test_the_smoke_run_keeps_the_trader_off_the_real_env_and_state() -> None:
    smoke = services((ROOT / "deploy" / "compose.smoke.yaml").read_text())

    assert "env_file: !override deploy/smoke.env" in smoke["trader"]
    assert "./state" not in smoke["trader"] and "smoke-state:/state" in smoke["trader"]


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


FAKE_DOCKER = """#!/usr/bin/env bash
# Stands in for docker: logs each call; the engine answers unless the checkout's
# BROKEN file exists (a commit that does not start), a prune and `system df` fail while
# DOCKER_FAILS does, and a notice while NOTIFY_FAILS does. The run that waits for the engine is
# killed, as a reboot would, while INTERRUPT does. `images` lists the checkout's IMAGES, a tag a
# line. `system df` says the build cache is the checkout's CACHE (2.5GB without one): a prune with
# `until` leaves it, as Docker leaves the cache it made before it last started (#568); one without
# a filter empties it.
#
# Images are commits: a build makes krx-quant:latest of KRX_COMMIT, `compose up` runs the engine
# on krx-quant:latest, the engine's image ID is sha256:<its commit>, and `tag` copies a commit to
# a tag, in $DOCKER_LOG.state. An engine runs (`compose ps`) unless NO_ENGINE exists.
echo "$*" >>"$DOCKER_LOG"
state="$DOCKER_LOG.state"
mkdir -p "$state"
commit_of() {
  if [[ "$1" == sha256:* ]]; then echo "${1#sha256:}"; else cat "$state/${1//\\//_}" 2>/dev/null; fi
}
if [[ ( "$2" == prune || "$*" == "system df"* ) && -f DOCKER_FAILS ]]; then
  echo "Error response from daemon: a prune operation is already running" >&2
  exit 1
fi
# A notice does not go while NOTIFY_FAILS does (Telegram does not answer).
if [[ "$*" == *" engine notify "* && -f NOTIFY_FAILS ]]; then
  exit 1
fi
if [[ "$*" == "system df"* ]]; then
  cache=$(cat CACHE 2>/dev/null || echo 2.5GB)
  printf 'Images|1.37GB\\nContainers|0B\\nLocal Volumes|0B\\nBuild Cache|%s\\n' "$cache"
fi
if [[ "$*" == "builder prune -af" ]]; then
  echo 0B >CACHE
fi
if [[ "$*" == "images --format {{.Repository}}:{{.Tag}}" && -f IMAGES ]]; then
  cat IMAGES
fi
if [[ "$*" == "info --format {{.ServerVersion}} {{.Driver}}" ]]; then
  echo 29.1.3 overlayfs
fi
# The commit a compose build is given (KRX_COMMIT), one per build.
if [[ "$*" == "compose up -d --build" ]]; then
  echo "${KRX_COMMIT:-}" >>"$DOCKER_LOG.commits"
  echo "${KRX_COMMIT:-}" >"$state/krx-quant:latest"
fi
if [[ "$*" == "compose up -d" || "$*" == "compose up -d --build" ]]; then
  cp "$state/krx-quant:latest" "$state/running"
fi
if [[ "$*" == "compose ps -q engine" && -s "$state/running" && ! -f NO_ENGINE ]]; then
  echo 5f2c0ffee
fi
if [[ "$*" == "inspect --format {{.Image}} 5f2c0ffee" ]]; then
  echo "sha256:$(cat "$state/running")"
fi
if [[ "$1 $2" == "image inspect" ]]; then
  made=$(commit_of "${@: -1}")
  [[ -n "$made" ]] || exit 1
  printf 'PATH=/usr/local/bin\\nKRX_COMMIT=%s\\n' "$made"
fi
if [[ "$1" == tag ]]; then
  made=$(commit_of "$2")
  [[ -n "$made" ]] || exit 1
  echo "$made" >"$state/${3//\\//_}"
fi
if [[ "$*" == *"exec -T engine"* ]]; then
  if [[ -f INTERRUPT ]]; then
    kill -KILL "$PPID"
  fi
  [[ ! -f BROKEN ]]
fi
"""


FAKE_SUDO = """#!/usr/bin/env bash
# Stands in for sudo: logs each call and runs nothing, so a test never changes this machine's
# kernel settings; it fails like a sudo that wants a password while the checkout's NO_SUDO exists.
echo "$*" >>"$SUDO_LOG"
if [[ -f NO_SUDO ]]; then
  echo "sudo: a password is required" >&2
  exit 1
fi
"""


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


REPO = "codeyoma/krx-quant"
RUNS_PATH = f"/repos/{REPO}/actions/workflows/ci.yml/runs"


def ci_run(
    sha: str,
    status: str = "completed",
    conclusion: str | None = "success",
    *,
    event: str = "push",
    number: int = 1,
) -> dict[str, Any]:
    """One run of the ci workflow as GitHub lists it (the fields probed on 2026-11-05, #541)."""
    return {
        "id": 42000000000 + number,
        "name": "ci",
        "run_number": number,
        "run_attempt": 1,
        "event": event,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "head_branch": "main",
        "head_sha": sha,
        "html_url": f"https://github.com/{REPO}/actions/runs/{42000000000 + number}",
        "path": ".github/workflows/ci.yml",
    }


@dataclass
class FakeActions:
    """GitHub's list of the ci workflow's runs of a commit (``GET {RUNS_PATH}?head_sha=…``), as
    deploy/auto-update.sh asks it, on a local port. A commit not in ``runs`` has one push run that
    passed. ``stray`` runs are listed for every commit, as an answer that ignored the filters
    would. ``fail`` answers that HTTP status instead, as GitHub out of requests (403) or down
    (503) does; ``body`` answers 200 with that body."""

    api: str = ""
    runs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stray: list[dict[str, Any]] = field(default_factory=list)
    fail: int | None = None
    body: bytes | None = None
    asked: list[dict[str, str]] = field(default_factory=list)  # each request's query, in order

    def asked_for(self) -> list[str]:
        return [query.get("head_sha", "") for query in self.asked]


@pytest.fixture
def github() -> Iterator[FakeActions]:
    fake = FakeActions()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            query = dict(parse_qsl(url.query))
            fake.asked.append(query)
            if url.path != RUNS_PATH:
                self.answer(404, b'{"message": "Not Found"}')
            elif fake.fail is not None:
                remaining = "0" if fake.fail == 403 else None
                self.answer(fake.fail, b'{"message": "API rate limit exceeded"}', remaining)
            elif fake.body is not None:
                self.answer(200, fake.body)
            else:
                sha = query.get("head_sha", "")
                listed = fake.runs.get(sha, [ci_run(sha)]) + fake.stray
                self.answer(200, json.dumps({"total_count": len(listed), "workflow_runs": listed}).encode())

        def answer(self, status: int, body: bytes, remaining: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if remaining is not None:
                self.send_header("x-ratelimit-remaining", remaining)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    # Listening once made: a request sent before serve_forever runs waits in the backlog.
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    fake.api = f"http://127.0.0.1:{http.server_port}/repos/{REPO}"
    yield fake
    http.shutdown()
    http.server_close()
    thread.join()


@pytest.fixture
def server(tmp_path: Path, github: FakeActions) -> Path:
    """A checkout of a bare origin, with the deploy scripts and a fake docker on PATH, and a fake
    GitHub API whose ci passed on every commit unless a test says otherwise."""
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
    (bin_dir / "sudo").write_text(FAKE_SUDO)
    (bin_dir / "sudo").chmod(0o755)
    images = tmp_path / "docker.log.state"  # the fake docker's images: the first commit runs
    images.mkdir()
    for name in ("krx-quant:latest", "running"):
        (images / name).write_text(git(checkout, "rev-parse", "HEAD") + "\n")
    (tmp_path / "github-api").write_text(github.api)
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


def move_fincept(server: Path, pointer: str) -> str:
    """A commit on origin's main that only moves the fincept pointer, as the fincept-pointer
    workflow pushes it (#186)."""
    work = server.parent / "work"
    (work / "fincept").mkdir(exist_ok=True)  # an uninitialised submodule: git leaves it alone
    git(work, "update-index", "--add", "--cacheinfo", f"160000,{pointer},fincept")
    git(work, "commit", "-q", "-m", f"fincept: follow custom to {pointer[:7]}")
    git(work, "push", "-q", "origin", "main")
    return git(work, "rev-parse", "HEAD")


def update(server: Path, now: str = "6 0600", api: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{server.parent / 'bin'}:/usr/bin:/bin",
        "HOME": str(server.parent),
        "DOCKER_LOG": str(server.parent / "docker.log"),
        "SUDO_LOG": str(server.parent / "sudo.log"),
        "KRX_UPDATE_NOW": now,
        "KRX_UPDATE_TRIES": "2",
        "KRX_UPDATE_PAUSE": "0",
        "KRX_UPDATE_GITHUB_API": api if api is not None else (server.parent / "github-api").read_text(),
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
    assert calls.count("compose up -d --build") == 1
    notice = (server.parent / "docker.log").read_text().split(" notify ", 1)[1]
    assert notice.startswith("자동 업데이트") and new[:7] in notice and "- Granger learns" in notice


def sudo_calls(server: Path) -> list[str]:
    log = server.parent / "sudo.log"
    return log.read_text().splitlines() if log.exists() else []


def test_every_run_applies_the_kernel_settings_with_passwordless_sudo(server: Path) -> None:
    """#389: deploy/kernel-recovery.sh (#385) reaches the server by the 06:00 update, on a day
    with nothing new on main too."""
    applied = f"-n {server / 'deploy' / 'kernel-recovery.sh'}"

    assert update(server).returncode == 0
    assert sudo_calls(server) == [applied], "nothing new on main: applied all the same"

    push(server, "Granger learns")
    assert update(server).returncode == 0
    assert sudo_calls(server) == [applied, applied]


def test_without_passwordless_sudo_the_update_goes_on_and_says_so(server: Path) -> None:
    (server / "NO_SUDO").write_text("x")
    new = push(server, "Granger learns")

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert git(server, "rev-parse", "HEAD") == new
    assert "could not apply deploy/kernel-recovery.sh" in result.stderr
    assert "자동 업데이트 오류" not in (server.parent / "docker.log").read_text()


def test_in_market_hours_the_kernel_settings_wait_too(server: Path) -> None:
    assert update(server, now="3 1000").returncode != 0
    assert sudo_calls(server) == []


def built_commits(server: Path) -> list[str]:
    log = server.parent / "docker.log.commits"
    return log.read_text().splitlines() if log.exists() else []


def test_each_build_is_given_the_commit_it_runs(server: Path) -> None:
    """With no engine running to keep the image of, a rollback builds the commit before."""
    (server / "NO_ENGINE").write_text("x")
    before = git(server, "rev-parse", "HEAD")
    new = push(server, "Granger learns")
    assert update(server).returncode == 0
    bad = push(server, "a bug", broken=True)
    assert update(server).returncode != 0

    # The new main, then the bad one that did not start and the one it went back to.
    assert built_commits(server) == [new, bad, new]
    assert before != new
    assert not any(c.startswith("tag ") for c in docker_calls(server)), "no image to keep"


def running(server: Path) -> str:
    """The commit of the image the fake docker's engine runs."""
    return (server.parent / "docker.log.state" / "running").read_text().strip()


def test_an_update_tags_the_image_the_engine_runs_previous_before_it_builds(server: Path) -> None:
    """#568: the image a rollback starts again, once it is seen to be built from the deployed
    commit; the cleanup keeps it."""
    before = git(server, "rev-parse", "HEAD")
    push(server, "Granger learns")

    assert update(server).returncode == 0

    calls = docker_calls(server)
    image = calls.index("inspect --format {{.Image}} 5f2c0ffee")
    checked = calls.index(
        f"image inspect --format {{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}} sha256:{before}"
    )
    tagged = calls.index(f"tag sha256:{before} krx-quant:previous")
    assert (
        calls.index("compose ps -q engine") < image < checked < tagged < calls.index("compose up -d --build")
    )


def test_a_main_that_does_not_start_goes_back_to_the_image_the_engine_ran_without_a_build(
    server: Path,
) -> None:
    """#568: the rollback needs neither the build cache the cleanup removes nor PyPI."""
    before = git(server, "rev-parse", "HEAD")
    bad = push(server, "a bug", broken=True)

    result = update(server)

    assert result.returncode != 0
    assert git(server, "rev-parse", "HEAD") == before
    assert built_commits(server) == [bad], "only the bad main was built"
    assert running(server) == before
    calls = docker_calls(server)
    back = calls.index("tag krx-quant:previous krx-quant:latest")
    assert calls.index("compose up -d --build") < back < calls.index("compose up -d")
    assert any("자동 업데이트 실패" in c and f"{before[:7]}로 되돌렸습니다" in c for c in calls)


def test_a_run_cut_short_after_the_new_engine_started_still_goes_back_to_the_checked_image(
    server: Path,
) -> None:
    """#568 review: a reboot after the new engine started and before it was checked leaves that
    image running. The run that does the update again must not tag it krx-quant:previous: when
    the new main does not start, it goes back to the image the deployed commit was checked on."""
    good = push(server, "Granger learns")
    assert update(server).returncode == 0
    assert running(server) == good
    bad = push(server, "a bug", broken=True)
    (server / "INTERRUPT").write_text("x")

    cut = update(server)

    assert cut.returncode == -9, "killed while it waited for the new engine"
    assert deployed(server) == good and running(server) == bad, "the unchecked image runs"
    (server / "INTERRUPT").unlink()
    (server.parent / "docker.log").unlink()

    result = update(server)

    assert result.returncode != 0
    assert "the running image is not built from" in result.stdout
    calls = docker_calls(server)
    assert not any(c.endswith(" krx-quant:previous") and c.startswith("tag ") for c in calls), calls
    assert calls.index("compose up -d --build") < calls.index("tag krx-quant:previous krx-quant:latest")
    assert running(server) == good, "back on the image the deployed commit was checked on"
    assert git(server, "rev-parse", "HEAD") == good
    assert (server / ".auto-update-failed").read_text().strip() == bad
    assert any(f"{good[:7]}로 되돌렸습니다" in c for c in calls)


def test_a_previous_image_of_another_commit_is_not_gone_back_to(server: Path) -> None:
    """#568 review: a krx-quant:previous not built from the deployed commit (the run that tagged
    the checked image was cut short before it) is not trusted: the rollback builds that commit."""
    good = push(server, "Granger learns")
    assert update(server).returncode == 0
    images = server.parent / "docker.log.state"
    (images / "running").write_text("1" * 40 + "\n")  # started, never checked
    (images / "krx-quant:previous").write_text("0" * 40 + "\n")
    bad = push(server, "a bug", broken=True)

    result = update(server)

    assert result.returncode != 0
    assert built_commits(server)[-2:] == [bad, good], "the bad main, then the deployed commit built"
    assert running(server) == good
    assert "tag krx-quant:previous krx-quant:latest" not in docker_calls(server)


def test_an_update_keeps_the_used_latest_and_previous_images_and_a_day_of_build_cache(
    server: Path,
) -> None:
    """#379, #568: after the new engine started, every other tag is taken off and the images left
    without one go; the build cache no build used for a day goes. The notice says what Docker
    kept and the disk's free space, before and after, and Docker's version and storage driver."""
    (server / "IMAGES").write_text(
        "krx-quant:latest\nkrx-quant:previous\n<none>:<none>\nregistry.example/krx-quant:latest\nbusybox:1.37\n"
    )
    push(server, "Markov reads the IV")

    result = update(server)

    assert result.returncode == 0, result.stderr
    calls = docker_calls(server)
    started = max(i for i, c in enumerate(calls) if "exec -T engine" in c)
    untagged = [c for c in calls if c.startswith("rmi ")]
    assert untagged == ["rmi registry.example/krx-quant:latest", "rmi busybox:1.37"]
    image = calls.index("image prune -f")
    builder = calls.index("builder prune -af --filter until=24h")
    assert started < calls.index(untagged[0]) < image < builder
    assert "builder prune -af" not in calls, "2.5GB is under 3GB: the day's prune is enough"
    measured = [i for i, c in enumerate(calls) if c == "system df --format {{.Type}}|{{.Size}}"]
    assert measured[0] < calls.index(untagged[0]) and measured[-1] > builder
    assert "docker images 1.37GB -> 1.37GB, build cache 2.5GB -> 2.5GB; disk free " in result.stdout
    assert "(docker 29.1.3 overlayfs)" in result.stdout
    last = (server.parent / "docker.log").read_text().rstrip("\n").splitlines()[-1]
    assert re.fullmatch(
        r"디스크 여유 \S+ → \S+ / \S+ · Docker 이미지 1\.37GB → 1\.37GB, 빌드 캐시 2\.5GB → 2\.5GB"
        r" · Docker 29\.1\.3 overlayfs",
        last,
    ), last


@pytest.mark.parametrize(
    ("left", "swept"),
    [
        ("0B", False),
        ("898.5MB", False),
        ("2.5GB", False),
        ("3GB", False),
        ("3.01GB", True),
        ("22.27GB", True),
    ],
)
def test_a_build_cache_over_3gb_after_the_day_s_prune_is_removed_whole(
    server: Path, left: str, swept: bool
) -> None:
    """#568: Docker keeps the cache made before it last started past every prune with `until`; the
    server kept 21.5 GB so from 2026-10-28. More than 3 GB left, one build's cache being about
    1 GB, is that, and goes whole."""
    (server / "CACHE").write_text(left)
    push(server, "Granger learns")

    result = update(server)

    assert result.returncode == 0, result.stderr
    calls = docker_calls(server)
    assert ("builder prune -af" in calls) is swept
    log = (server.parent / "docker.log").read_text()
    if swept:
        assert calls.index("builder prune -af --filter until=24h") < calls.index("builder prune -af")
        assert f"the build cache is {left} after the 24h prune, over 3GB: removing all of it" in result.stdout
        assert f"빌드 캐시 {left} → 0B" in log
        assert f"빌드 캐시가 하루 정리 뒤에도 3GB를 넘어({left}) 모두 지웠습니다." in log
    else:
        assert f"빌드 캐시 {left} → {left}" in log and "모두 지웠습니다" not in log


def test_a_prune_that_fails_leaves_the_update_done_and_the_notice_says_why(server: Path) -> None:
    (server / "DOCKER_FAILS").write_text("x")
    new = push(server, "Granger learns")

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert (server / ".auto-update-deployed").read_text().strip() == new
    error = "Error response from daemon: a prune operation is already running"
    assert f"could not prune the build cache: {error}" in result.stderr
    assert f"could not prune the images: {error}" in result.stderr
    log = (server.parent / "docker.log").read_text()
    assert "자동 업데이트 오류" not in log
    assert "Docker 이미지 ? → ?, 빌드 캐시 ? → ?" in log, "what could not be read is said so"
    assert f"Docker 정리 실패: 이미지({error}); 빌드 캐시({error}). 다음 배포가 다시 정리합니다." in log


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


def deployed(server: Path) -> str:
    return (server / ".auto-update-deployed").read_text().strip()


def notices(server: Path) -> list[str]:
    """The first line of each notice the script sent or tried to send, in order."""
    return [c.split(" engine notify ", 1)[1] for c in docker_calls(server) if " engine notify " in c]


def assert_kept(server: Path, running: str) -> None:
    """The engine runs what it ran: the checkout and the record of it did not move, and nothing
    was built."""
    assert git(server, "rev-parse", "HEAD") == running
    assert deployed(server) == running
    assert "compose up -d --build" not in docker_calls(server)


def test_a_main_whose_ci_passed_is_deployed_and_github_is_asked_for_that_very_commit(
    server: Path, github: FakeActions
) -> None:
    """#541: the ci workflow's run that the push of exactly this commit to main started."""
    assert update(server).returncode == 0
    assert github.asked == [], "nothing new on main: GitHub's API is not asked"

    new = push(server, "Granger learns")
    result = update(server)

    assert result.returncode == 0, result.stderr
    assert git(server, "rev-parse", "HEAD") == new and deployed(server) == new
    assert github.asked == [{"event": "push", "head_sha": new}]


def test_a_main_whose_ci_failed_keeps_the_running_engine_and_is_told_once(
    server: Path, github: FakeActions
) -> None:
    assert update(server).returncode == 0
    running = deployed(server)
    bad = push(server, "a red main")
    github.runs[bad] = [ci_run(bad, conclusion="failure", number=7)]

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert_kept(server, running)
    assert f"auto-update: keeping {running[:7]}: the ci of {bad} failed (failure)" in result.stdout
    assert ci_run(bad, number=7)["html_url"] in result.stdout
    [notice] = notices(server)
    assert notice.startswith(f"자동 업데이트 보류: main {bad[:7]}의 CI가 실패했습니다(failure).")
    assert f"엔진은 {running[:7]} 그대로" in notice
    assert ci_run(bad, number=7)["html_url"] in (server.parent / "docker.log").read_text()

    again = update(server)  # the next morning, main as it was

    assert again.returncode == 0, again.stderr
    assert_kept(server, running)
    assert len(notices(server)) == 1, "the same failure is not told every day"
    assert f"the ci of {bad} failed (failure)" in again.stdout and "told on Telegram before" in again.stdout

    fixed = push(server, "the fix")
    assert update(server).returncode == 0
    assert git(server, "rev-parse", "HEAD") == fixed and deployed(server) == fixed
    assert not (server / ".auto-update-ci-failed").exists()


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "startup_failure", "action_required"])
def test_any_conclusion_but_success_keeps_the_running_engine(
    server: Path, github: FakeActions, conclusion: str
) -> None:
    assert update(server).returncode == 0
    running = deployed(server)
    new = push(server, "anything")
    github.runs[new] = [ci_run(new, conclusion=conclusion)]

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert_kept(server, running)
    assert f"the ci of {new} failed ({conclusion})" in result.stdout
    assert len(notices(server)) == 1


@pytest.mark.parametrize(
    ("runs", "said"),
    [
        ([], "no run yet"),
        ([("queued", None)], "queued"),
        ([("in_progress", None)], "in_progress"),
        # Pushed twice: the newer run counts, though the answer lists it last.
        ([("completed", "failure"), ("in_progress", None)], "in_progress"),
    ],
)
def test_a_main_whose_ci_is_not_done_keeps_the_running_engine_until_it_passes(
    server: Path, github: FakeActions, runs: list[tuple[str, str | None]], said: str
) -> None:
    assert update(server).returncode == 0
    running = deployed(server)
    new = push(server, "Granger learns")
    github.runs[new] = [
        ci_run(new, status, conclusion, number=i + 1) for i, (status, conclusion) in enumerate(runs)
    ]

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert_kept(server, running)
    assert f"auto-update: keeping {running[:7]}: the ci of {new} is not done ({said})" in result.stdout
    assert notices(server) == [], "the next run decides"

    github.runs[new] = [ci_run(new, number=9)]
    assert update(server).returncode == 0
    assert git(server, "rev-parse", "HEAD") == new and deployed(server) == new


def test_a_run_of_another_commit_or_of_a_pull_request_does_not_let_main_through(
    server: Path, github: FakeActions
) -> None:
    assert update(server).returncode == 0
    running = deployed(server)
    new = push(server, "Granger learns")
    # A pull request's run tests its merge with the base, not this commit; the commit before
    # passed, in an answer that ignored the filter.
    github.runs[new] = [ci_run(new, event="pull_request", number=2)]
    github.stray = [ci_run(running, number=1)]

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert_kept(server, running)
    assert f"the ci of {new} is not done (no run yet)" in result.stdout


@pytest.mark.parametrize(
    ("fail", "body", "said"),
    [
        (403, None, "HTTP 403, 0 requests left this hour"),
        (503, None, "HTTP 503"),
        (None, b"<html>a proxy's page</html>", "JSONDecodeError"),
        (None, b'{"message": "Moved Permanently"}', "KeyError"),
    ],
)
def test_when_github_cannot_be_read_the_running_engine_stays_and_it_is_told(
    server: Path, github: FakeActions, fail: int | None, body: bytes | None, said: str
) -> None:
    assert update(server).returncode == 0
    running = deployed(server)
    new = push(server, "Granger learns")
    github.fail, github.body = fail, body

    result = update(server)

    assert result.returncode != 0
    assert_kept(server, running)
    said_why = f"auto-update: keeping {running[:7]}: could not read the ci of {new} from GitHub ({said})"
    assert said_why in result.stderr
    [notice] = notices(server)
    assert notice == (
        f"자동 업데이트 보류: GitHub에서 main {new[:7]}의 CI 결과를 읽지 못했습니다({said})."
        f" 엔진은 {running[:7]} 그대로입니다."
    )
    assert "자동 업데이트 오류" not in notice

    github.fail = github.body = None
    assert update(server).returncode == 0
    assert git(server, "rev-parse", "HEAD") == new


def test_when_github_does_not_answer_at_all_the_running_engine_stays(server: Path) -> None:
    assert update(server).returncode == 0
    running = deployed(server)
    new = push(server, "Granger learns")
    with socket.socket() as closed:  # a port nothing listens on
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]

    result = update(server, api=f"http://127.0.0.1:{port}/repos/{REPO}")

    assert result.returncode != 0
    assert_kept(server, running)
    assert f"could not read the ci of {new} from GitHub (URLError(ConnectionRefusedError))" in result.stderr


def test_a_commit_that_only_moves_fincept_is_vouched_for_by_the_commit_it_was_made_on(
    server: Path, github: FakeActions
) -> None:
    """The fincept-pointer workflow pushes with GITHUB_TOKEN, which starts no workflow: GitHub
    lists no ci run for such a commit (63e4cdc, probed 2026-11-05). The image and CI read nothing
    of fincept/."""
    assert update(server).returncode == 0
    code = push(server, "Granger learns")
    first, second = move_fincept(server, "a" * 40), move_fincept(server, "b" * 40)
    github.runs[first] = github.runs[second] = []

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert git(server, "rev-parse", "HEAD") == second and deployed(server) == second
    assert github.asked_for() == [code]
    said = f"main {second[:7]} only moves the fincept pointer since {code[:7]}; its ci answers"
    assert said in result.stdout


def test_fincept_pointer_moves_on_a_red_main_keep_it_held_and_tell_nothing_new(
    server: Path, github: FakeActions
) -> None:
    assert update(server).returncode == 0
    running = deployed(server)
    bad = push(server, "a red main")
    github.runs[bad] = [ci_run(bad, conclusion="failure")]
    assert update(server).returncode == 0
    pointer = move_fincept(server, "a" * 40)
    github.runs[pointer] = []

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert_kept(server, running)
    assert github.asked_for() == [bad, bad]
    assert len(notices(server)) == 1 and "told on Telegram before" in result.stdout


def test_a_failure_notice_that_did_not_go_is_sent_on_the_next_run(server: Path, github: FakeActions) -> None:
    assert update(server).returncode == 0
    (server / "NOTIFY_FAILS").write_text("x")
    bad = push(server, "a red main")
    github.runs[bad] = [ci_run(bad, conclusion="failure")]

    result = update(server)

    assert result.returncode == 0, result.stderr
    assert "could not send to Telegram" in result.stderr
    assert not (server / ".auto-update-ci-failed").exists(), "not told: not remembered"

    (server / "NOTIFY_FAILS").unlink()
    again = update(server)

    assert again.returncode == 0, again.stderr
    assert len(notices(server)) == 2 and "told on Telegram before" not in again.stdout
    assert (server / ".auto-update-ci-failed").read_text().strip() == bad


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
WATCHDOG_USER = "krx-quant-watchdog"
# aws and gh as deploy/create-watchdog-user.sh calls them. IAM keeps the user and its access keys
# in STATE/iam.json ("all" holds every key ever made, deleted ones too), and every call, with what
# gh read on its standard input, goes to LOG as one JSON line. AWS's limit of two keys per user
# holds. GH_LOGGED_OUT=1 makes gh logged out; GH_FAIL_SECRET names a secret gh fails to set.
FAKE_AWS_AND_GH = f"""#!/usr/bin/env python3
import json, os, sys
state_file = os.path.join(os.environ["STATE"], "iam.json")
log = os.environ["LOG"]
args = sys.argv[1:]
tool = os.path.basename(sys.argv[0])
stdin = sys.stdin.read() if tool == "gh" and args[:2] == ["secret", "set"] else None
with open(log, "a") as f:
    f.write(json.dumps({{"tool": tool, "args": args, "stdin": stdin}}) + "\\n")
command = " ".join(args[:2])
if tool == "gh":
    if command == "auth status" and os.environ.get("GH_LOGGED_OUT"):
        sys.exit(1)
    if command == "secret set" and args[2] == os.environ.get("GH_FAIL_SECRET"):
        sys.exit(1)
    sys.exit(0)
if command == "configure get":
    print("ap-southeast-2")
    sys.exit(0)
if command == "lightsail get-instance":
    print("{INSTANCE_ARN}")
    sys.exit(0)
if not command.startswith("iam "):
    sys.exit(f"unexpected aws call: {{args}}")
iam = json.load(open(state_file)) if os.path.exists(state_file) else {{"user": False, "keys": [], "all": []}}
option = lambda name: args[args.index(name) + 1]
if command == "iam get-user":
    if not iam["user"]:
        sys.exit(254)
    print("{WATCHDOG_USER}")
elif command == "iam create-user":
    iam["user"] = True
elif command == "iam put-user-policy":
    pass
elif command == "iam list-access-keys":
    keys = iam["keys"]
    if "sort_by(AccessKeyMetadata,&CreateDate)" in option("--query"):
        keys = sorted(keys, key=lambda k: k["created"])
    print("\\t".join(k["id"] for k in keys))
elif command == "iam create-access-key":
    if not iam["user"] or len(iam["keys"]) >= 2:
        sys.exit("LimitExceeded: Cannot exceed quota for AccessKeysPerUser: 2")
    n = len(iam["all"]) + 1
    key = {{"id": f"TESTKEY{{n:04d}}", "secret": f"test-secret-{{n}}-must-never-show", "created": n}}
    iam["keys"].append(key)
    iam["all"].append(key)
    print(key["id"] + "\\t" + key["secret"])
elif command == "iam delete-access-key":
    before = len(iam["keys"])
    iam["keys"] = [k for k in iam["keys"] if k["id"] != option("--access-key-id")]
    if len(iam["keys"]) == before:
        sys.exit("NoSuchEntity")
else:
    sys.exit(f"unexpected aws call: {{args}}")
json.dump(iam, open(state_file, "w"))
"""


def create_watchdog_user(
    tmp_path: Path, *, keys: tuple[dict[str, Any], ...] = (), bash_x: bool = False, **extra_env: str
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]], dict[str, Any]]:
    """Run deploy/create-watchdog-user.sh against the fake aws and gh: its result, the calls it
    made, and IAM afterwards. ``keys`` are the user's keys before the first run."""
    bin_dir, state, log = tmp_path / "bin", tmp_path / "state", tmp_path / "calls.jsonl"
    for folder in (bin_dir, state):
        folder.mkdir(exist_ok=True)
    for tool in ("aws", "gh"):
        (bin_dir / tool).write_text(FAKE_AWS_AND_GH)
        (bin_dir / tool).chmod(0o755)
    if keys:
        made = {"user": True, "keys": list(keys), "all": list(keys)}
        (state / "iam.json").write_text(json.dumps(made))
    before = len(log.read_text().splitlines()) if log.exists() else 0
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STATE": str(state), "LOG": str(log), "HOME": str(tmp_path)}
    script = ROOT / "deploy" / "create-watchdog-user.sh"
    command = ["bash", "-x", str(script)] if bash_x else [str(script)]
    done = subprocess.run(command, env={**env, **extra_env}, capture_output=True, text=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()[before:]]
    iam_file = state / "iam.json"
    return done, calls, json.loads(iam_file.read_text()) if iam_file.exists() else {}


def document(calls: list[dict[str, Any]], command: str, option: str) -> Any:
    [args] = [c["args"] for c in calls if " ".join(c["args"][:2]) == command]
    return json.loads(args[args.index(option) + 1])


def aws_commands(calls: list[dict[str, Any]]) -> list[str]:
    return [" ".join(c["args"][:2]) for c in calls if c["tool"] == "aws"]


def secrets_set(calls: list[dict[str, Any]]) -> list[tuple[str, str]]:
    sets = [c for c in calls if c["tool"] == "gh" and c["args"][:2] == ["secret", "set"]]
    assert all(c["args"][3:] == ["--repo", "codeyoma/krx-quant"] for c in sets), sets
    return [(c["args"][2], c["stdin"]) for c in sets]


def index_of(calls: list[dict[str, Any]], *args: str) -> int:
    [i] = [i for i, c in enumerate(calls) if tuple(c["args"][: len(args)]) == args]
    return i


def assert_shows_nothing_secret(done: subprocess.CompletedProcess[str], iam: dict[str, Any]) -> None:
    shown = done.stdout + done.stderr
    assert ACCOUNT not in shown and "arn:" not in shown, "the ARNs hold the account ID"
    for key in iam["all"]:
        assert key["id"] not in shown and key["secret"] not in shown


def made_key(n: int) -> dict[str, Any]:
    return {"id": f"TESTKEY{n:04d}", "secret": f"test-secret-{n}-must-never-show", "created": n}


def test_the_watchdog_user_may_only_read_health_and_reboot_and_its_key_goes_only_into_the_secrets(
    tmp_path: Path,
) -> None:
    done, calls, iam = create_watchdog_user(tmp_path)

    assert done.returncode == 0, done.stderr
    assert aws_commands(calls) == [
        "configure get",
        "lightsail get-instance",
        "iam get-user",
        "iam create-user",
        "iam put-user-policy",
        "iam list-access-keys",
        "iam create-access-key",
    ], "no console password, no other policy, nothing to delete, and no OIDC provider"
    assert ["iam", "create-user", "--user-name", WATCHDOG_USER] in [c["args"] for c in calls]
    [put] = [c["args"] for c in calls if c["args"][:2] == ["iam", "put-user-policy"]]
    assert put[put.index("--user-name") + 1] == WATCHDOG_USER
    assert put[put.index("--policy-name") + 1] == "krx-quant-watchdog"
    policy = document(calls, "iam put-user-policy", "--policy-document")
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
    [key] = iam["keys"]
    assert secrets_set(calls) == [
        ("AWS_WATCHDOG_ACCESS_KEY_ID", key["id"]),
        ("AWS_WATCHDOG_SECRET_ACCESS_KEY", key["secret"]),
    ]
    assert not any(key["secret"] in arg for c in calls for arg in c["args"]), "on no command line"
    assert_shows_nothing_secret(done, iam)
    assert "open-id-connect" not in (ROOT / "deploy" / "create-watchdog-user.sh").read_text()


def test_the_watchdog_user_script_run_again_rotates_the_key_and_deletes_the_old_one_after_both_secrets(
    tmp_path: Path,
) -> None:
    create_watchdog_user(tmp_path)

    done, calls, iam = create_watchdog_user(tmp_path)

    assert done.returncode == 0, done.stderr
    assert "iam create-user" not in aws_commands(calls)
    assert aws_commands(calls).count("iam create-access-key") == 1
    old, new = iam["all"]
    assert iam["keys"] == [new]
    assert secrets_set(calls) == [
        ("AWS_WATCHDOG_ACCESS_KEY_ID", new["id"]),
        ("AWS_WATCHDOG_SECRET_ACCESS_KEY", new["secret"]),
    ]
    deleted = index_of(
        calls, "iam", "delete-access-key", "--user-name", WATCHDOG_USER, "--access-key-id", old["id"]
    )
    assert deleted > index_of(calls, "secret", "set", "AWS_WATCHDOG_SECRET_ACCESS_KEY")
    assert deleted > index_of(calls, "secret", "set", "AWS_WATCHDOG_ACCESS_KEY_ID")
    assert "deleted the older keys: 1" in done.stdout
    assert_shows_nothing_secret(done, iam)


def test_with_two_keys_there_the_watchdog_user_script_deletes_the_older_first_and_the_other_after_the_secrets(
    tmp_path: Path,
) -> None:
    older, newer = made_key(1), made_key(2)

    done, calls, iam = create_watchdog_user(tmp_path, keys=(newer, older))

    assert done.returncode == 0, done.stderr
    new = iam["all"][-1]
    assert iam["keys"] == [new]
    first = index_of(
        calls, "iam", "delete-access-key", "--user-name", WATCHDOG_USER, "--access-key-id", older["id"]
    )
    assert first < index_of(calls, "iam", "create-access-key")
    second = index_of(
        calls, "iam", "delete-access-key", "--user-name", WATCHDOG_USER, "--access-key-id", newer["id"]
    )
    assert second > index_of(calls, "secret", "set", "AWS_WATCHDOG_SECRET_ACCESS_KEY")
    assert secrets_set(calls)[-1] == ("AWS_WATCHDOG_SECRET_ACCESS_KEY", new["secret"])
    assert "two keys were there, the most AWS allows: deleted the older one first" in done.stdout
    assert_shows_nothing_secret(done, iam)


def test_the_watchdog_user_script_stops_before_making_anything_when_gh_is_not_logged_in(
    tmp_path: Path,
) -> None:
    done, calls, iam = create_watchdog_user(tmp_path, GH_LOGGED_OUT="1")

    assert done.returncode != 0
    assert "gh auth login" in done.stderr and "Nothing was made." in done.stderr
    assert aws_commands(calls) == ["configure get"], "no AWS call but the profile's region"
    assert iam == {}


def test_when_a_secret_cannot_be_set_the_older_key_stays_and_running_again_repairs_it(tmp_path: Path) -> None:
    create_watchdog_user(tmp_path)

    done, calls, iam = create_watchdog_user(tmp_path, GH_FAIL_SECRET="AWS_WATCHDOG_SECRET_ACCESS_KEY")

    assert done.returncode != 0
    assert "iam delete-access-key" not in aws_commands(calls)
    assert iam["keys"] == iam["all"], "the older key is kept"
    assert "run this again" in done.stderr
    assert_shows_nothing_secret(done, iam)

    done, calls, iam = create_watchdog_user(tmp_path)

    assert done.returncode == 0, done.stderr
    assert iam["keys"] == [iam["all"][-1]] and len(iam["all"]) == 3
    assert secrets_set(calls)[-1] == ("AWS_WATCHDOG_SECRET_ACCESS_KEY", iam["all"][-1]["secret"])


def test_the_watchdog_user_script_shows_no_key_even_when_traced(tmp_path: Path) -> None:
    done, _, iam = create_watchdog_user(tmp_path, bash_x=True)

    assert done.returncode == 0, done.stderr
    assert_shows_nothing_secret(done, iam)
