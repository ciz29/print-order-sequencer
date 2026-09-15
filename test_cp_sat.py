"""
Adversarial test suite for scheduler_cp_sat.py.

Nine kinds of check:

1. BRUTE FORCE vs CP-SAT. For small random instances, enumerate every
   possible machine assignment and every sequence on every machine, and
   confirm CP-SAT's answer equals the true minimum. This is the real proof
   that the model says what we think it says - the independent replay in
   verify() only proves the reported schedule is self-consistent, not that
   it is optimal.

2. EDGE CASES designed to break it: ties everywhere, exact capacity
   boundaries, zero screens, zero quantity, a single job, every job
   infeasible, an empty file, duplicate job ids, whitespace and casing.

3. REGRESSIONS for the silent bugs found so far - each returned a confident
   wrong answer rather than an error: lateness measured in seconds instead
   of days, lateness variables bounded too tightly, a solver timeout
   mistaken for proven infeasibility, a deadline computed as due_date + 24h
   instead of end-of-due-DATE, a job sequence inferred from start times
   (which ties), and unusable input accepted silently.

4. THE SOLUTION HINT is advisory, not a constraint - checked against brute
   force hinted and unhinted, plus a deliberately bad hint.

5. THE MAKESPAN CAP equals the greedy's makespan exactly, ties included.

6. UNDERBASE IS EXPLICIT DATA: the `underbase` column (White / Grey /
   Black) names the real screens. Black parses and costs like the others, a
   misspelling is rejected rather than silently becoming a screen, and
   ambiguous input - force_on naming no screens, or an override alongside a
   filled column - is FLAGGED for a human, never resolved by guessing.

7. UNDERBASE SCREEN REUSE happens through the ORDINARY screen mechanism,
   the same set difference that reuses ink colours. No underbase-specific
   reuse logic exists.

8. THE TWO DEMO FILES, each checked for the specific behaviour it was built
   to show: reuse_demo.csv (one UB_white screen's worth of reuse saved) and
   review_flags_demo.csv (both manual-review flags fire, and both jobs are
   still scheduled). Both are hand-crafted illustrations, NOT real orders,
   and are kept out of REAL_CSVS for that reason.

9. DETERMINISM. Same input twice, same answer.

Run: .venv/bin/python test_cp_sat.py
"""

import csv
import itertools
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta

from scheduler import (MACHINE_MAX_SCREENS, MACHINE_UNITS_PER_HOUR,
                       SETUP_MINUTES_PER_SCREEN, UNDERBASE_OVERRIDE_VALUES,
                       _annotate_underbase, _underbase_screen,
                       changeover_minutes, load_jobs)
from scheduler_cp_sat import (INITIAL_LOADED_SCREENS, MACHINES,
                              deadline_seconds, eligible_machines,
                              greedy_reference, late_jobs, machines_used,
                              run_seconds, solve, underbase_review_jobs,
                              validate_jobs, verify)

START = datetime(2026, 8, 25, 8, 0)
FAR = datetime(2030, 1, 1)          # a due date that can never bind
REAL_CSVS = ["sample_jobs.csv", "messy_jobs.csv",
             "real_po_anonymized_jobs.csv"]
# Hand-crafted illustrative fixtures, NOT real orders. Deliberately separate
# from REAL_CSVS - see the per-file notes in test_demo_files().
DEMO_CSVS = ["reuse_demo.csv", "review_flags_demo.csv"]

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


def job(job_id, screens, quantity=100, due=FAR, po="PO"):
    return {"job_id": job_id, "po_id": po, "due_date": due,
            "quantity": quantity, "screens": set(screens)}


# ---------------------------------------------------------------------------
# 1. Brute force
# ---------------------------------------------------------------------------

def brute_force(jobs, start_time, cap_seconds=None):
    """True minimum total changeover, by exhaustive enumeration.

    Due dates are assumed non-binding (callers use FAR). Because each
    machine's changeover cost and its finish time depend only on its own
    subset and ordering, and the makespan cap is `max(finish) <= cap`, each
    machine can be optimised independently once the assignment is fixed."""
    per_machine_best = {}

    def best_for(machine, subset):
        """(min changeover, ...) over all orderings of `subset` on `machine`,
        subject to the machine's own finish time respecting the cap."""
        key = (machine, subset)
        if key in per_machine_best:
            return per_machine_best[key]
        best = None
        for order in itertools.permutations(subset):
            loaded = set(INITIAL_LOADED_SCREENS[machine])
            cost, clock = 0, 0
            for i in order:
                setup = changeover_minutes(loaded, jobs[i]["screens"])
                cost += setup
                clock += setup * 60 + run_seconds(jobs[i], machine)
                loaded = set(jobs[i]["screens"])
            if cap_seconds is not None and clock > cap_seconds:
                continue
            if best is None or cost < best:
                best = cost
        per_machine_best[key] = best
        return best

    best_total = None
    for assignment in itertools.product(*[eligible_machines(j) for j in jobs]):
        groups = {}
        for i, m in enumerate(assignment):
            groups.setdefault(m, []).append(i)
        total = 0
        for m, subset in groups.items():
            b = best_for(m, tuple(subset))
            if b is None:
                total = None
                break
            total += b
        if total is not None and (best_total is None or total < best_total):
            best_total = total
    return best_total


