# C2 Two-Tier Skill Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Make `SkillRouter.select()` run all 3 samples up front, return immediately on unanimous agreement, and break ties with one extra thinking-budget sample.

**Architecture:** Extract a private `_sample_select(task, thinking_budget)` coroutine from the existing per-attempt loop body in `select()`. It makes one litellm call and returns the chosen skill name (validated against the catalog) or None. The new `select()` collects 3 zero-budget samples; if all non-None picks agree it returns that pick; on disagreement it runs one `thinking_budget_tokens=1024` sample to break the tie. Event emission (`reasoning` event with `_last_reasoning`) is preserved on the returned pick.

**Tech Stack:** Python, litellm (Gemma 4 31B), pytest.

---

### Task 1: Extract `_sample_select` and rewrite `select` as two-tier

**Files:**
- Modify: `services/orchestrator/skill_router.py`
- Modify: `tests/services/orchestrator/test_skill_router.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_skill_router.py` (the existing module already imports `SkillRouter`, `SkillRunner`, `SkillMeta`, `json`, `AsyncMock`, `MagicMock`, `patch`, and defines `_make_tool_call_response`):

```python
@pytest.mark.mocked
class TestTwoTierSelect:
    @pytest.fixture
    def router(self):
        runner = MagicMock(spec=SkillRunner)
        runner.catalog = {
            "ast-repo-map": SkillMeta("ast-repo-map", "Map a repo",
                                      Path("/fake/SKILL.md"), "bundled"),
            "web-search": SkillMeta("web-search", "Search the web",
                                    Path("/fake/SKILL.md"), "bundled"),
        }
        runner.catalog_prompt.return_value = "- ast-repo-map: Map a repo\n- web-search: Search the web"
        runner.tool_schema.return_value = {"type": "function", "function": {"name": "load_skill"}}
        return SkillRouter(runner=runner, redis=AsyncMock(),
                           gemma_api_base="http://localhost:8000/v1")

    @pytest.mark.asyncio
    async def test_unanimous_returns_immediately_without_tiebreak(self, router):
        """3 agreeing samples → return pick; no 4th (tiebreak) call."""
        resp = _make_tool_call_response("web-search")
        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new=AsyncMock(return_value=resp)) as mac:
            pick = await router.select("find recent papers")
        assert pick == "web-search"
        # Exactly SELECT_ATTEMPTS (3) calls — no tiebreak sample.
        assert mac.await_count == 3
        # All three used a zero thinking budget.
        for call in mac.await_args_list:
            assert call.kwargs["extra_body"]["thinking_budget_tokens"] == 0

    @pytest.mark.asyncio
    async def test_disagreement_runs_tiebreak_with_budget(self, router):
        """Disagreeing samples → one extra tiebreak call with budget=1024."""
        responses = [
            _make_tool_call_response("web-search"),
            _make_tool_call_response("ast-repo-map"),
            _make_tool_call_response("web-search"),
            _make_tool_call_response("ast-repo-map"),  # tiebreak result
        ]
        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new=AsyncMock(side_effect=responses)) as mac:
            pick = await router.select("ambiguous task")
        assert pick == "ast-repo-map"
        assert mac.await_count == 4  # 3 samples + 1 tiebreak
        # The 4th call uses the thinking budget.
        assert mac.await_args_list[3].kwargs["extra_body"]["thinking_budget_tokens"] == 1024

    @pytest.mark.asyncio
    async def test_returns_none_when_no_sample_picks(self, router):
        """No sample emits a valid catalog pick → None, no tiebreak."""
        no_call = MagicMock()
        no_call.choices = [MagicMock()]
        no_call.choices[0].message.tool_calls = None
        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new=AsyncMock(return_value=no_call)) as mac:
            pick = await router.select("nothing matches")
        assert pick is None
        assert mac.await_count == 3  # no tiebreak when there are zero picks

    @pytest.mark.asyncio
    async def test_sample_select_returns_validated_pick(self, router):
        """_sample_select returns a catalog-valid name, ignores out-of-catalog."""
        good = _make_tool_call_response("web-search")
        bad = _make_tool_call_response("not-a-real-skill")
        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new=AsyncMock(side_effect=[good, bad])):
            assert await router._sample_select("t", 0) == "web-search"
            assert await router._sample_select("t", 0) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_skill_router.py::TestTwoTierSelect -v`
