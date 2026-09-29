"""Council Router — 3 models plan in parallel, a critic merges, an executor builds step by step,
an executable verifier checks every step, failures retry with the error attached.
Benchmarked against the same executor model working alone.

    python council.py --mock        # simulated models, no API key
    python council.py               # real Claude models (needs ANTHROPIC_API_KEY)
"""
import argparse
import ast
import html
import json
import os
import random
import re
import secrets
import subprocess
import sys
import tempfile
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor

from tasks import TASKS

PLANNERS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
CRITIC = "claude-opus-5-5"
EXECUTOR = "claude-sonnet-5-5"  # baseline uses the same model alone, so the delta is the orchestration
PRICE = {"claude-opus-5-5": (4, 20), "claude-sonnet-5-5": (2, 10), "claude-haiku-4-5": (1, 5)}  # $ per 1M in/out
MAX_RETRIES = 2

PLAN = """You are planning how to implement a Python function.

Task: {spec}
Examples (must hold):
{examples}

Break the work into 2-4 ordered steps, simple to hard. For each step give a goal and 1-4 single-line
Python assert statements that verify it. Checks may only call the function. Think about edge cases
the examples do not cover. Reply with only JSON:
{{"steps": [{{"goal": "...", "checks": ["assert ..."]}}]}}"""

MERGE = """Three planners proposed plans for this Python task.

Task: {spec}
Examples (must hold):
{examples}

Plans:
{plans}

Merge them into one best plan. Keep the strongest checks, drop duplicates, and drop any check that
contradicts the task or examples (a wrong check is worse than no check). Order steps simple to hard.
Reply with only JSON in the same shape: {{"steps": [{{"goal": "...", "checks": ["assert ..."]}}]}}"""

STEP = """Implement this Python task.

Task: {spec}
Examples (must hold):
{examples}

Current code:
```python
{code}
```

Now do step {i}/{n}: {goal}
These checks must pass:
{checks}
{feedback}
Reply with the complete updated code in one ```python block."""

FEEDBACK = "\nYour previous attempt failed verification:\n{err}\nFind the root cause and fix it.\n"

BASELINE = """Implement this Python task.

Task: {spec}
Examples (must hold):
{examples}

Reply with the complete code in one ```python block."""


# ---------- models ----------

class Claude:
    def __init__(self):
        import anthropic
        self.client = anthropic.Anthropic()

    def __call__(self, model, prompt, role, task, retry=False):
        kw = {} if "haiku" in model else {"output_config": {"effort": "medium"}}  # effort isn't supported on Haiku 4.5
        r = self.client.messages.create(model=model, max_tokens=16000, messages=[{"role": "user", "content": prompt}], **kw)
        if r.stop_reason == "refusal":
            raise RuntimeError(f"{model} refused: {r.stop_details}")
        text = "".join(b.text for b in r.content if b.type == "text")
        return text, r.usage.input_tokens, r.usage.output_tokens


