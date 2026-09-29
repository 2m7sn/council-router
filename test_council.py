from council import Mock, checks_for, extract_code, extract_plan, grade, solve, verify
from tasks import TASKS


def test_task_data_is_sound():
    for t in TASKS:
        assert grade(t["ref"], t), f"{t['fn']}: reference fails its own tests"
        assert not grade(t["buggy"], t), f"{t['fn']}: bug is never caught"
        assert verify(t["ref"], checks_for(t, [t["edge"]]))[0], f"{t['fn']}: edge case wrong"
        assert not verify(t["buggy"], checks_for(t, [t["edge"]]))[0], f"{t['fn']}: edge misses the bug"


def test_verifier_reports_the_failing_check():
    t = TASKS[0]
    ok, err = verify(t["buggy"], checks_for(t, t["public"]))
    assert not ok and "IV" in err


def test_verifier_survives_crashes_hangs_and_cheats():
    assert not verify("raise ValueError('boom')", [])[0]
    assert verify("while True: pass", [], timeout=1) == (False, "timed out after 1s")
    assert not verify("import sys; sys.exit(0)", ["assert 1 == 2"])[0]


def test_plan_parsing_keeps_only_real_checks():
    text = 'Sure {thing}. ```json\n{"steps": [{"goal": "g", "checks": ["assert f(1) == 1", "f(1) == 2", "assert f( == 1", "assertEqual(f(1), 1)"]}]}\n```'
    assert extract_plan(text) == [{"goal": "g", "checks": ["assert f(1) == 1"]}]
    assert extract_code("snippet ```py\na = 1\n``` full ```python\na = 1\nb = 2\n```") == "a = 1\nb = 2"


def test_mock_pipeline_end_to_end():
    r = solve(Mock(seed=1), TASKS[0])
    lanes = {e["lane"] for e in r["events"]}
    assert {"plan opus-5-5", "plan sonnet-5-5", "plan haiku-4-5", "critic", "code", "verify"} <= lanes
    assert r["c_cost"] > r["b_cost"] > 0
