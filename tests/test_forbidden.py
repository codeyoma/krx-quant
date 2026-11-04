"""The repository check (``scripts/check_forbidden.py``) fails on a PR number placeholder (#321),
and on a KIS order TR ID or endpoint anywhere but the trader's KIS account module (#403).

The placeholders, TR IDs and endpoints here are put together at run time, so the check does not
find them in this file.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from krx_quant.forbidden import ORDER_MODULE, REPOSITORY_RULES, RULES, findings
from krx_quant.redaction import MASK, redact

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_forbidden.py"
HASH = "#"


def placeholder_hits(text: str) -> list[str]:
    return [name for name, pattern in REPOSITORY_RULES if pattern.search(text)]


@pytest.mark.parametrize(
    "stand_in",
    [
        *("PRNUM", "NNN", "PRB", "PRDOCS", "__PR3__", "PR_NUMBER", "XXX", "TBD", "NNNN"),
        *("KQPR", "FORKPR", "DOCS_PR"),  # capitals ending in PR (#375)
    ],
)
def test_a_change_notes_stand_in_for_its_pr_number_is_found(stand_in: str) -> None:
    note = f"> **바뀜** (2026-10-20, {HASH}{stand_in}): 이전 → 지금. 이유: …."
    assert placeholder_hits(note) == ["PR number placeholder"]


@pytest.mark.parametrize(
    "text",
    [
        "> **바뀜** (2026-10-20, #317): 이전 → 지금.",  # a real PR number
        "Closes #321",
        "- `> **삭제됨** (YYYY-MM-DD, #PR): 무엇이 없어졌는지.`",  # the format itself (AGENTS.md)
        "cited by commit SHA instead of `#PR`.",
        "# PRNUM heading",  # a heading, not a reference
        "x = 1  # NNN in a comment after a space",
        "codeyoma/FinceptTerminal#97",
        "codeyoma/FinceptTerminal#103",
        "> **바뀜** (2026-10-24, #375): 이전 → 지금.",
        "#PRs",  # lower case: prose, not a stand-in
        "#KQPRs",  # nor capitals running on in lower case
        "#Kqpr",
    ],
)
def test_real_references_and_the_format_pass(text: str) -> None:
    assert placeholder_hits(text) == []


def test_the_check_fails_on_a_file_with_a_placeholder_and_passes_once_it_is_replaced(tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text(f"> **바뀜** (2026-10-20, {HASH}PRNUM): 이전 → 지금.\n", encoding="utf-8")
    failed = subprocess.run([sys.executable, str(SCRIPT), str(doc)], capture_output=True, text=True)
    assert failed.returncode == 1
    assert "notes.md:1: PR number placeholder" in failed.stdout

    doc.write_text("> **바뀜** (2026-10-20, #321): 이전 → 지금.\n", encoding="utf-8")
    passed = subprocess.run([sys.executable, str(SCRIPT), str(doc)], capture_output=True, text=True)
    assert passed.returncode == 0


def test_posted_output_is_not_masked_for_placeholders() -> None:
    """The placeholder rule is the repository's alone: what the server posts keeps it."""
    text = f"see {HASH}PRNUM"
    assert redact(text, ()) == text
    assert all(pattern.pattern != REPOSITORY_RULES[0][1].pattern for _, pattern in RULES)


# The KIS order rules' one exception (#393, #403): the trader's KIS account module. The TR IDs and
# endpoints below are put together at run time, so the check does not find them in this file.
ORDER_TR_ID = "VTTC" + "0012U"
ORDER_ENDPOINT = "/uapi/domestic-stock/v1/trading/" + "order" + "-cash"
CANCEL_ENDPOINT = "order-" + "rvsecncl"
ORDER_LINES = (f'BUY = "{ORDER_TR_ID}"', f'PATH = "{ORDER_ENDPOINT}"', f"see {CANCEL_ENDPOINT}")
ROOT = SCRIPT.parents[1]


def hit_names(path: str, text: str) -> list[str]:
    return [name for _, name in findings(path, text)]


@pytest.mark.parametrize("line", ORDER_LINES)
def test_an_order_tr_id_or_endpoint_passes_in_the_trader_s_kis_module_only(line: str) -> None:
    assert hit_names(ORDER_MODULE, line) == []
    for elsewhere in (
        "src/krx_quant/order_probe.py",  # the probe goes through kis_orders
        "src/krx_quant/kis.py",
        "src/krx_quant/fake_kis.py",
        "src/krx_quant/trader/kis_orders.py",  # a file of the same name elsewhere
        "tests/test_order_probe.py",
        "docs/kis-feeds.md",
        "kis_orders.py",
    ):
        assert hit_names(elsewhere, line) in (["KIS order TR ID"], ["KIS order endpoint"]), elsewhere


