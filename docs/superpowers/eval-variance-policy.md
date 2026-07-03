# Eval Variance Policy — when a flag default may be called a "win"

A flag default (or a mode, or a numeric constant) may be claimed a **win** ONLY when
all of the following hold, evidenced by a committed results file:

1. **TRIALS >= 3.** Single-shot runs are never a win (c1/c3 flake on the Q4 model —
   "same code, different dice").
2. **Disjoint Wilson 95% CIs.** The variant arm's `pass_rate_ci` must not overlap the
   baseline arm's. An overlapping-CI bump (e.g. +1/3 at n=3) is NOT a win.
3. **Above the trivial baseline.** The variant must beat the no-op floor
   (`eval/seq_ab/baselines.py`) and be reported next to it.
4. **Single-axis capture.** Baseline and variant differ on exactly ONE axis, under the
   same model + git sha + fixtures (see the controlled-comparison protocol).

The machine check is `eval/seq_ab/compare.py::compare_runs` (`win=True` requires 1, 2,
and rate>baseline). Produce the two arms with `eval/seq_ab/run_flag_ab.sh`.

**Flags whose defaults do NOT yet meet this bar** (see the audit spec, Part A2) are
intuition/anecdotal and must not be described as measured wins until an A/B is run.
Retiring vs measuring them is the backlog in the spec, Part E.