def random_instance(rng, n_jobs, palette, max_screens):
    jobs = []
    for k in range(n_jobs):
        size = rng.randint(0, max_screens)
        jobs.append(job(f"R{k}", rng.sample(palette, size),
                        quantity=rng.choice([50, 100, 200, 400])))
    return jobs


def test_brute_force():
    print("\n[1] CP-SAT optimum vs exhaustive enumeration")
    rng = random.Random(20260827)
    palette = ["c1", "c2", "c3", "c4", "c5", "UB_white", "UB_grey"]

    mismatches = 0
    for trial in range(25):
        n = rng.randint(2, 5)
        jobs = random_instance(rng, n, palette, max_screens=4)

        truth = brute_force(jobs, START)
        result = solve(jobs, START, makespan_cap_seconds=None, time_limit=30)
        if result is None:
            check(f"trial {trial}: solver returned a schedule", False)
            mismatches += 1
            continue
        got = result["total_changeover"]
        if got != truth or result["status"] != "OPTIMAL" or verify(result, START):
            mismatches += 1
            check(f"trial {trial} (n={n})", False,
                  f"brute force {truth}, CP-SAT {got}, status "
                  f"{result['status']}, verify {verify(result, START)}")
    check(f"25 random instances match the exhaustive optimum", mismatches == 0,
          f"{mismatches} mismatch(es)")


def test_brute_force_capped():
    print("\n[2] CP-SAT optimum vs enumeration, WITH a makespan cap")
    rng = random.Random(7)
    palette = ["c1", "c2", "c3", "c4", "UB_white"]
    mismatches = 0
    for trial in range(12):
        n = rng.randint(3, 5)
        jobs = random_instance(rng, n, palette, max_screens=3)
        # A cap tight enough to bite but loose enough to stay feasible.
        loosest = sum(max(run_seconds(j, m) for m in MACHINES) for j in jobs)
        cap = max(loosest // 3, max(run_seconds(j, m) for j in jobs for m in MACHINES)
                  + 60 * SETUP_MINUTES_PER_SCREEN * 5)

        truth = brute_force(jobs, START, cap_seconds=cap)
        result = solve(jobs, START, makespan_cap_seconds=cap, time_limit=30)
        if result is None or truth is None:
            check(f"capped trial {trial}", False, f"truth={truth} result={result}")
            mismatches += 1
            continue
        problems = verify(result, START)
        if result["total_changeover"] != truth or problems:
            mismatches += 1
            check(f"capped trial {trial} (n={n}, cap={cap}s)", False,
                  f"brute force {truth}, CP-SAT {result['total_changeover']}, "
                  f"verify {problems}")
    check("12 capped random instances match the exhaustive optimum", mismatches == 0,
          f"{mismatches} mismatch(es)")


# ---------------------------------------------------------------------------
# 3. Edge cases
# ---------------------------------------------------------------------------

def solve_and_verify(name, jobs, cap=None, expect_none=False):
    result = solve(jobs, START, makespan_cap_seconds=cap, time_limit=30)
    if expect_none:
        check(name, result is None, f"expected no solution, got {result}")
        return None
    if result is None:
        check(name, False, "solver returned None")
        return None
    problems = verify(result, START)
    check(name, not problems, "; ".join(problems))
    return result