Expected: FAIL (`_sample_select` does not exist; `select` does not run a tiebreak)

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/skill_router.py`, replace the entire `select` method body (lines from `async def select` through its `return None`) with the extracted helper plus the new two-tier `select`:

```python
    async def _sample_select(self, task: str, thinking_budget: int) -> str | None:
        """
        One independent selection sample. Makes a single litellm call with
        tools=[load_skill] and returns the chosen skill name (validated against
        the catalog) or None. On the returned pick, captures reasoning so the
        caller can emit a reasoning event.
        """
        catalog = self._runner.catalog_prompt()
        schema = self._runner.tool_schema()
        directive = (
            "You are a skill router. If ANY available skill is relevant to the "
            "user's task, you MUST call load_skill with that skill's name. Only "
            "decline to call a tool if truly no skill fits."
        )
        try:
            r = await litellm.acompletion(
                model="openai/gemma-4-31b",
                api_base=self._gemma_base,
                api_key="not-needed",
                messages=[
                    {"role": "system", "content": f"{directive}\n\n{catalog}"},
                    {"role": "user", "content": task},
                ],
                tools=[schema],
                tool_choice="auto",
                extra_body={"thinking_budget_tokens": thinking_budget},
            )
        except Exception as exc:
            _log.warning("_sample_select error: %s", exc)
            return None
        choices = getattr(r, "choices", None)
        if not choices:
            return None
        message = getattr(choices[0], "message", None)
        tool_calls = getattr(message, "tool_calls", None) if message else None
        if not tool_calls:
            return None
        for tc in tool_calls:
            func = getattr(tc, "function", None)
            if func is None or getattr(func, "name", None) != "load_skill":
                continue
            args_str = getattr(func, "arguments", "{}")
            if isinstance(args_str, str):
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    continue
            else:
                args = args_str or {}
            skill_name = args.get("name")
            if skill_name and skill_name in self._runner.catalog:
                # Stash reasoning on the instance so select() can emit it for the
                # pick it ultimately returns.
                self._last_reasoning = events.extract_reasoning(r)
                return skill_name
        return None

    async def select(self, task: str) -> str | None:
        """
        Two-tier selection (C2): run SELECT_ATTEMPTS independent zero-budget
        samples. If every non-None pick agrees, return it immediately. On
        disagreement, run one extra sample with a thinking budget to break the
        tie. Returns None when no sample produces a catalog-valid pick.
        """
        picks = [await self._sample_select(task, 0) for _ in range(SELECT_ATTEMPTS)]
        picks = [p for p in picks if p is not None]
        if not picks:
            return None
        if len(set(picks)) == 1:
            chosen = picks[0]
        else:
            chosen = await self._sample_select(task, 1024)
        if chosen is None:
            return None
        _log.info("selected skill: %s", chosen)
        await events.emit(
            "reasoning",
            node="route",
            summary=events.reasoning_summary(self._last_reasoning),
            text=self._last_reasoning,
        )
        return chosen
```

Note: `SELECT_ATTEMPTS = 3` and the `litellm`, `json`, `events`, `_log` imports are already present at module scope — do not duplicate them.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_skill_router.py::TestTwoTierSelect -v`
Expected: PASS

- [ ] **Step 5: Run the full skill_router suite for regressions**

Run: `pytest tests/services/orchestrator/test_skill_router.py -v`
Expected: PASS

If existing `select` tests assumed the old early-return-on-first-hit behavior (e.g. asserting exactly one acompletion call for a single matching sample), update them: unanimous selection now always makes exactly `SELECT_ATTEMPTS` (3) calls. Adjust any `assert mac.await_count == 1` in pre-existing `select` tests to `== 3`, and confirm the returned pick is unchanged.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/skill_router.py tests/services/orchestrator/test_skill_router.py
git commit -m "feat(orchestrator): two-tier skill selection with tiebreak sample (C2)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
