"""Budget-aware grounding of raw tool output for the ReAct loop.

The weak local model can only tell whether an edit applied or whether tests
actually passed if it sees the REAL tool output. So we feed tool results back
verbatim whenever they fit a generous byte budget. Only on genuine overflow do
we truncate — and even then we keep a HEAD and a TAIL joined by a clear marker,
because the decisive evidence (FAILED lines, assert messages, tracebacks) is
almost always at the END of the output.

Pure and deterministic: no I/O, no imports beyond what's shown.
"""
from __future__ import annotations

DEFAULT_TOOL_RESULT_BUDGET = 16000


def ground_tool_result(text: str, budget: int = DEFAULT_TOOL_RESULT_BUDGET) -> str:
    """Return ``text`` verbatim if it fits ``budget`` chars; otherwise keep a
    head and a tail joined by a ``\\n…[N chars truncated]…\\n`` marker.

    Args:
        text: the raw tool output (stdout/stderr, file contents, test results).
        budget: max chars of ORIGINAL content to retain (the marker is extra).

    Guarantees:
        * ``len(text) <= budget``  → returns ``text`` unchanged (no marker).
        * otherwise → returns ``head + marker + tail`` where head and tail are
          taken from the start and end of ``text``, ``head`` + ``tail`` length
          ``<= budget``, and the marker reports the exact number of dropped chars.
        * both the first and last characters of ``text`` are always preserved
          when truncation occurs (head and tail are each at least 1 char for a
          positive budget).
    """
    if budget <= 0 or len(text) <= budget:
        return text

    head_len = budget // 2
    tail_len = budget - head_len
    # Guard the degenerate budget==1 / odd-split case so both ends survive.
    if head_len < 1:
        head_len = 1
    if tail_len < 1:
        tail_len = 1

    head = text[:head_len]
    tail = text[len(text) - tail_len:]
    dropped = len(text) - head_len - tail_len
    marker = f"\n…[{dropped} chars truncated]…\n"
    return head + marker + tail