def test_edge_cases():
    print("\n[3] Edge cases")

    # --- a single job -------------------------------------------------------
    r = solve_and_verify("single job, 3 screens -> 15 min",
                         [job("only", ["a", "b", "c"])])
    if r:
        check("  single job changeover == 15", r["total_changeover"] == 15,
              str(r["total_changeover"]))

    # --- a job with no screens at all (messy_jobs K19) ----------------------
    r = solve_and_verify("zero-screen job costs 0", [job("empty", [])])
    if r:
        check("  zero-screen changeover == 0", r["total_changeover"] == 0,
              str(r["total_changeover"]))

    # --- zero quantity ------------------------------------------------------
    r = solve_and_verify("zero-quantity job (0-second run)",
                         [job("zeroqty", ["a"], quantity=0)])

    # --- every job identical: total should be one setup, rest free ----------
    ident = [job(f"S{k}", ["a", "b", "c", "d"]) for k in range(6)]
    r = solve_and_verify("6 identical jobs -> ONE setup total", ident)
    if r:
        check("  6 identical jobs cost 20 min (one setup)",
              r["total_changeover"] == 20, str(r["total_changeover"]))
        used = sum(1 for m in MACHINES if r["sequences"][m])
        check("  ...and they land on a single machine", used == 1, f"used {used}")

    # --- total tie: every job distinct, nothing shareable -------------------
    distinct = [job(f"D{k}", [f"x{k}"]) for k in range(6)]
    r = solve_and_verify("6 jobs sharing nothing -> 5 min each, any layout", distinct)
    if r:
        check("  unshareable jobs cost 30 min however they are placed",
              r["total_changeover"] == 30, str(r["total_changeover"]))

    # --- exact capacity boundary -------------------------------------------
    r = solve_and_verify("12 screens: fits only M5 (cap 12)",
                         [job("cap12", [f"s{k}" for k in range(12)])])
    if r:
        check("  12-screen job placed on M5", bool(r["sequences"]["M5"]),
              str({m: [s["job_id"] for s in r["sequences"][m]] for m in MACHINES}))

    r = solve_and_verify("4 screens on a queue that must use M3 (cap 4)",
                         [job(f"C{k}", [f"s{i}" for i in range(4)]) for k in range(2)])

    # --- 13 screens: fits nothing ------------------------------------------
    r = solve_and_verify("13 screens: no machine can hold it",
                         [job("cap13", [f"s{k}" for k in range(13)])])
    if r:
        check("  13-screen job flagged, not scheduled",
              [j["job_id"] for j in r["infeasible_capacity"]] == ["cap13"]
              and r["total_changeover"] == 0,
              str(r["infeasible_capacity"]))

    # --- EVERY job infeasible ----------------------------------------------
    allbad = [job(f"B{k}", [f"s{i}" for i in range(13 + k)]) for k in range(3)]
    r = solve_and_verify("every job exceeds every machine", allbad)
    if r:
        check("  all 3 flagged, nothing scheduled",
              len(r["infeasible_capacity"]) == 3 and r["status"] == "EMPTY",
              f"{r['infeasible_capacity']} {r['status']}")

    # --- no jobs at all -----------------------------------------------------
    r = solve_and_verify("empty job list", [])
    if r:
        check("  empty input -> empty schedule, 0 changeover",
              r["total_changeover"] == 0 and not any(r["sequences"].values()))

    # --- impossible due date, must relax not crash --------------------------
    past = [job("late1", ["a", "b"], due=datetime(2020, 1, 1)),
            job("late2", ["a", "b"], due=datetime(2020, 1, 1))]
    r = solve_and_verify("due dates already in the past -> relax, don't crash", past)
    if r:
        check("  relaxation reported, both jobs flagged late",
              r["due_dates"] == "relaxed" and len(late_jobs(r, START)) == 2,
              f"{r['due_dates']} {late_jobs(r, START)}")
        check("  ...and shared screens still exploited (10 min, not 20)",
              r["total_changeover"] == 10, str(r["total_changeover"]))

    # --- due date so tight nothing can meet it, mixed with feasible jobs ----
    mixed = [job("tight", ["a"], quantity=100000, due=datetime(2026, 8, 25)),
             job("easy", ["a"], quantity=10, due=FAR)]
    r = solve_and_verify("one unmeetable job among feasible ones", mixed)
    if r:
        ids = [x[0] for x in late_jobs(r, START)]
        check("  only the unmeetable job is flagged late", ids == ["tight"], str(ids))

    # --- a due date that is exactly the start day (day-granular boundary) ---
    sameday = [job("today", ["a"], quantity=100, due=datetime(2026, 8, 25))]
    r = solve_and_verify("due today, finishes today -> on time", sameday)
    if r:
        check("  same-day due date is met", not late_jobs(r, START),
              str(late_jobs(r, START)))

    # --- makespan cap tight enough to force spreading -----------------------
    # 4 x 500 units of the same 3 screens. All four on the fastest machine
    # (M3, 435/h) would take 4.85h, so a 4h cap has to split them - even
    # though sharing one machine is what minimises changeover.
    spread = [job(f"P{k}", ["a", "b", "c"], quantity=500) for k in range(4)]
    r = solve_and_verify("binding cap forces parallel machines", spread,
                         cap=4 * 3600)
    if r:
        used = sum(1 for m in MACHINES if r["sequences"][m])
        check("  cap actually applied, not dropped", r["cap_applied"] == 4 * 3600,
              f"cap_applied={r['cap_applied']} notes={r['notes']}")
        check("  cap respected and jobs spread out",
              r["makespan_seconds"] <= 4 * 3600 and used > 1,
              f"makespan {r['makespan_seconds']}s on {used} machines")
        check("  ...and the extra machine costs exactly one extra setup",
              r["total_changeover"] == 30, str(r["total_changeover"]))

    # --- an infeasibly tight cap must fall back, not return None ------------
    r = solve_and_verify("absurdly tight cap falls back to uncapped", spread, cap=1)
    if r:
        check("  fallback noted in the result",
              r["cap_applied"] is None and any("cap" in n for n in r["notes"]),
              f"cap_applied={r['cap_applied']} notes={r['notes']}")


