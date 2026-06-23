import asyncio
import json
import logging
import sys

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

from semantic_scholar import SemanticScholarClient

log = logging.getLogger("citation-graph.server")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

mcp = FastMCP("citation-graph")
_ss = SemanticScholarClient()

# Free-tier politeness: serialize SS calls and space them out.
_rate_lock = asyncio.Lock()


async def _throttle() -> None:
    async with _rate_lock:
        await asyncio.sleep(0.6)


def _to_jsonl(papers: list[dict]) -> str:
    return "\n".join(json.dumps(p, ensure_ascii=False) for p in papers)


@mcp.tool()
async def search_papers(query: str, limit: int = 10, year_from: int | None = None) -> str:
    """Keyword/semantic search. Returns JSONL of papers
    (title/authors/year/doi/citation_count/...)."""
    await _throttle()
    papers = await asyncio.to_thread(_ss.search, query, limit, year_from)
    return _to_jsonl(papers)


@mcp.tool()
async def get_citations(paper_id: str, limit: int = 20) -> str:
    """Papers that cite this paper (forward citations). Returns JSONL."""
    await _throttle()
    papers = await asyncio.to_thread(_ss.get_citations, paper_id, limit)
    return _to_jsonl(papers)


@mcp.tool()
async def get_references(paper_id: str, limit: int = 20) -> str:
    """Papers this paper cites (references / backward citations). Returns JSONL."""
    await _throttle()
    papers = await asyncio.to_thread(_ss.get_references, paper_id, limit)
    return _to_jsonl(papers)


@mcp.tool()
async def find_similar(paper_id: str, limit: int = 10) -> str:
    """Embedding-based (SPECTER) similar papers. Returns JSONL."""
    await _throttle()
    papers = await asyncio.to_thread(_ss.find_similar, paper_id, limit)
    return _to_jsonl(papers)


@mcp.tool()
async def get_paper(paper_id: str) -> str:
    """Full metadata for one paper (abstract, venue, tldr, open_access_url). Returns one JSON line."""
    await _throttle()
    paper = await asyncio.to_thread(_ss.get_paper, paper_id)
    return json.dumps(paper, ensure_ascii=False)


if __name__ == "__main__":
    log.info("starting citation-graph MCP server on stdio")
    mcp.run()  # FastMCP defaults to stdio transport
