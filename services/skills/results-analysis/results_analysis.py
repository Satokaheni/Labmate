"""results-analysis skill logic: profile, compare, and visualize result files.

CRITICAL: never write to stdout. All logging goes to stderr.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless backend — must precede pyplot import
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("results-analysis")

_ID_HINTS = ("id", "index", "seed", "run", "step", "epoch")


def _read(file_path: str) -> pd.DataFrame:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(file_path)
    if suffix in (".jsonl", ".ndjson"):
        return pd.read_json(file_path, lines=True)
    if suffix == ".json":
        return pd.read_json(file_path)
    if suffix in (".parquet", ".pq"):
        return pd.read_parquet(file_path)
    raise ValueError(f"unsupported file type: {suffix}")


def _metric_columns(df: pd.DataFrame) -> list[str]:
    """Numeric columns whose name is not an obvious id/index column."""
    metrics = []
    for col in df.columns:
        if any(h in str(col).lower() for h in _ID_HINTS):
            continue
        if pd.api.types.is_numeric_dtype(df[col]):
            metrics.append(col)
    return metrics


def profile_results(file_path: str) -> dict:
    """Descriptive stats + auto-detected metric columns."""
    df = _read(file_path)
    metrics = _metric_columns(df)
    stats_out: dict = {}
    for col in metrics:
        s = df[col].dropna()
        stats_out[col] = {
            "mean": float(s.mean()) if len(s) else None,
            "std": float(s.std()) if len(s) else None,
            "min": float(s.min()) if len(s) else None,
            "max": float(s.max()) if len(s) else None,
            "count": int(s.count()),
        }
    return {
        "columns": [str(c) for c in df.columns],
        "stats": stats_out,
        "row_count": int(len(df)),
    }


def compare_runs(file_path: str, group_col: str, metric_col: str) -> dict:
    """Paired-ish significance test + bootstrap CI of the difference of means."""
    df = _read(file_path)
    groups = {str(k): g[metric_col].dropna().to_numpy()
              for k, g in df.groupby(group_col)}
    keys = list(groups)
    if len(keys) < 2:
        return {"groups": {k: v.tolist() for k, v in groups.items()},
                "p_value": 1.0, "confidence_interval": [0.0, 0.0],
                "significant": False}
    a, b = groups[keys[0]], groups[keys[1]]
    t_stat, p_value = stats.ttest_ind(a, b, equal_var=False)

    rng = np.random.default_rng(0)
    diffs = []
    for _ in range(2000):
        ra = rng.choice(a, size=len(a), replace=True)
        rb = rng.choice(b, size=len(b), replace=True)
        diffs.append(rb.mean() - ra.mean())
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {
        "groups": {k: v.tolist() for k, v in groups.items()},
        "p_value": float(p_value),
        "confidence_interval": [float(lo), float(hi)],
        "significant": bool(p_value < 0.05),
    }


def make_figures(file_path: str, metric_cols: list[str], output_dir: str) -> dict:
    """Bar plots per metric + markdown/LaTeX summary tables."""
    df = _read(file_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    figure_paths: list[str] = []
    for col in metric_cols:
        if col not in df.columns:
            continue
        fig, ax = plt.subplots()
        df[col].plot(kind="bar", ax=ax)
        ax.set_title(col)
        ax.set_ylabel(col)
        path = out / f"{col}.png"
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        figure_paths.append(str(path))

    summary = df[metric_cols].describe().transpose()
    summary.index.name = "metric"
    markdown_table = summary.reset_index().to_markdown(index=False)
    latex_table = summary.reset_index().to_latex(index=False)
    return {
        "figure_paths": figure_paths,
        "markdown_table": markdown_table,
        "latex_table": latex_table,
    }