def test_regressions():
    """Bugs that real runs and probing surfaced. Every one of them was silent -
    the program returned a confident wrong answer rather than an error."""
    print("\n[4] Regressions")

    # BUG 5: the per-machine sequence used to be inferred by sorting jobs on
    # their start time. Zero-length runs (quantity 0) tie on start time, so the
    # inferred order disagreed with the solver's real sequence and the verifier
    # reported bogus per-job changeover mismatches. The order is now read back
    # off the circuit arcs the solver actually chose.
    twelve = [f"s{k}" for k in range(12)]   # 12 screens => only M5 fits
    tied = [job(c, twelve, quantity=0) for c in "ABC"]
    r = solve(tied, START, time_limit=20)
    check("zero-length jobs that tie on start time still verify",
          r is not None and not verify(r, START),
          "; ".join(verify(r, START)) if r else "no result")
    if r:
        check("  ...and cost exactly one setup between them",
              r["total_changeover"] == 60, str(r["total_changeover"]))
        seq = [s["job_id"] for s in r["sequences"]["M5"]]
        first_setup = r["sequences"]["M5"][0]["changeover_minutes"] if seq else None
        check("  ...with the full setup charged to whichever job is FIRST",
              first_setup == 60, f"sequence {seq}, first setup {first_setup}")

    # BUG 6: a negative quantity made the model infeasible for no stated
    # reason; solve() returned None and callers died with a TypeError.
    for bad, label in [([job("neg", ["a"], quantity=-500)], "negative quantity"),
                       ([job("D", ["a"]), job("D", ["b"])], "duplicate job_id")]:
        try:
            solve(bad, START, time_limit=10)
            check(f"{label} is rejected with a clear error", False,
                  "no exception raised")
        except ValueError as e:
            check(f"{label} is rejected with a clear error", True)
            print(f"        ({e})")

    # BUG 1: deadline_seconds() used due_date + 24h, so a due date carrying a

    # BUG 4: deadline_seconds() used due_date + 24h, so a due date carrying a
    # time-of-day put the deadline on the NEXT calendar day. The hard
    # constraint then accepted jobs the lateness report called late.
    midnight = job("m", ["a"], due=datetime(2026, 8, 26, 0, 0))
    afternoon = job("a", ["a"], due=datetime(2026, 8, 26, 17, 30))
    check("deadline ignores the due date's time-of-day",
          deadline_seconds(midnight, START) == deadline_seconds(afternoon, START),
          f"{deadline_seconds(midnight, START)} vs {deadline_seconds(afternoon, START)}")

    # A job due 08/26 08:00 that cannot finish until 08/27 must be reported
    # late, not quietly accepted because 08/27 07:59 is "within 24h".
    overnight = [job("overnight", ["a"], quantity=6000,
                     due=datetime(2026, 8, 26, 8, 0))]
    r = solve(overnight, START, time_limit=20)
    consistent = r is not None and (
        (r["due_dates"] == "hard" and not late_jobs(r, START))
        or (r["due_dates"] == "relaxed" and late_jobs(r, START)))
    check("time-of-day due date: constraint and report agree", consistent,
          f"due_dates={r['due_dates'] if r else None} late={late_jobs(r, START) if r else None}")
    if r:
        check("  ...and the schedule still verifies", not verify(r, START),
              "; ".join(verify(r, START)))

    # BUG 3: a solver TIMEOUT (UNKNOWN) was treated as "due dates are
    # infeasible", so the model relaxed constraints it had merely run out of
    # time to satisfy and reported late jobs the greedy met comfortably.
    # With a deliberately tiny time limit the answer must be a real on-time
    # schedule or an honest TIMEOUT - never invented lateness.
    rng = random.Random(99)
    palette = [f"c{i}" for i in range(12)] + ["UB_white", "UB_grey"]
    fake_lateness = 0
    for trial in range(6):
        jobs = [job(f"T{k}", rng.sample(palette, rng.randint(1, 7)),
                    quantity=rng.randint(50, 600),
                    due=START + timedelta(days=rng.randint(5, 20)))
                for k in range(35)]
        greedy = greedy_reference(jobs, START)
        if greedy["late"]:
            continue  # only meaningful when the greedy IS on time
        r = solve(jobs, START,
                  makespan_cap_seconds=int(greedy["makespan_seconds"]) + 60,
                  time_limit=0.05, hint=greedy["hint"])
        if r is None:
            continue
        if r["status"] != "TIMEOUT" and late_jobs(r, START):
            fake_lateness += 1
    check("a solver timeout never invents late jobs", fake_lateness == 0,
          f"{fake_lateness} run(s) reported lateness the greedy did not have")

    # And a timeout must be labelled as one, not passed off as a schedule.
    jobs = [job(f"U{k}", rng.sample(palette, rng.randint(1, 7)), quantity=300,
                due=START + timedelta(days=10)) for k in range(45)]
    r = solve(jobs, START, time_limit=0.01)
    check("an unsolved model reports TIMEOUT, not a fake schedule",
          r is None or r["status"] == "TIMEOUT" or not verify(r, START),
          f"status={r['status'] if r else None}")


def test_hint_does_not_constrain():
    """The greedy schedule is fed to CP-SAT via add_hint(). A hint is meant to
    be advisory - a starting point, not a restriction. If it were accidentally
    acting as a constraint, the hinted optimum would sometimes be WORSE than
    the unhinted one, and both would sometimes be worse than brute force.

    Checked directly rather than argued from the docs: same instance, solved
    hinted and unhinted, both compared against exhaustive enumeration."""
    print("\n[5] The solution hint is advisory, not a constraint")
    rng = random.Random(4242)
    palette = ["c1", "c2", "c3", "c4", "c5", "UB_white"]

    disagreements, hint_worse = 0, 0
    for _ in range(15):
        jobs = random_instance(rng, rng.randint(3, 5), palette, max_screens=4)
        truth = brute_force(jobs, START)
        greedy = greedy_reference(jobs, START)

        hinted = solve(jobs, START, time_limit=30, hint=greedy["hint"])
        plain = solve(jobs, START, time_limit=30)
        if hinted["total_changeover"] != truth or plain["total_changeover"] != truth:
            disagreements += 1
        if hinted["total_changeover"] > plain["total_changeover"]:
            hint_worse += 1

    check("hinted and unhinted both reach the exhaustive optimum",
          disagreements == 0, f"{disagreements} instance(s) disagreed")
    check("the hint never makes the result worse", hint_worse == 0,
          f"{hint_worse} instance(s) got worse with the hint")

    # A deliberately BAD hint (everything crammed onto one machine) must not
    # stop the solver reaching the true optimum either.
    jobs = [job(f"H{k}", ["a", "b"] if k % 2 else ["c", "d"], quantity=400)
            for k in range(5)]
    truth = brute_force(jobs, START)
    bad_hint = {j["job_id"]: "M3" for j in jobs}
    r = solve(jobs, START, time_limit=30, hint=bad_hint)
    check("a deliberately bad hint still reaches the optimum",
          r["total_changeover"] == truth and r["status"] == "OPTIMAL",
          f"hinted {r['total_changeover']} vs optimum {truth}, {r['status']}")

    # A hint naming a machine the job cannot fit on must be ignored, not crash.
    jobs = [job("big", [f"s{k}" for k in range(12)])]   # only M5 holds 12
    r = solve(jobs, START, time_limit=20, hint={"big": "M3"})
    check("a hint pointing at an ineligible machine is ignored safely",
          r is not None and bool(r["sequences"]["M5"]) and not verify(r, START),
          f"{r['status'] if r else None}")


