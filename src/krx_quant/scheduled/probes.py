"""The probe jobs (#500): each measures on the server what an outside source's data means (KIS,
Toss, the KRX Open API, the recordings) before the engine uses it (CODING_STANDARDS.md "External
data is probed before it is used"). ``JOBS`` (``krx_quant.scheduled``) lists them in the order they
run.

None is in ``JOBS`` now: the sixteen probes and checks up to ``probe-member-frames-381`` ran once,
posted their results and left it (#505), as a one-off job does once it has done its work, so that
a lost state file cannot run them again. Most of their commands stay for a run by hand (``krx-quant
probe-supply`` and the rest); those whose questions were settled went with their modules (#563).
A new probe goes here, with its condition (``core._first_night`` and ``core._first_trading_night``
are the usual ones).
"""
