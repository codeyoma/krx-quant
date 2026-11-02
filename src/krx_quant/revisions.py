"""Revisions (#454): a running bar-close algorithm whose code changes is judged again.

An algorithm the engine has enrolled runs whatever its pre-validation report says (``engine``'s
``_admit``): without this, a variant chosen in an experiment and put into a running algorithm
(#283's neyman, #448's turing) would run live, keeping its probation or its place in the consensus,
never judged on the days after it was chosen. So the engine keeps the code each running bar-close
algorithm (the direction's and the other bar-close kinds': the daily kinds have their own rule,
#264) runs (``predictions.revisions``): its code digest (``prevalidation.code_digest``), its target
and inputs, and when it first ran it, the revision's boundary. At each start:

- **No revision yet** (the first start of this code, a new algorithm, one never recorded): its code
  is recorded as its first revision, as it runs. No status changes and no history is made up.
- **The same code**: nothing.
- **A reviewed refactor** (``SAME_REVISION``: an old digest, its new one and the evidence that
  training and prediction are unchanged): the new code is the same revision, its boundary, state
  and verdict kept; the old digest stays in the history. The map is never inferred.
- **Other code**: an algorithm active, benched, a clone or on probation goes back on probation once
  (``lifecycle.CODE_CHANGED``), in the same transaction as the new revision (a restart does not do
  it again). A retired one stays retired and its revision is left as it was: a later unretire and
  start find the code changed then. Those registered before pre-validation (turing among them)
  follow the rule too: their exemption holds for their first revision only.

That probation keeps predicting, out of the consensus and the signals. Its review counts the
predictions of the revision only, from its first whole trading day (``market_calendar.
first_whole_day``), and it is never simulated (#298): the days a simulation replays may be those
the change was chosen on. Once ``prevalidation.MIN_DAYS`` recorded days are past its boundary, a
scheduled job pre-validates it on those days only (``prevalidate --revision``, every check): the
report is bound to the revision (its ``revision``). Refused, the engine retires it at its next bar
close and tells it once. Passed, nothing more: it becomes active only when its live probation
passes a review too, and until it passed its pre-validation a review that would make it active
leaves it on probation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from krx_quant.algorithms import Algorithm, KindAlgorithm
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Kind
from krx_quant.prevalidation import _target, code_digest, inputs_of, revision_report


@dataclass(frozen=True)
class Refactor:
    """A reviewed change of an algorithm's code that changes neither what it learns nor what it
    predicts: ``new`` is the revision ``old`` was. ``evidence`` says where that was shown (the PR
    and the test comparing training and prediction on the same days); a prediction of the old model
    unchanged is not enough."""

    algorithm: str
    old: str  # the digest it replaces
    new: str
    evidence: str


# The reviewed map (#454): none so far. An entry goes in the PR that changes the code, with its
# evidence; the engine never infers one.
SAME_REVISION: tuple[Refactor, ...] = ()


@dataclass(frozen=True)
class Code:
    """What a revision is of: the algorithm's code digest, its target and its inputs' versions."""

    digest: str
    target_version: int | None
    inputs: Mapping[str, int]


@dataclass(frozen=True)
class Verdict:
    """The pre-validation of a revision (#454): passed or refused, with the checks it failed."""

    passed: bool
    why: str


class RevisionGate:
    """What the live engine asks of the code it runs (#454): each algorithm's ``Code``, whether a
    change of it is a reviewed refactor, and the verdict of its revision's pre-validation (the
    report in ``state_dir``, bound to the revision's boundary)."""

    def __init__(
        self,
        state_dir: Path,
        target_version: int = 1,
        kinds: Sequence[Kind] = KINDS,
        refactors: Sequence[Refactor] = SAME_REVISION,
    ) -> None:
        self._state_dir = state_dir
        self._target_version = target_version
        self._kinds = tuple(kinds)
        self._refactors = {(r.algorithm, r.old, r.new): r for r in refactors}

    def code(self, algorithm: Algorithm | KindAlgorithm) -> Code:
        return Code(
            code_digest(algorithm),
            _target(algorithm, self._target_version, self._kinds),
            inputs_of(algorithm),
        )

    def refactor(self, algorithm: str, old: str, new: str) -> Refactor | None:
        """The reviewed refactor that makes ``new`` the revision ``old`` was, if there is one."""
        return self._refactors.get((algorithm, old, new))

    def verdict(self, algorithm: Algorithm | KindAlgorithm, boundary: datetime) -> Verdict | None:
        """The verdict of the pre-validation of ``algorithm``'s revision begun at ``boundary``: its
        report made for it judged the code and target it runs now (``LastReport.judged``: passed, or
        refused on at least ``MIN_DAYS`` days); None while there is none."""
        found = revision_report(self._state_dir, algorithm, boundary, self._target_version, self._kinds)
        if found is None or not found[0].judged:
            return None
        last, why = found
        return Verdict(last.passed, "" if last.passed else why)