def test_makespan_cap_is_exact():
    """The cap must equal the greedy's makespan measured the same way the model
    measures it. The greedy works in floats and the model in whole seconds, so
    the cap used to carry 60s of rounding slack - meaning 'never slower than
    the greedy' was really 'never more than a minute slower'. The cap is now
    derived by replaying the greedy schedule through the model's own integer
    arithmetic, so it is exact and the greedy always satisfies it."""
    print("\n[6] The makespan cap is exactly the greedy's makespan")

    for path in REAL_CSVS:
        jobs = load_jobs(path)
        greedy = greedy_reference(jobs, START)
        cap = greedy["makespan_seconds"]

        check(f"{path}: cap is a whole number of seconds", isinstance(cap, int),
              f"{cap!r}")
        check(f"{path}: Phase 1's own changeover total matches the replay",
              greedy["total_changeover"] == greedy["changeover_as_phase1_reported"],
              f"{greedy['total_changeover']} vs {greedy['changeover_as_phase1_reported']}")

        r = solve(jobs, START, makespan_cap_seconds=cap, time_limit=60,
                  hint=greedy["hint"])
        # The greedy schedule satisfies the cap exactly, so the cap can never
        # be the reason a solution is not found.
        check(f"{path}: capped solve stays capped (never dropped)",
              r["cap_applied"] == cap, f"cap_applied={r['cap_applied']} cap={cap}")
        check(f"{path}: CP-SAT makespan <= greedy makespan, exactly",
              r["makespan_seconds"] <= cap,
              f"{r['makespan_seconds']} > {cap}")

    # Tie case: several machines finishing at the identical moment. The cap is
    # max(finish), so ties must not inflate or deflate it.
    tied = [job(f"E{k}", [f"s{k}"], quantity=600) for k in range(6)]
    greedy = greedy_reference(tied, START)
    replay_max = 0
    for m in MACHINES:
        clock, loaded = 0, set(INITIAL_LOADED_SCREENS[m])
        for step in greedy["machines"][m]["run"]:
            j = next(x for x in tied if x["job_id"] == step["job_id"])
            clock += changeover_minutes(loaded, j["screens"]) * 60 + run_seconds(j, m)
            loaded = set(j["screens"])
        replay_max = max(replay_max, clock)
    check("tied machine finish times: cap == max over machines",
          greedy["makespan_seconds"] == replay_max,
          f"{greedy['makespan_seconds']} vs {replay_max}")
    r = solve(tied, START, makespan_cap_seconds=greedy["makespan_seconds"],
              time_limit=30, hint=greedy["hint"])
    check("  ...and the tied instance solves under that exact cap",
          r["cap_applied"] == greedy["makespan_seconds"] and not verify(r, START),
          f"cap_applied={r['cap_applied']}")


