# Eval Variance Policy — when a flag default may be called a "win"

A flag default (or a mode, or a numeric constant) may be claimed a **win** ONLY when
all of the following hold, evidenced by a committed results file:

1. **TRIALS >= 5.** Single-shot runs are never a win (c1/c3 flake on the Q4 model —
   "same code, different dice"). **n=3 is not enough to CLAIM a win** — it is below the
   floor *and* mathematically cannot clear condition 2: even a perfect 3/3-vs-0/3 split
   gives Wilson CIs `[0.44, 1.0]` vs `[0.0, 0.56]`, which overlap. n=5 is the smallest
   count where a clean 5/5-vs-0/5 separates (`[0.57, 1.0]` vs `[0.0, 0.43]`); partial
   effects need more. Use `TRIALS>=3` to *report* variance, but `>=5` to *claim* a win.
   (Confirmed live: the 12B `ROUTE_EDIT_TO_REACT` A/B at n=3 showed c6 go 1.0→0.0 yet
   still scored "no measured win" — the bar was structurally unreachable at 3 trials.)
2. **Disjoint Wilson 95% CIs.** The variant arm's `pass_rate_ci` must not overlap the
   baseline arm's. An overlapping-CI bump (e.g. +1/3 at n=3) is NOT a win.
3. **Above the trivial baseline.** The variant must beat the no-op floor
   (`eval/seq_ab/baselines.py`) and be reported next to it.
4. **Single-axis capture.** Baseline and variant differ on exactly ONE axis, under the
   same model + git sha + fixtures (see the controlled-comparison protocol).

The machine check is `eval/seq_ab/compare.py::compare_runs` (`win=True` requires 1, 2,
and rate>baseline); its `min_trials` default is **5**, so a run at n<5 can never report a
win. Produce the two arms with `eval/seq_ab/run_flag_ab.sh` (defaults `TRIALS=5`).

**Flags whose defaults do NOT yet meet this bar** (see the audit spec, Part A2) are
intuition/anecdotal and must not be described as measured wins until an A/B is run.
Retiring vs measuring them is the backlog in the spec, Part E.