def test_every_other_rule_holds_in_the_trader_s_kis_module() -> None:
    telegram = "1234567890" + ":" + "A" * 35
    toss = "/api/v1/" + "orders"
    placeholder = f"> **바뀜** (2026-10-29, {HASH}PRNUM): 이전 → 지금."
    assert hit_names(ORDER_MODULE, f'TOKEN = "{telegram}"') == ["Telegram bot token"]
    assert hit_names(ORDER_MODULE, f'TOSS = "{toss}"') == ["Toss order endpoint"]
    assert hit_names(ORDER_MODULE, placeholder) == ["PR number placeholder"]


def test_the_check_passes_the_module_but_fails_a_copy_of_it_anywhere_else(tmp_path: Path) -> None:
    module = ROOT / ORDER_MODULE
    text = module.read_text(encoding="utf-8")
    found = {name for _, name in findings("elsewhere.py", text)}
    assert found == {"KIS order TR ID", "KIS order endpoint"}, (
        "the module holds the order TR IDs and endpoints: the exception is what lets it pass"
    )
    passed = subprocess.run([sys.executable, str(SCRIPT), str(module)], capture_output=True, text=True)
    assert passed.returncode == 0, passed.stdout
    for copy in (tmp_path / "kis_orders.py", tmp_path / "src" / "krx_quant" / "kis_orders.py"):
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(text, encoding="utf-8")
        failed = subprocess.run([sys.executable, str(SCRIPT), str(copy)], capture_output=True, text=True)
        assert failed.returncode == 1 and f"{copy}:" in failed.stdout and "KIS order TR ID" in failed.stdout


def test_an_order_tr_id_in_any_other_file_fails_the_check(tmp_path: Path) -> None:
    for name in ("probe.py", "notes.md"):
        path = tmp_path / name
        path.write_text(f'tr_id = "{ORDER_TR_ID}"\n', encoding="utf-8")
        failed = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
        assert failed.returncode == 1 and f"{name}:1: KIS order TR ID" in failed.stdout


def test_posted_output_is_masked_of_the_module_s_tr_ids_too() -> None:
    """``redaction.py`` takes no exception: what the server posts never shows an order TR ID."""
    from krx_quant import kis_orders

    for tr in (kis_orders.BUY, kis_orders.SELL, kis_orders.CANCEL):
        assert MASK in redact(f"sent {tr.mock} and {tr.live}", ())
        assert tr.mock not in redact(f"sent {tr.mock}", ()) and tr.live not in redact(tr.live, ())
    assert redact(f"POST {kis_orders.ORDER_PATH}", ()) != f"POST {kis_orders.ORDER_PATH}"


# A draft pull request's run skips the placeholder rule alone (#507): its number is filled in once
# it is open (scripts/fill_pr_number.py). Marking it ready, a push to main and a run by hand check
# everything.
TELEGRAM = "1234567890" + ":" + "A" * 35


def check(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=20)


def test_a_draft_s_check_skips_the_placeholder_rule_and_still_shows_it(tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text(f"> **바뀜** (2026-11-04, {HASH}PRNUM): 이전 → 지금.\n", encoding="utf-8")
    draft = check("--draft", str(doc))
    assert draft.returncode == 0, draft.stdout
    assert "notes.md:1: PR number placeholder (skipped on a draft" in draft.stdout
    assert "::warning::" in draft.stdout and "scripts/fill_pr_number.py" in draft.stdout

    ready = check(str(doc))
    assert ready.returncode == 1
    assert "notes.md:1: PR number placeholder\n" in ready.stdout


def test_a_draft_s_check_still_fails_every_other_rule(tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text(f'TOKEN = "{TELEGRAM}"\nsee {HASH}PRNUM\ntr_id = "{ORDER_TR_ID}"\n', encoding="utf-8")
    draft = check("--draft", str(doc))
    assert draft.returncode == 1
    assert "notes.md:1: Telegram bot token\n" in draft.stdout
    assert "notes.md:3: KIS order TR ID\n" in draft.stdout
    assert "notes.md:2: PR number placeholder (skipped on a draft" in draft.stdout


def test_the_skipped_rule_is_forbidden_py_s_placeholder_rule() -> None:
    from krx_quant import forbidden

    spec = importlib.util.spec_from_file_location("check_forbidden", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    [(name, pattern)] = forbidden.REPOSITORY_RULES
    assert (module.PLACEHOLDER, module.PLACEHOLDER_PATTERN) == (name, pattern)


def test_ci_passes_draft_only_on_a_draft_pull_request_and_runs_again_once_it_is_ready() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    # A push to main has no pull_request: the expression gives "" and every rule holds.
    flag = "${{ github.event.pull_request.draft && '--draft' || '' }}"
    assert f"run: uv run python scripts/check_forbidden.py {flag}\n" in workflow
    assert workflow.count("check_forbidden.py") == 1
    # Marking a draft ready runs the checks again, with the placeholder rule.
    assert "types: [opened, synchronize, reopened, ready_for_review]" in workflow