def test_underbase_is_explicit():
    """Underbase is explicit, per-order, human-entered data via the
    `underbase` column. There is no inference left to test - what matters is
    that the column parses, that its screens cost and reuse like any other
    screen, and that ambiguous input is FLAGGED rather than guessed at.

    This section replaces the old luminance/colour-based tests, which were
    retired along with the mechanism they covered."""
    print("\n[7] Underbase is explicit data")

    def ub(job_id, underbase_col, colors=("red", "blue"), override=""):
        """Build a job the way load_jobs() would, from raw column values."""
        screens = [_underbase_screen(c, job_id, "<test>")
                   for c in underbase_col.split(";") if c.strip()]
        j = job(job_id, list(screens) + list(colors))
        j["blank_color"] = ""
        j["underbase_override"] = UNDERBASE_OVERRIDE_VALUES[override]
        return _annotate_underbase(j)

    # --- Black is a valid underbase colour, priced like any other screen ----
    black = ub("black", "Black")
    white = ub("white", "White")
    check("underbase 'Black' parses to a UB_black screen",
          "UB_black" in black["screens"], str(sorted(black["screens"])))
    check("  ...and costs exactly what UB_white would",
          changeover_minutes(set(), black["screens"])
          == changeover_minutes(set(), white["screens"])
          == 3 * SETUP_MINUTES_PER_SCREEN,
          f"black {changeover_minutes(set(), black['screens'])} "
          f"white {changeover_minutes(set(), white['screens'])}")
    check("  ...and is a distinct screen from UB_white",
          black["screens"] != white["screens"]
          and changeover_minutes(white["screens"], black["screens"])
          == SETUP_MINUTES_PER_SCREEN,
          str(changeover_minutes(white["screens"], black["screens"])))

    combo = ub("combo", "White;Grey;Black")
    check("all three underbase colours combine into three screens",
          {"UB_white", "UB_grey", "UB_black"} <= combo["screens"]
          and len(combo["screens"]) == 5, str(sorted(combo["screens"])))

    # --- casing and whitespace, as the real CSVs contain -------------------
    messy = ub("messy", " white ; GREY ")
    check("underbase values are case/whitespace normalised",
          {"UB_white", "UB_grey"} <= messy["screens"], str(sorted(messy["screens"])))

    # --- Grey without White is ACCEPTED - deliberately not validated -------
    grey_only = ub("grey_only", "Grey")
    check("Grey without White is accepted (no such rule was ever confirmed)",
          "UB_grey" in grey_only["screens"]
          and grey_only["needs_underbase_review"] is False,
          str(sorted(grey_only["screens"])))

    # --- a misspelled underbase is rejected, not silently made a screen ----
    for bad in ["Whit", "Blu", "white2"]:
        try:
            ub("bad", bad)
            check(f"underbase {bad!r} is rejected", False, "no exception raised")
        except ValueError as e:
            check(f"underbase {bad!r} is rejected", "allowed values" in str(e))

    # --- blank underbase, no override: unaffected, zero underbase screens --
    blank = ub("blank", "")
    check("blank underbase + no override -> no UB_ screens, no flag",
          not any(s.startswith("UB_") for s in blank["screens"])
          and blank["underbase_status"] == "none"
          and blank["needs_underbase_review"] is False, str(blank))
    check("  ...and costs only its ink screens",
          changeover_minutes(set(), blank["screens"]) == 2 * SETUP_MINUTES_PER_SCREEN,
          str(changeover_minutes(set(), blank["screens"])))

    # --- force_off + blank column: still skipped, and NOT required for it ---
    off = ub("off", "", override="force_off")
    check("force_off + blank column -> same as blank, not flagged",
          not any(s.startswith("UB_") for s in off["screens"])
          and off["needs_underbase_review"] is False, str(off))

    # --- force_on + BLANK column: ambiguous -> FLAGGED, never guessed ------
    on_blank = ub("on_blank", "", override="force_on")
    check("force_on + blank column -> FLAGGED for manual review",
          on_blank["needs_underbase_review"] is True
          and on_blank["underbase_status"] == "override_without_layers",
          str(on_blank))
    check("  ...and no layer count is invented - still zero UB_ screens",
          not any(s.startswith("UB_") for s in on_blank["screens"]),
          str(sorted(on_blank["screens"])))
    check("  ...so it costs exactly what the same job without the override costs",
          changeover_minutes(set(), on_blank["screens"])
          == changeover_minutes(set(), blank["screens"]),
          str(changeover_minutes(set(), on_blank["screens"])))

    # --- override alongside a NON-blank column: contradictory -> FLAGGED ---
    for ov in ["force_on", "force_off"]:
        clash = ub(f"clash_{ov}", "White", override=ov)
        check(f"{ov} + explicit underbase column -> FLAGGED as contradictory",
              clash["needs_underbase_review"] is True
              and clash["underbase_status"] == "override_contradicts_column",
              str(clash))
        check(f"  ...and the COLUMN still wins: UB_white is scheduled",
              "UB_white" in clash["screens"], str(sorted(clash["screens"])))

    # --- flagged jobs reach the report -------------------------------------
    r = solve([ub("flag_me", "", override="force_on"), ub("fine", "White")],
              START, time_limit=20)
    flagged = sorted(j["job_id"] for j in underbase_review_jobs(r))
    check("flagged jobs are surfaced in the result, not silently dropped",
          flagged == ["flag_me"], str(flagged))
    check("  ...and the schedule still verifies", not verify(r, START),
          "; ".join(verify(r, START)))


def test_underbase_screen_reuse():
    """Underbase screens are reused by the ORDINARY screen mechanism - the
    same set difference that reuses ink colours. No underbase-specific reuse
    logic exists any more; the retired scalar system needed its own only
    because its underbase was never a real screen.

    This section replaces the old scalar-reuse tests."""
    print("\n[8] Underbase screens reuse via the ordinary screen mechanism")

    def ub(job_id, underbase_col, colors):
        screens = [_underbase_screen(c, job_id, "<test>")
                   for c in underbase_col.split(";") if c.strip()]
        j = job(job_id, list(screens) + list(colors))
        j["blank_color"] = ""
        j["underbase_override"] = None
        return _annotate_underbase(j)

    a = ub("a", "White", ["c1", "c2"])
    b = ub("b", "White", ["d1", "d2"])          # same underbase, different inks

    check("first job pays for its underbase screen",
          changeover_minutes(set(), a["screens"]) == 3 * SETUP_MINUTES_PER_SCREEN,
          str(changeover_minutes(set(), a["screens"])))
    check("a following job does NOT re-pay for the shared UB_white",
          changeover_minutes(a["screens"], b["screens"])
          == 2 * SETUP_MINUTES_PER_SCREEN,
          str(changeover_minutes(a["screens"], b["screens"])))
    check("  ...saving exactly one screen's worth",
          changeover_minutes(set(), b["screens"])
          - changeover_minutes(a["screens"], b["screens"]) == SETUP_MINUTES_PER_SCREEN)

    # A different underbase colour is a different screen: no free ride.
    c = ub("c", "Black", ["d1", "d2"])
    check("a DIFFERENT underbase colour is not reused from UB_white",
          changeover_minutes(a["screens"], c["screens"])
          == 3 * SETUP_MINUTES_PER_SCREEN,
          str(changeover_minutes(a["screens"], c["screens"])))

    # Partial underbase overlap carries over partially - this is screen-set
    # arithmetic, not the retired binary all-or-nothing rule.
    wg = ub("wg", "White;Grey", ["c1"])
    w = ub("w", "White", ["c1"])
    check("partial underbase overlap carries over per screen",
          changeover_minutes(wg["screens"], w["screens"]) == 0
          and changeover_minutes(w["screens"], wg["screens"])
          == SETUP_MINUTES_PER_SCREEN,
          f"{changeover_minutes(wg['screens'], w['screens'])} / "
          f"{changeover_minutes(w['screens'], wg['screens'])}")

    # reuse_demo.csv exercises this same mechanism end to end - see section [9].


