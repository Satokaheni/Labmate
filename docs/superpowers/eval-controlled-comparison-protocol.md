# Controlled-Comparison Protocol (freeze model+code, vary one axis)

Every A/B comparison must **freeze model + git sha + fixtures** and **vary exactly one
axis**. Both result files carry a provenance header (`eval/provenance.py`); compare
them with `compare_provenance` before trusting a delta.

## Rule
1. Capture baseline and variant back-to-back on the SAME pod, SAME `MODEL` gguf, SAME
   `git rev-parse HEAD`, SAME fixtures (reset per trial by `run_seq_ab.py`).
2. The ONLY difference between the two runs is the one axis under test
   (`SEQUENCING_MODE`, one flag value, etc.).
3. Stamp `provenance` into both files (automatic in `run_seq_ab.py`).
4. `compare_provenance(a, b)` MUST return `[]` (no model/sha drift) before the delta
   counts. A non-empty result means the comparison mixes >1 axis — discard it.

## Known-bad comparisons this retires
- **current 12B code vs committed 31B `results-*.json`** — moved model + code at once.
  The stale files are now `results-*.31b.ref.json` (history only; never compared to a
  12B run without a `compare_provenance` warning).
- **`results-*.ref.json` (pre-fix) vs post-fix** — moved a bundle of code changes.
- **routing cases generated on 31B, evaluated on 12B** — generation/eval model drift.

## Known-good template (copy this)
- `skill_first` vs `react`: paired, same commit + model + fixtures, only
  `SEQUENCING_MODE` varied.

## Re-baseline task (run on the GPU host)
Recapture the 12B baseline under current HEAD:
```bash
TRIALS=3 bash eval/seq_ab/run_mode.sh skill_first
TRIALS=3 bash eval/seq_ab/run_mode.sh react
git add eval/seq_ab/results-skill_first.json eval/seq_ab/results-react.json
git commit -m "chore(eval): re-baseline seq_ab on 12B under HEAD"
```
The new files carry a 12B provenance header; the `.31b.ref.json` files stay for history.
