"""dataset-search skill logic: find public datasets from HF Hub and PapersWithCode.

CRITICAL: never write to stdout. All logging goes to stderr.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import requests

logging.basicConfig(
    stream=sys.stderr, level=logging.INFO, format="%(name)s %(levelname)s %(message)s"
)
log = logging.getLogger("dataset-search")

_HF_API = "https://huggingface.co/api/datasets"
_PWC_API = "https://paperswithcode.com/api/v1/datasets"
_GITHUB_API = "https://api.github.com/search/repositories"


def search_hf_hub(
    query: str,
    task: str | None = None,
    modality: str | None = None,
    max_results: int = 10,
    timeout: int = 15,
) -> dict:
    """Search Hugging Face Hub for datasets matching a query.

    Returns {results: [{id, downloads, likes, task_categories, description}]}.
    """
    params: dict[str, Any] = {"search": query, "limit": max_results, "full": False}
    if task:
        params["filter"] = task
    try:
        resp = requests.get(_HF_API, params=params, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as exc:
        log.warning("HF Hub search failed: %s", exc)
        return {"results": [], "error": str(exc)}
    results = []
    for ds in raw:
        if not isinstance(ds, dict):
            continue
        results.append(
            {
                "id": ds.get("id", ""),
                "downloads": ds.get("downloads", 0),
                "likes": ds.get("likes", 0),
                "task_categories": ds.get("task_categories", []),
                "description": (ds.get("description") or "")[:300],
            }
        )
    return {"results": results}


def search_papers_with_code(
    query: str,
    max_results: int = 5,
    timeout: int = 15,
) -> dict:
    """Search PapersWithCode for datasets matching a query.

    Returns {results: [{name, full_name, description, tasks, url}]}.
    """
    params: dict[str, Any] = {"q": query, "items_per_page": max_results}
    try:
        resp = requests.get(_PWC_API, params=params, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as exc:
        log.warning("PapersWithCode search failed: %s", exc)
        return {"results": [], "error": str(exc)}
    results = []
    for ds in raw.get("results", []):
        if not isinstance(ds, dict):
            continue
        results.append(
            {
                "name": ds.get("name", ""),
                "full_name": ds.get("full_name", ""),
                "description": (ds.get("description") or "")[:300],
                "tasks": [t.get("task", "") for t in (ds.get("evaluations") or [])],
                "url": ds.get("url", ""),
            }
        )
    return {"results": results}


def search_github(
    query: str,
    max_results: int = 10,
    timeout: int = 15,
) -> dict:
    """Search GitHub for dataset repositories matching a query.

    Dataset-scoped: the query is constrained with a `dataset` term so this returns
    corpora/benchmarks hosted on GitHub, NOT general code/tool repos (those belong
    to web-search). Uses GITHUB_TOKEN if set (raises the search rate limit from
    ~10 to 30 req/min); works unauthenticated otherwise. Returns
    {results: [{full_name, description, stars, url, topics}]}.
    """
    # Append a dataset scope so we surface corpora, not arbitrary tool repos.
    scoped_q = f"{query} dataset"
    params: dict[str, Any] = {
        "q": scoped_q,
        "per_page": max_results,
        "sort": "stars",
        "order": "desc",
    }
    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(_GITHUB_API, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as exc:
        log.warning("GitHub search failed: %s", exc)
        return {"results": [], "error": str(exc)}
    results = []
    for repo in raw.get("items", []):
        if not isinstance(repo, dict):
            continue
        results.append(
            {
                "full_name": repo.get("full_name", ""),
                "description": (repo.get("description") or "")[:300],
                "stars": repo.get("stargazers_count", 0),
                "url": repo.get("html_url", ""),
                "topics": repo.get("topics", []),
            }
        )
    return {"results": results}


def rank_candidates(
    candidates: list[dict],
    query: str,
    criteria: list[str] | None = None,
) -> dict:
    """Score and rank candidates by keyword overlap with the query.

    Purely lexical — no LLM. Returns {ranked: [{...original, score}]}.
    """
    if criteria is None:
        criteria = []
    terms = set(query.lower().split() + [c.lower() for c in criteria])
    scored = []
    for item in candidates:
        searchable = " ".join(
            [
                str(item.get("id", "")),
                str(item.get("name", "")),
                str(item.get("full_name", "")),
                str(item.get("description", "")),
                " ".join(str(t) for t in item.get("task_categories", [])),
                " ".join(str(t) for t in item.get("tasks", [])),
                " ".join(str(t) for t in item.get("topics", [])),
            ]
        ).lower()
        overlap = sum(1 for t in terms if t in searchable)
        score = overlap / max(len(terms), 1)
        scored.append({**item, "score": round(score, 3)})
    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)
    return {"ranked": ranked}