def test_demo_files():
    """The two hand-crafted demonstration CSVs, each checked for the SPECIFIC
    behaviour it was built to show - not merely that it runs.

    These are illustrative fixtures, deliberately kept OUT of REAL_CSVS so
    nobody confuses them with order data pulled from an actual PO."""
    print("\n[9] Hand-crafted demo files")

    def run(path):
        """Same pipeline run_one() uses: greedy first, then CP-SAT capped at
        the greedy's makespan."""
        jobs = load_jobs(path)
        g = greedy_reference(jobs, START)
        r = solve(jobs, START, makespan_cap_seconds=g["makespan_seconds"],
                  time_limit=30, hint=g["hint"])
        return jobs, g, r

    # ---------------------------------------------------------------------
    # reuse_demo.csv -- hand-crafted test case proving underbase screen
    # reuse across consecutive same-machine jobs. Not a real order.
    # ---------------------------------------------------------------------
    jobs, g, r = run("reuse_demo.csv")
    seq = r["sequences"]["M5"]
    charges = [s["changeover_minutes"] for s in seq]

    check("reuse_demo.csv: both jobs forced onto M5, back to back",
          machines_used(r["sequences"]) == 1 and len(seq) == 2,
          str({m: [s["job_id"] for s in r["sequences"][m]] for m in MACHINES
               if r["sequences"][m]}))
    check("  ...both jobs carry the same UB_white screen",
          all("UB_white" in j["screens"] for j in jobs),
          str([sorted(j["screens"]) for j in jobs]))
    check("  ...first job pays for all 12 of its screens",
          charges[0] == 12 * SETUP_MINUTES_PER_SCREEN, str(charges))
    # THE POINT OF THIS FILE: the follower pays for 11 screens, not 12. The
    # screen it does not re-pay for is UB_white, still loaded from job one.
    check("  ...second job pays for 11, NOT 12 - UB_white is reused",
          charges[1] == 11 * SETUP_MINUTES_PER_SCREEN, str(charges))
    check("  ...i.e. exactly one screen's worth of underbase saved",
          charges[0] - charges[1] == SETUP_MINUTES_PER_SCREEN,
          f"saved {charges[0] - charges[1]} min")
    check("  ...total 115, not the 120 it would cost without reuse",
          r["total_changeover"] == 115, str(r["total_changeover"]))
    check("  ...greedy agrees, and the schedule verifies",
          g["total_changeover"] == 115 and not verify(r, START),
          f"greedy {g['total_changeover']}; {'; '.join(verify(r, START))}")

    # ---------------------------------------------------------------------
    # review_flags_demo.csv -- hand-crafted test case proving the two
    # manual-review flags (override_without_layers,
    # override_contradicts_column) fire correctly. Not a real order.
    # ---------------------------------------------------------------------
    jobs, g, r = run("review_flags_demo.csv")
    flagged = {j["job_id"]: j["underbase_status"] for j in underbase_review_jobs(r)}
    scheduled = {s["job_id"]: s for m in MACHINES for s in r["sequences"][m]}
    by_id = {j["job_id"]: j for j in jobs}

    check("review_flags_demo.csv: BOTH jobs are flagged for manual review",
          set(flagged) == {"RF_Incomplete", "RF_Contradiction"}, str(flagged))
    check("  ...RF_Incomplete tagged override_without_layers "
          "(force_on, blank underbase column)",
          flagged.get("RF_Incomplete") == "override_without_layers",
          str(flagged.get("RF_Incomplete")))
    check("  ...RF_Contradiction tagged override_contradicts_column "
          "(force_off vs an explicit White)",
          flagged.get("RF_Contradiction") == "override_contradicts_column",
          str(flagged.get("RF_Contradiction")))

    # Flagging is a WARNING, not a rejection: both must still be scheduled
    # with real screens and real times.
    check("  ...and BOTH are still scheduled, not rejected or skipped",
          set(scheduled) == {"RF_Incomplete", "RF_Contradiction"}, str(list(scheduled)))
    for job_id in ("RF_Incomplete", "RF_Contradiction"):
        step = scheduled.get(job_id)
        check(f"  ...{job_id} got real screens and a real run window",
              step is not None and len(step["screens"]) > 0
              and step["end_s"] > step["start_s"],
              str(step and (len(step["screens"]), step["start_s"], step["end_s"])))

    # What each was scheduled with BY DEFAULT while awaiting review: exactly
    # what its underbase column named. No layer count invented for the
    # force_on, none dropped for the force_off.
    incomplete_ub = sorted(s for s in by_id["RF_Incomplete"]["screens"]
                           if s.startswith("UB_"))
    contradiction_ub = sorted(s for s in by_id["RF_Contradiction"]["screens"]
                              if s.startswith("UB_"))
    check("  ...force_on with a blank column invented NO underbase layer",
          incomplete_ub == [], str(incomplete_ub))
    check("  ...force_off did NOT drop the column's UB_white",
          contradiction_ub == ["UB_white"], str(contradiction_ub))
    check("  ...so the column alone decided both: 3 and 4 screens",
          len(by_id["RF_Incomplete"]["screens"]) == 3
          and len(by_id["RF_Contradiction"]["screens"]) == 4,
          f"{len(by_id['RF_Incomplete']['screens'])} / "
          f"{len(by_id['RF_Contradiction']['screens'])}")
    check("  ...totalling 35 min, and the schedule verifies",
          r["total_changeover"] == 35 and not verify(r, START),
          f"{r['total_changeover']}; {'; '.join(verify(r, START))}")

    # Neither demo file may drift into the real-order set.
    check("demo files are kept out of REAL_CSVS",
          not set(DEMO_CSVS) & set(REAL_CSVS), str(DEMO_CSVS))


