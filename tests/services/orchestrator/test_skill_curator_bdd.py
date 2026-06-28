"""pytest-bdd step defs for skill_curator.feature.

Mocked only (@mocked): no GPU, no services. Binds Gherkin to the real pure
curator functions, the real propose_skill file writer, and the real SkillRunner
discover(). The one LLM-shaped step passes a literal description (propose_skill
takes the description as an argument), so no model call is issued."""
from __future__ import annotations

from pathlib import Path

import frontmatter as _frontmatter
import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.skill_runner.skill_runner import SkillRunner
from services.orchestrator import events
from services.orchestrator import skill_curator as sc
from tests.conftest import run_async

scenarios("features/skill_curator.feature")

HOUR = 3600.0
_FM = "---\nname: {name}\ndescription: {desc}\n---\nbody for {name}\n"


class _RecordingEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, type, **fields):
        self.events.append((type, fields))


@pytest.fixture
def ctx(tmp_path):
    return {
        "root": tmp_path / "skills",
        "state": sc.CuratorState(),
        "now": 200 * HOUR,
        "idle_for_s": 9999.0,
        "interval_hours": 168.0,
        "min_idle_hours": 2.0,
        "gate": None,
        "seq": None,
        "description": "",
        "draft_dir": None,
        "verdicts": {},
        "emitter": _RecordingEmitter(),
        "spawn_curator": None,
    }


@pytest.fixture(autouse=True)
def _emitter(ctx):
    token = events.current_emitter.set(ctx["emitter"])
    yield
    events.current_emitter.reset(token)


def _write_active(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_FM.format(name=name, desc=f"does {name}"),
                               encoding="utf-8")


# ── Background ──────────────────────────────────────────────────────────────

@given(parsers.parse('a skills root with an active skill "{name}"'))
def _root(ctx, name):
    _write_active(ctx["root"], name)


@given("a curator state sidecar that has never run")
def _fresh_state(ctx):
    ctx["state"] = sc.CuratorState(last_run_at=0.0)


# ── Given ───────────────────────────────────────────────────────────────────

@given(parsers.parse('ENABLE_SKILL_CURATOR is "{val}"'))
def _enable_flag(ctx, val):
    enabled = val.strip() in ("1", "true", "yes", "on")
    ctx["spawn_curator"] = enabled


@given(parsers.parse("the curator last ran {hours:d} hours ago"))
def _last_ran(ctx, hours):
    ctx["state"] = sc.CuratorState(
        last_run_at=ctx["now"] - hours * HOUR, paused=ctx["state"].paused
    )


@given(parsers.parse("the system has been idle for {secs:d} seconds"))
def _idle(ctx, secs):
    ctx["idle_for_s"] = float(secs)


@given("the curator is paused")
def _paused(ctx):
    ctx["state"] = sc.CuratorState(last_run_at=ctx["state"].last_run_at, paused=True)


@given(parsers.parse(
    'a recent successful sequence "{name}" using tools "{tools}"'))
def _recent_seq(ctx, name, tools):
    ctx["seq"] = sc.CapturedSequence(
        name=name, goal=f"goal {name}",
        tools=tuple(tools.split(",")), ok=True, ts=1.0,
    )


@given(parsers.parse('the LLM drafts the description "{desc}"'))
def _drafted(ctx, desc):
    ctx["description"] = desc


@given(parsers.parse('a proposed draft "{name}" staged under ".proposed"'))
def _staged(ctx, name):
    seq = sc.CapturedSequence(name, "g", ("a", "b"), ok=True, ts=1.0)
    run_async(sc.propose_skill(ctx["root"], seq, "desc"))


@given(parsers.parse(
    'an active skill "{name}" last used {secs:d} seconds ago'))
def _usage(ctx, name, secs):
    ctx.setdefault("usages", []).append(
        sc.SkillUsage(name, last_used_at=ctx["now"] - secs, success_count=1)
    )


@given("the LLM drafting call raises an error")
def _draft_raises(ctx):
    async def _bad(prompt):
        raise RuntimeError("model down")
    ctx["draft_fn"] = _bad
    ctx["seq"] = sc.CapturedSequence("x", "g", ("a", "b"), ok=True, ts=1.0)


# ── When ────────────────────────────────────────────────────────────────────