class Mock:
    """Deterministic stand-in. Planners find the task's edge case with a model-dependent chance, Haiku
    sometimes invents a wrong check, the critic usually keeps edges and drops wrong checks. The executor
    ships the task's known bug ~50% of the time first try, ~15% after verifier feedback.
    Numbers from --mock are SIMULATED."""

    INSIGHT = {"claude-opus-5-5": 0.7, "claude-sonnet-5-5": 0.5, "claude-haiku-4-5": 0.3}

    def __init__(self, seed):
        self.seed = seed

    def __call__(self, model, prompt, role, task, retry=False):
        rng = random.Random(f"{self.seed}|{task['fn']}|{model}|{role}|{len(prompt)}|{retry}")
        speed = {"claude-haiku-4-5": 0.5, "claude-sonnet-5-5": 0.8}.get(model, 1.2)
        time.sleep(rng.uniform(0.15, 0.35) * speed)
        if role in ("plan", "critic"):
            cases = task["public"]
            half = max(1, len(cases) // 2)
            edge = checks_for(task, [task["edge"]])[0]
            wrong = f"assert {call_src(task, task['edge'][0])} == None"
            if role == "plan":
                extra = [edge] if rng.random() < self.INSIGHT[model] else []
                extra += [wrong] if "haiku" in model and rng.random() < 0.5 else []
            else:
                extra = [edge] if edge in prompt and rng.random() < 0.9 else []
                extra += [wrong] if wrong in prompt and rng.random() < 0.3 else []
            out = json.dumps({"steps": [
                {"goal": "core behaviour", "checks": checks_for(task, cases[:half])},
                {"goal": "edge cases", "checks": checks_for(task, cases[half:]) + extra},
            ]})
        else:
            bug = rng.random() < (0.15 if retry else 0.5)
            out = f"```python\n{task['buggy'] if bug else task['ref']}\n```"
        return out, len(prompt) // 4, len(out) // 4


# ---------- helpers ----------

def call_src(task, args):
    return f"{task['fn']}({', '.join(map(repr, args))})"


def checks_for(task, cases):
    return [f"assert {call_src(task, a)} == {e!r}" for a, e in cases]


def examples_text(task):
    return "\n".join(f"{call_src(task, a)} == {e!r}" for a, e in task["public"])


def extract_code(text):
    blocks = re.findall(r"```[\w-]*[ \t]*\r?\n(.*?)```", text, re.S)
    return (max(blocks, key=len) if blocks else text).strip()  # longest block = the full file, not a snippet


def extract_plan(text):
    plan, dec = None, json.JSONDecoder()
    for m in re.finditer(r"\{", text):  # first '{' that starts a valid object with "steps"
        try:
            obj = dec.raw_decode(text, m.start())[0]
        except ValueError:
            continue
        if isinstance(obj, dict) and "steps" in obj:
            plan = obj
            break
    if plan is None:
        raise ValueError("no JSON plan found")
    steps = [{"goal": str(s["goal"]), "checks": [c for c in s["checks"] if is_check(c)]} for s in plan["steps"]]
    if not steps:
        raise ValueError("empty plan")
    return steps


def is_check(c):
    """Only real, compilable asserts: a bare `f(x) == 1` would pass without checking anything."""
    try:
        body = ast.parse(c).body if isinstance(c, str) else []
    except SyntaxError:
        return False
    return bool(body) and all(isinstance(n, ast.Assert) for n in body)


def verify(code, checks, timeout=10):
    """Run code + each check in a fresh interpreter. Returns (ok, failure report).
    Not a sandbox — it executes model-written code, so run it somewhere you trust."""
    body = "\n".join(
        f"try:\n{textwrap.indent(c, '    ')}\nexcept Exception as e:\n"
        f"    _f.append({c!r} + '  ->  ' + (repr(e) if str(e) else type(e).__name__))"
        for c in checks)
    done = secrets.token_hex(8)  # printed only after every check ran, so an early sys.exit(0) can't fake a pass
    src = f"{code}\n\n_f = []\n{body}\nif _f:\n    raise SystemExit('\\n'.join(_f))\nprint({done!r})\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(src)
    try:
        r = subprocess.run([sys.executable, "-I", f.name], capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0 and done in r.stdout
        return ok, "" if ok else (r.stderr.strip()[-1500:] or "exited before all checks ran")
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    finally:
        os.unlink(f.name)


def short(model):
    return model.replace("claude-", "")


# ---------- pipeline ----------

class Run:
    """Records every model call and verification as a timeline event."""

    def __init__(self, llm):
        self.llm, self.events, self.t0 = llm, [], time.perf_counter()

    def ask(self, model, role, prompt, task, retry=False, lane=None):
        t = time.perf_counter()
        text, tin, tout = self.llm(model, prompt, role, task, retry)
        pin, pout = PRICE[model]
        self.events.append({"lane": lane or role, "model": short(model), "t0": t - self.t0,
                            "t1": time.perf_counter() - self.t0, "cost": (tin * pin + tout * pout) / 1e6})
        return text

    def check(self, code, checks, lane="verify"):
        t = time.perf_counter()
        ok, err = verify(code, checks)
        self.events.append({"lane": lane, "model": "python", "t0": t - self.t0,
                            "t1": time.perf_counter() - self.t0, "ok": ok, "err": err, "cost": 0})
        return ok, err

    def note(self, text):
        now = time.perf_counter() - self.t0
        self.events.append({"lane": "note", "model": "", "t0": now, "t1": now, "err": text, "cost": 0})

    @property
    def cost(self):
        return sum(e["cost"] for e in self.events)

    @property
    def seconds(self):
        return max((e["t1"] for e in self.events), default=0)


def baseline(llm, task):
    run = Run(llm)
    code = extract_code(run.ask(EXECUTOR, "code", BASELINE.format(spec=task["spec"], examples=examples_text(task)), task))
    return code, run


def council(llm, task):
    run = Run(llm)
    ex = examples_text(task)
    public = checks_for(task, task["public"])

    # 1. three planners in parallel
    def plan(model):
        try:
            return extract_plan(run.ask(model, "plan", PLAN.format(spec=task["spec"], examples=ex), task,
                                        lane=f"plan {short(model)}"))
        except Exception as e:
            run.note(f"{short(model)} plan unusable: {e}")
            return None

    with ThreadPoolExecutor(len(PLANNERS)) as pool:
        plans = [p for p in pool.map(plan, PLANNERS) if p]

    # 2. critic merges; fall back to the first valid plan, then to a single public-examples step
    steps = None
    if plans:
        shown = "\n\n".join(f"Plan {i + 1}:\n{json.dumps(p, indent=1)}" for i, p in enumerate(plans))
        try:
            steps = extract_plan(run.ask(CRITIC, "critic", MERGE.format(spec=task["spec"], examples=ex, plans=shown), task))
        except Exception as e:
            run.note(f"critic output unusable: {e}")
            steps = plans[0]
    steps = steps or [{"goal": "make the examples pass", "checks": []}]

    # 3. execute step by step; every step must keep all earlier checks green
    code, kept = "# nothing yet", []
    for i, step in enumerate(steps, 1):
        checks, feedback = kept + step["checks"], ""
        for attempt in range(MAX_RETRIES + 1):
            prompt = STEP.format(spec=task["spec"], examples=ex, code=code, i=i, n=len(steps), goal=step["goal"],
                                 checks="\n".join(checks) or "(examples only)", feedback=feedback)
            attempt_code = extract_code(run.ask(EXECUTOR, "code", prompt, task, retry=attempt > 0))
            ok, err = run.check(attempt_code, public + checks)
            if ok:
                code, kept = attempt_code, checks
                break
            feedback = FEEDBACK.format(err=err)
        else:
            # the step's own checks may be wrong (planners hallucinate too): quarantine them and
            # keep the attempt only if it still satisfies everything already trusted
            run.note(f"step {i} checks quarantined after {MAX_RETRIES + 1} failed attempts")
            if run.check(attempt_code, public + kept, lane="verify")[0]:
                code = attempt_code
    return code, run


def safe_solve(llm, task):
    """One task's API failure shouldn't throw away every other task's results."""
    try:
        return solve(llm, task)
    except Exception as e:
        crash = {"lane": "note", "model": "", "t0": 0, "t1": 0, "cost": 0, "err": f"run crashed: {e!r}"}
        return {"task": task["fn"], "baseline": False, "council": False, "retries": 0, "b_cost": 0, "c_cost": 0,
                "b_s": 0, "c_s": 0, "events": [crash], "b_events": [], "code": "", "error": repr(e)}


def grade(code, task):
    return verify(code, checks_for(task, task["public"] + task["hidden"]))[0]


def solve(llm, task):
    b_code, b_run = baseline(llm, task)
    c_code, c_run = council(llm, task)
    return {"task": task["fn"], "baseline": grade(b_code, task), "council": grade(c_code, task),
            "retries": sum(1 for e in c_run.events if e["lane"] == "verify" and not e.get("ok")),
            "b_cost": b_run.cost, "c_cost": c_run.cost, "b_s": b_run.seconds, "c_s": c_run.seconds,
            "events": c_run.events, "b_events": b_run.events, "code": c_code}


# ---------- report ----------

LANE_COLOR = {"plan": "#5b8def", "critic": "#a371f7", "code": "#e3a008", "verify": "#2ea043", "note": "#8b949e"}


def timeline(events, span):
    lanes = list(dict.fromkeys(e["lane"] for e in events if e["lane"] != "note"))
    rows = []
    for lane in lanes:
        bars = []
        for e in (e for e in events if e["lane"] == lane):
            color = "#da3633" if e.get("ok") is False else LANE_COLOR[lane.split()[0]]
            left, width = 100 * e["t0"] / span, max(0.6, 100 * (e["t1"] - e["t0"]) / span)
            tip = html.escape(f"{e['model']}  {e['t1'] - e['t0']:.2f}s" + (f"\n{e['err']}" if e.get("err") else ""))
            bars.append(f'<i style="left:{left:.2f}%;width:{width:.2f}%;background:{color}" title="{tip}"></i>')
        rows.append(f'<div class="lane"><span>{html.escape(lane)}</span><div class="track">{"".join(bars)}</div></div>')
    notes = "".join(f'<div class="note">⚠ {html.escape(e["err"])}</div>' for e in events if e["lane"] == "note")
    return "".join(rows) + notes


def report(rows, mode):
    n = len(rows)
    b = sum(r["baseline"] for r in rows)
    c = sum(r["council"] for r in rows)
    bc, cc = sum(r["b_cost"] for r in rows), sum(r["c_cost"] for r in rows)
    fixed = sum(r["council"] and not r["baseline"] for r in rows)
    broke = sum(r["baseline"] and not r["council"] for r in rows)
    mark = lambda ok: '<b class="ok">PASS</b>' if ok else '<b class="bad">FAIL</b>'
    cards = "".join(f"""
<section class="task">
  <header><h3>{r['task']}</h3><div>baseline {mark(r['baseline'])} &nbsp; council {mark(r['council'])}
  &nbsp; <small>{r['retries']} failed verifications · ${r['c_cost']:.4f} · {r['c_s']:.1f}s</small></div></header>
  {timeline(r['events'], max(r['c_s'], 0.01))}
  <details><summary>final code</summary><pre>{html.escape(r['code'])}</pre></details>
</section>""" for r in rows)
    banner = ('<p class="sim">SIMULATED RUN (--mock): models are stand-ins that replay known bugs. '
              'Run without --mock and with an API key for real numbers.</p>') if mode == "mock" else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Council Router Report</title><style>
:root{{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--card:#161b22;--line:#30363d}}
@media (prefers-color-scheme:light){{:root{{--bg:#fff;--fg:#1f2328;--mut:#59636e;--card:#f6f8fa;--line:#d1d9e0}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;max-width:1000px;margin:0 auto;padding:24px 16px}}
h1{{margin:0 0 4px}} .mut{{color:var(--mut)}} .sim{{background:#9e6a0322;border:1px solid #9e6a03;padding:8px 12px;border-radius:6px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}} .stat b{{font-size:26px;display:block}}
.task{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:12px 0}}
.task header{{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;align-items:center}} h3{{margin:0;font-family:ui-monospace,monospace}}
.ok{{color:#2ea043}} .bad{{color:#da3633}}
.lane{{display:flex;align-items:center;gap:8px;margin:4px 0}} .lane span{{width:130px;flex:none;color:var(--mut);font:12px ui-monospace,monospace}}
.track{{position:relative;height:14px;flex:1;background:var(--bg);border-radius:3px}} .track i{{position:absolute;top:0;bottom:0;border-radius:3px}}
.note{{color:var(--mut);font-size:12px;margin-top:4px}} pre{{overflow:auto;background:var(--bg);padding:10px;border-radius:6px}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px}}
</style></head><body>
<h1>Council Router</h1><p class="mut">3 planners → critic → step executor ⇄ verifier. Baseline = {short(EXECUTOR)} alone. Graded on hidden tests.</p>
{banner}
<div class="stats">
<div class="stat"><b>{b}/{n}</b>baseline pass</div>
<div class="stat"><b class="ok">{c}/{n}</b>council pass</div>
<div class="stat"><b>{fixed} / {broke}</b>fixed / broken by council</div>
<div class="stat"><b>${bc:.3f} → ${cc:.3f}</b>total cost</div>
</div>
<p class="legend mut"><i style="background:#5b8def"></i>plan<i style="background:#a371f7"></i>critic<i style="background:#e3a008"></i>code
<i style="background:#2ea043"></i>verify pass<i style="background:#da3633"></i>verify fail — hover bars for details</p>
{cards}</body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", action="store_true", help="simulated models, no API key needed")
    ap.add_argument("--seed", type=int, default=7, help="mock randomness seed")
    ap.add_argument("--only", help="comma-separated task names")
    ap.add_argument("--workers", type=int, default=4, help="tasks run in parallel")
    ap.add_argument("--out", default="report.html")
    a = ap.parse_args()

    tasks = [t for t in TASKS if not a.only or t["fn"] in a.only.split(",")]
    llm = Mock(a.seed) if a.mock else Claude()
    with ThreadPoolExecutor(a.workers) as pool:
        rows = list(pool.map(lambda t: safe_solve(llm, t), tasks))

    yes = lambda ok: "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"\n{'task':<16}{'baseline':<10}{'council':<10}retries   cost")
    for r in rows:
        print(f"{r['task']:<16}{yes(r['baseline']):<19}{yes(r['council']):<19}{r['retries']:<10}${r['b_cost']:.4f} -> ${r['c_cost']:.4f}")
    n = len(rows)
    b, c = sum(r["baseline"] for r in rows), sum(r["council"] for r in rows)
    print(f"\nbaseline {b}/{n} ({100 * b / n:.0f}%)  ->  council {c}/{n} ({100 * c / n:.0f}%)"
          + ("   [SIMULATED]" if a.mock else ""))

    with open(a.out, "w", encoding="utf-8") as f:
        f.write(report(rows, "mock" if a.mock else "live"))
    with open(os.path.splitext(a.out)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)
    print(f"report -> {a.out}")


if __name__ == "__main__":
    main()