def test_csv_edge_cases():
    print("\n[10] CSV loading edge cases (via load_jobs, shared with Phase 1)")
    tmp = tempfile.mkdtemp()

    def write(name, rows):
        path = os.path.join(tmp, name)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["job_id", "po_id", "due_date", "quantity", "underbase", "colors"])
            w.writerows(rows)
        return path

    # header only
    p = write("empty.csv", [])
    jobs = load_jobs(p)
    check("header-only CSV loads as zero jobs", jobs == [], str(jobs))
    r = solve(jobs, START, time_limit=10)
    check("  ...and solves to an empty schedule", r is not None and r["total_changeover"] == 0)

    # casing + whitespace must collapse to the SAME screen set
    p = write("case.csv", [
        ["A", "P", "2026-09-30", "100", "White", "Red;Blue"],
        ["B", "P", "2026-09-30", "100", " white ", " RED ; blue "],
    ])
    jobs = load_jobs(p)
    check("casing/whitespace normalise to one screen set",
          jobs[0]["screens"] == jobs[1]["screens"], f"{jobs[0]['screens']} vs {jobs[1]['screens']}")
    r = solve_and_verify("  ...so the pair costs one setup (15 min)", jobs)
    if r:
        check("    changeover == 15", r["total_changeover"] == 15, str(r["total_changeover"]))

    # an underbase named like an ink colour must NOT be confused with it
    p = write("ubclash.csv", [
        ["A", "P", "2026-09-30", "100", "White", ""],
        ["B", "P", "2026-09-30", "100", "", "White"],
    ])
    jobs = load_jobs(p)
    check("underbase 'white' != ink colour 'white'",
          jobs[0]["screens"] != jobs[1]["screens"], f"{jobs[0]['screens']} {jobs[1]['screens']}")
    r = solve_and_verify("  ...so they cost two setups (10 min)", jobs)
    if r:
        check("    changeover == 10", r["total_changeover"] == 10, str(r["total_changeover"]))

    # duplicate job ids - rejected up front, since they collapse every
    # id-keyed lookup (the solution hint, the verifier's job map)
    p = write("dupes.csv", [
        ["DUP", "P", "2026-09-30", "100", "White", "Red"],
        ["DUP", "P", "2026-09-30", "100", "", "Blue;Green"],
    ])
    jobs = load_jobs(p)
    try:
        solve(jobs, START, time_limit=20)
        check("duplicate job_id in a CSV is rejected", False, "no exception")
    except ValueError as e:
        check("duplicate job_id in a CSV is rejected", "DUP" in str(e), str(e))
        print(f"        ({e})")

    # the three real CSVs must all pass validation
    for path in REAL_CSVS:
        try:
            validate_jobs(load_jobs(path))
            check(f"{path} passes input validation", True)
        except ValueError as e:
            check(f"{path} passes input validation", False, str(e))


def test_determinism():
    print("\n[11] Determinism and cross-file consistency")
    for path in REAL_CSVS:
        jobs = load_jobs(path)
        greedy = greedy_reference(jobs, START)
        cap = greedy["makespan_seconds"]
        a = solve(jobs, START, makespan_cap_seconds=cap, time_limit=60)
        b = solve(jobs, START, makespan_cap_seconds=cap, time_limit=60)
        check(f"{path}: two runs agree",
              a["total_changeover"] == b["total_changeover"],
              f"{a['total_changeover']} vs {b['total_changeover']}")
        check(f"{path}: CP-SAT is never worse than the greedy",
              a["total_changeover"] <= greedy["total_changeover"],
              f"CP-SAT {a['total_changeover']} > greedy {greedy['total_changeover']}")
        check(f"{path}: CP-SAT is no later than the greedy",
              len(late_jobs(a, START)) <= len(greedy["late"]),
              f"CP-SAT {len(late_jobs(a, START))} > greedy {len(greedy['late'])}")
        check(f"{path}: schedule verifies", not verify(a, START),
              "; ".join(verify(a, START)))


if __name__ == "__main__":
    test_brute_force()
    test_brute_force_capped()
    test_edge_cases()
    test_regressions()
    test_hint_does_not_constrain()
    test_makespan_cap_is_exact()
    test_underbase_is_explicit()
    test_underbase_screen_reuse()
    test_demo_files()
    test_csv_edge_cases()
    test_determinism()

    print(f"\n{'=' * 60}")
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name in FAIL:
            print(f"  FAILED: {name}")
        sys.exit(1)
    print("all checks passed")