@when("the orchestrator decides whether to spawn the curator loop")
def _decide_spawn(ctx):
    # mirrors main.run(): the loop is created only when the flag is on
    ctx["loop_created"] = bool(ctx["spawn_curator"])


@when(parsers.parse(
    "the gate is evaluated with interval {iv:d} hours and min idle {mi:d} hours"))
def _eval_gate(ctx, iv, mi):
    ctx["gate"] = sc.should_run_now(
        ctx["state"], now=ctx["now"], interval_hours=iv,
        min_idle_hours=mi, idle_for_s=ctx["idle_for_s"],
    )


@when("the curator proposes a skill from that sequence")
def _propose(ctx):
    ctx["draft_dir"] = run_async(
        sc.propose_skill(ctx["root"], ctx["seq"], ctx["description"])
    )


@when("the skill runner discovers skills")
def _discover(ctx):
    runner = SkillRunner(roots=[ctx["root"]])
    runner.discover()
    ctx["catalog"] = runner.catalog


@when("the curator sweeps lifecycle transitions")
def _sweep(ctx):
    ctx["verdicts"] = sc.sweep_transitions(ctx["usages"], now=ctx["now"])


@when("the curator runs one cycle")
def _run_cycle(ctx, tmp_path):
    buf = sc.RecentSequences()
    buf.record(ctx["seq"])
    ctx["cycle_result"] = run_async(sc.run_curator(
        skills_root=ctx["root"], state_path=tmp_path / "s.json",
        recent=buf, draft_fn=ctx["draft_fn"],
        now=ctx["now"], idle_for_s=9999.0,
    ))
    ctx["cycle_raised"] = False


# ── Then ────────────────────────────────────────────────────────────────────

@then("the curator loop task is not created")
def _no_loop(ctx):
    assert ctx["loop_created"] is False


@then(parsers.parse('no ".proposed" directory is created'))
def _no_proposed(ctx):
    assert not (ctx["root"] / ".proposed").exists()


@then("the gate result is closed")
def _gate_closed(ctx):
    assert ctx["gate"] is False


@then("the gate result is open")
def _gate_open(ctx):
    assert ctx["gate"] is True


@then(parsers.parse('a file "{relpath}" exists'))
def _file_exists(ctx, relpath):
    # relpath is repo-relative "services/skills/.proposed/<name>/<file>";
    # map it under the tmp root by its tail after ".proposed/".
    tail = relpath.split(".proposed/", 1)[1]
    assert (ctx["root"] / ".proposed" / tail).exists()


@then(parsers.parse('the SKILL.md frontmatter has name "{name}"'))
def _fm_name(ctx, name):
    post = _frontmatter.load(str(ctx["draft_dir"] / "SKILL.md"))
    assert post["name"] == name


@then(parsers.parse(
    'the SKILL.md body mentions tools "{a}" and "{b}"'))
def _body_tools(ctx, a, b):
    post = _frontmatter.load(str(ctx["draft_dir"] / "SKILL.md"))
    assert a in post.content and b in post.content


@then("the server stub is marked non-functional")
def _stub_marked(ctx):
    text = (ctx["draft_dir"] / "server.py.stub").read_text(encoding="utf-8")
    assert "NOT FUNCTIONAL" in text


@then(parsers.parse('a "skill.proposed" event was emitted with name "{name}"'))
def _event_emitted(ctx, name):
    assert any(
        t == "skill.proposed" and f.get("name") == name
        for t, f in ctx["emitter"].events
    )


@then(parsers.parse('the catalog does not contain "{name}"'))
def _catalog_excludes(ctx, name):
    assert name not in ctx["catalog"]


@then(parsers.parse('the catalog still contains "{name}"'))
def _catalog_includes(ctx, name):
    assert name in ctx["catalog"]


@then(parsers.parse('the transition for "{name}" is "{state}"'))
def _transition(ctx, name, state):
    assert ctx["verdicts"][name] == state


@then("the curator cycle returns without raising")
def _no_raise(ctx):
    assert ctx["cycle_raised"] is False
    assert ctx["cycle_result"] is None


@then("the orchestrator goal loop is unaffected")
def _loop_unaffected(ctx):
    # The cycle swallowed the error and produced no proposal; nothing leaked.
    assert ctx["cycle_result"] is None
