"""
Print Order Sequencing Optimizer - Phase 2 (CP-SAT)

Solves the same problem as scheduler.py's greedy heuristic - assign print
jobs to machines and sequence them so total screen changeover time is as
small as possible, without missing due dates - but with Google OR-Tools
CP-SAT, which looks at the whole job queue at once and can PROVE the
solution is optimal instead of approximating it.

Reuses load_jobs() and the machine tables from scheduler.py, so Phase 1 and
Phase 2 read exactly the same job CSV format and use the same constants.

--------------------------------------------------------------------------
Modelling decisions (all mirror Phase 1 so the comparison is apples-to-
apples; each one is a knob, not a hidden assumption)
--------------------------------------------------------------------------

1. Initial machine state: every machine starts with NO screens loaded, so
   the first job on each machine pays full setup for all of its screens.
   That is what Phase 1 does. The real "what's loaded right now" input
   doesn't exist yet; INITIAL_LOADED_SCREENS below is the hook for it.

2. "Screens already loaded" means the screens of the job IMMEDIATELY
   preceding this one on the same machine - per PROJECT_BRIEF.md. See the
   KNOWN SIMPLIFICATION note on INITIAL_LOADED_SCREENS below.

3. Due dates are hard, at day granularity: a job is on time if it finishes
   any time before midnight at the end of its due date. Same rule as Phase
   1's `finish.date() > due_date.date()` test.

4. Jobs that no machine can hold (more screens than the biggest machine's
   capacity) are removed from the model and flagged loudly. They are never
   silently dropped, and Phase 1 excludes them too, so both totals cover
   the same job set.

5. If the hard due dates are PROVEN unsatisfiable (messy_jobs.csv has jobs
   whose due date is already in the past at start_time), the solver does
   NOT give up and return nothing. It relaxes them lexicographically:
   fewest late jobs, then least total lateness, then least changeover -
   and reports exactly which jobs are late, by how much.

6. Minimising TOTAL changeover on its own pushes the optimum toward
   cramming every job onto one machine, because each additional machine
   used costs a fresh full setup. To keep the answer usable, changeover is
   minimised subject to makespan <= the Phase 1 greedy's makespan: the
   CP-SAT schedule is never slower than the greedy, and still uses less
   setup time. Pass --no-cap to see the unconstrained true optimum.

7. Time is modelled in whole SECONDS (setup is always a whole number of
   minutes; run times are quantity/units-per-hour rounded to the nearest
   second). The makespan cap is computed by replaying the greedy schedule
   in those same integer seconds, so the cap is exact rather than
   approximate - see greedy_reference().
"""

import argparse
from datetime import datetime, timedelta

from ortools.sat.python import cp_model

from scheduler import (
    MACHINE_MAX_SCREENS,
    MACHINE_UNITS_PER_HOUR,
    SETUP_MINUTES_PER_SCREEN,
    changeover_minutes,
    load_jobs,
    schedule_jobs,
)

MACHINES = list(MACHINE_MAX_SCREENS)

# Hook for the "current machine state" input described in the brief: what is
# physically racked on each machine when the run starts. Empty = all machines
# bare, which is what Phase 1 assumes. Fill this in (lowercased names, UB_
# prefix for underbases, exactly as load_jobs() produces) once that data is
# actually collected.
#
# KNOWN SIMPLIFICATION - PENDING CONFIRMATION FROM THE OPS TEAM.
# A machine's "loaded screens" is modelled as exactly the screen set of the
# job that ran immediately before, per PROJECT_BRIEF.md. A machine does not
# accumulate a pool of screens across several earlier jobs. Two consequences
# worth knowing before anyone trusts a number out of this:
#   - A job that shares screens with two jobs back gets no credit for them.
#   - A zero-screen job (messy_jobs.csv K19) presents an EMPTY screen set to
#     whatever follows it, so the next job pays full setup, as though the
#     rack had been stripped. CP-SAT exploits this by scheduling K19 first,
#     where it costs nothing.
# Phase 1 behaves identically, so the greedy-vs-CP-SAT comparison is fair
# either way. If ops confirms screens actually stay racked until something
# displaces them, this becomes an accumulating-pool model bounded by each
# machine's capacity - a change to BOTH phases, not a bug in one of them.
# Deliberately left as-is until that question is answered.
INITIAL_LOADED_SCREENS = {m: set() for m in MACHINES}

# Unit conversions. Named because the model mixes minutes (setup, which is
# always a whole number of them) with seconds (run times and the schedule).
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

# Solver settings. random_seed is pinned so repeated runs give the same
# schedule - a portfolio project that prints a different answer each time is
# hard to defend.
SOLVER_WORKERS = 8
SOLVER_RANDOM_SEED = 0
DEFAULT_TIME_LIMIT_SECONDS = 60.0

REPORT_WIDTH = 78


# ---------------------------------------------------------------------------
# Shared cost / duration helpers. changeover_minutes() lives in scheduler.py
# and is imported above, so the Phase 1 greedy and the Phase 2 model cost a
# changeover identically. It is the ENTIRE cost model: underbase screens are
# ordinary screens, so they are priced and reused by that one function.
# ---------------------------------------------------------------------------

def run_seconds(job, machine):
    return int(round(job["quantity"] / MACHINE_UNITS_PER_HOUR[machine]
                     * SECONDS_PER_HOUR))


def deadline_seconds(job, start_time):
    """Seconds from start_time until the last instant of the due DATE. Day
    granularity, matching Phase 1: finishing at 23:59:59 on the due date is on
    time. Negative means the due date is already gone at start_time.

    Anchored on .date(), not on due_date + 24h. load_jobs() parses due dates
    as midnight so the two agree there, but any due date carrying a
    time-of-day made "+24h" land on the NEXT calendar day - the hard
    constraint would then accept a job that the date-based lateness report
    called late, and the two halves of the program disagreed silently."""
    due_midnight = datetime.combine(job["due_date"].date(), datetime.min.time())
    last_instant = due_midnight + timedelta(days=1) - timedelta(seconds=1)
    return int((last_instant - start_time).total_seconds())


def due_day_index(job, start_time):
    """Which calendar day the due date falls on, counting the start day as 0.
    Negative if the due date is already past. Same day-granular basis as
    deadline_seconds() and late_jobs() - all three must agree."""
    return (job["due_date"].date() - start_time.date()).days


def eligible_machines(job):
    n = len(job["screens"])
    return [m for m in MACHINES if n <= MACHINE_MAX_SCREENS[m]]


def machines_used(sequences):
    return sum(1 for m in MACHINES if sequences[m])


# ---------------------------------------------------------------------------
# Input validation - bad data should say so, not produce a confident schedule
# ---------------------------------------------------------------------------

def validate_jobs(jobs):
    """Raise ValueError on input the model cannot meaningfully schedule.

    These are all silent-wrong-answer risks rather than crashes, which is why
    they are checked rather than left to fail somewhere downstream:
    a negative quantity makes the model infeasible for no stated reason, and
    duplicate job ids collapse every id-keyed lookup (the solution hint, the
    verifier's job map) so two different jobs get treated as one."""
    if set(MACHINE_MAX_SCREENS) != set(MACHINE_UNITS_PER_HOUR):
        raise ValueError(
            "MACHINE_MAX_SCREENS and MACHINE_UNITS_PER_HOUR list different "
            f"machines: {sorted(set(MACHINE_MAX_SCREENS) ^ set(MACHINE_UNITS_PER_HOUR))}")

    bad_qty = [j["job_id"] for j in jobs if j["quantity"] < 0]
    if bad_qty:
        raise ValueError(f"negative quantity on job(s): {bad_qty}")

    seen, dupes = set(), []
    for j in jobs:
        if j["job_id"] in seen:
            dupes.append(j["job_id"])
        seen.add(j["job_id"])
    if dupes:
        raise ValueError(
            f"duplicate job_id(s): {sorted(set(dupes))}. Job ids must be "
            f"unique - they are the key for reporting and verification.")


# ---------------------------------------------------------------------------
# Phase 1 greedy, re-run here so Phase 2 can report the comparison
# ---------------------------------------------------------------------------

def greedy_reference(jobs, start_time):
    """Run the Phase 1 greedy and pull out the numbers Phase 2 compares
    against: total changeover, makespan, late jobs, and a solution hint.

    The makespan is deliberately re-derived by replaying the greedy's own
    sequences through run_seconds()/changeover_minutes(), NOT read off the
    greedy's datetime clock. The greedy works in floats and the model in whole
    seconds, so the two disagree by a fraction of a second per job. Replaying
    it in the model's own arithmetic means the makespan cap can be the exact
    greedy makespan, with no rounding slack to fudge - and it guarantees the
    greedy schedule itself satisfies the cap, so the cap can never make a
    feasible problem look infeasible."""
    machines, flagged = schedule_jobs(jobs, start_time)

    makespan = 0
    total_changeover = 0
    for m, state in machines.items():
        loaded = set(INITIAL_LOADED_SCREENS[m])
        clock = 0
        for step in state["run"]:
            job = next(j for j in jobs if j["job_id"] == step["job_id"])
            setup = changeover_minutes(loaded, job["screens"])
            total_changeover += setup
            clock += setup * SECONDS_PER_MINUTE + run_seconds(job, m)
            loaded = set(job["screens"])
        makespan = max(makespan, clock)

    # Cross-check Phase 1's own bookkeeping against the shared cost helper.
    # These must agree; if they ever don't, one of the two phases is wrong.
    reported = sum(step["changeover_minutes"]
                   for state in machines.values() for step in state["run"])

    late = [(job["job_id"], reason) for job, reason in flagged
            if "after due date" in reason]

    # The greedy schedule is always a valid solution to the CP-SAT model, so
    # it makes an excellent solution hint: the solver starts from a known-good
    # point instead of searching for its first feasible schedule.
    hint = {step["job_id"]: m
            for m, state in machines.items() for step in state["run"]}

    return {
        "machines": machines,
        "total_changeover": total_changeover,
        "changeover_as_phase1_reported": reported,
        "makespan_seconds": makespan,
        "late": late,
        "hint": hint,
    }


# ---------------------------------------------------------------------------
# The CP-SAT model
# ---------------------------------------------------------------------------

def build_model(jobs, start_time, *, hard_due_dates, makespan_cap_seconds,
                max_late_count=None, max_total_lateness=None, hint=None):
    """Build the CP-SAT model over `jobs` (all of which must fit on at least
    one machine). Returns (model, handles) where handles exposes the decision
    variables the caller needs to read or bound.

    Sequencing uses one AddCircuit per machine over that machine's eligible
    jobs plus a depot node. A job not assigned to the machine takes its own
    self-loop, so the circuit degenerates to a Hamiltonian path through
    exactly the assigned jobs: depot -> first job -> ... -> last job -> depot.
    That gives a genuine total order per machine, which is what a
    sequence-dependent setup cost needs.
    """
    model = cp_model.CpModel()
    n = len(jobs)

    # Horizon: an absurdly bad schedule (everything on the slowest eligible
    # machine, full setup every time) still fits, and it is never shorter than
    # the furthest deadline.
    worst = sum(max(run_seconds(j, m) for m in eligible_machines(j))
                + len(j["screens"]) * SETUP_MINUTES_PER_SCREEN * SECONDS_PER_MINUTE
                for j in jobs)
    horizon = max([worst] + [deadline_seconds(j, start_time) for j in jobs]) + 1

    # --- assignment ---------------------------------------------------------
    x = {}  # (job index, machine) -> bool: job runs on machine
    for i, job in enumerate(jobs):
        for m in eligible_machines(job):
            x[i, m] = model.new_bool_var(f"x_{i}_{m}")
        model.add_exactly_one(x[i, m] for m in eligible_machines(job))

    # --- timing -------------------------------------------------------------
    setup, start, end, dur = [], [], [], []
    for i, job in enumerate(jobs):
        # A job's setup can never exceed the cost of loading every screen it
        # needs (plus a rule-derived underbase layer, which it always pays if
        # it applies), so bound it per job rather than by the queue-wide
        # worst case.
        # 0 (every screen already loaded) up to loading every screen fresh.
        setup.append(model.new_int_var(
            0, len(job["screens"]) * SETUP_MINUTES_PER_SCREEN, f"setup_{i}"))
        start.append(model.new_int_var(0, horizon, f"start_{i}"))
        end.append(model.new_int_var(0, horizon, f"end_{i}"))

        options = [run_seconds(job, m) for m in eligible_machines(job)]
        d = model.new_int_var(min(options), max(options), f"dur_{i}")
        for m in eligible_machines(job):
            model.add(d == run_seconds(job, m)).only_enforce_if(x[i, m])
        dur.append(d)

        model.add(end[i] == start[i] + d)
        # A job can never start before its own setup is done, whatever runs
        # before it. Redundant with the arc constraints below but a much
        # stronger bound for the solver.
        model.add(start[i] >= SECONDS_PER_MINUTE * setup[i])

    # --- sequencing, one circuit per machine --------------------------------
    # order[m] keeps the arc literals so the solved sequence can be read back
    # from the solver's actual decisions. Do NOT infer the order by sorting on
    # start time: zero-length jobs tie, and the inferred order then disagrees
    # with the real one.
    order = {}
    for m in MACHINES:
        elig = [i for i in range(n) if (i, m) in x]
        if not elig:
            order[m] = {"first": {}, "arc": {}}
            continue

        node = {i: k + 1 for k, i in enumerate(elig)}  # node 0 is the depot
        arcs = []
        order[m] = {"first": {}, "arc": {}}

        # A machine with no jobs needs the depot to self-loop, otherwise
        # add_circuit has no circuit to build.
        unused = model.new_bool_var(f"unused_{m}")
        model.add(sum(x[i, m] for i in elig) == 0).only_enforce_if(unused)
        model.add(sum(x[i, m] for i in elig) >= 1).only_enforce_if(~unused)
        arcs.append((0, 0, unused))

        for i in elig:
            arcs.append((node[i], node[i], ~x[i, m]))

            # depot -> i : i runs first on this machine, so it pays setup
            # against whatever was already loaded on it (nothing, by default).
            first = model.new_bool_var(f"first_{m}_{i}")
            arcs.append((0, node[i], first))
            model.add_implication(first, x[i, m])
            model.add(setup[i] == changeover_minutes(
                INITIAL_LOADED_SCREENS[m], jobs[i]["screens"])
            ).only_enforce_if(first)
            order[m]["first"][i] = first

            # i -> depot : i runs last on this machine. No cost.
            last = model.new_bool_var(f"last_{m}_{i}")
            arcs.append((node[i], 0, last))
            model.add_implication(last, x[i, m])

        for i in elig:
            for j in elig:
                if i == j:
                    continue
                arc = model.new_bool_var(f"arc_{m}_{i}_{j}")
                arcs.append((node[i], node[j], arc))
                model.add_implication(arc, x[i, m])
                model.add_implication(arc, x[j, m])
                model.add(setup[j] == changeover_minutes(
                    jobs[i]["screens"], jobs[j]["screens"])
                ).only_enforce_if(arc)
                # j is set up only after i has finished running: this is what
                # keeps a machine from being double-booked.
                model.add(start[j] >= end[i] + SECONDS_PER_MINUTE * setup[j]
                          ).only_enforce_if(arc)
                order[m]["arc"][i, j] = arc

        model.add_circuit(arcs)

        # Redundant but valuable: optional intervals + no-overlap give the
        # solver a real scheduling propagator on top of the arc precedences.
        model.add_no_overlap([
            model.new_optional_interval_var(start[i], dur[i], end[i], x[i, m],
                                           f"iv_{m}_{i}")
            for i in elig
        ])

    # --- objective ingredients ---------------------------------------------
    # Every job has exactly one incoming arc, so exactly one constraint fixes
    # its setup, and summing per-job setup is the exact total changeover.
    total_changeover = sum(setup)

    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(makespan, end)
    if makespan_cap_seconds is not None:
        model.add(makespan <= makespan_cap_seconds)

    # Lateness is measured in whole CALENDAR DAYS, not seconds, because the
    # due-date rule itself is day-granular. Measuring it in seconds made the
    # relaxation actively harmful: on the real PO file every job is already ~19
    # days overdue, and shaving a few HOURS off that meaningless lateness was
    # worth more to the objective than 75 minutes of real setup savings, so
    # the solver split four identical-screen jobs across four machines and
    # paid four full setups. In days those options tie, and changeover
    # decides - which is the point.
    day_offset = int((start_time
                      - datetime.combine(start_time.date(), datetime.min.time())
                      ).total_seconds())
    max_day = (horizon + day_offset) // SECONDS_PER_DAY + 1
    # A due date already in the past gives a negative due-day index, so
    # lateness can exceed max_day by that overshoot.
    max_late_days = max_day + max(
        [0] + [-due_day_index(j, start_time) for j in jobs])

    lateness, is_late = [], []
    for i, job in enumerate(jobs):
        if hard_due_dates:
            model.add(end[i] <= deadline_seconds(job, start_time))
            continue
        # Calendar day index (0 = the day the run starts) on which job i ends.
        finish_day = model.new_int_var(0, max_day, f"finish_day_{i}")
        model.add_division_equality(finish_day, end[i] + day_offset,
                                    SECONDS_PER_DAY)

        late = model.new_int_var(0, max_late_days, f"late_days_{i}")
        model.add_max_equality(
            late, [finish_day - due_day_index(job, start_time), 0])
        lateness.append(late)

        flag = model.new_bool_var(f"is_late_{i}")
        model.add(late >= 1).only_enforce_if(flag)
        model.add(late == 0).only_enforce_if(~flag)
        is_late.append(flag)

    if not hard_due_dates:
        if max_late_count is not None:
            model.add(sum(is_late) <= max_late_count)
        if max_total_lateness is not None:
            model.add(sum(lateness) <= max_total_lateness)

    # --- warm start from the greedy ----------------------------------------
    # add_hint() is advisory: CP-SAT uses it to find a first solution quickly
    # and is free to discard it entirely. It adds no constraint and cannot
    # change the optimum, and none of the parameters that would turn a hint
    # into a constraint (fix_variables_to_their_hinted_value, repair_hint) are
    # set. test_cp_sat.py checks this against brute force, hinted and not.
    if hint:
        for i, job in enumerate(jobs):
            if job["job_id"] not in hint:
                continue
            # Assignment only. Hinting start times too made the hint slightly
            # self-inconsistent (the greedy times come from float arithmetic,
            # the model works in whole seconds) and CP-SAT then spent its
            # effort repairing the hint instead of using it.
            hinted_machine = hint[job["job_id"]]
            for m in eligible_machines(job):
                model.add_hint(x[i, m], 1 if m == hinted_machine else 0)

    return model, {
        "x": x, "setup": setup, "start": start, "end": end, "order": order,
        "makespan": makespan, "total_changeover": total_changeover,
        "lateness": lateness, "is_late": is_late,
    }


def _solve(model, objective, time_limit):
    model.minimize(objective)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = SOLVER_WORKERS
    solver.parameters.random_seed = SOLVER_RANDOM_SEED
    return solver, solver.solve(model)


def _machine_sequence(machine, handles, solver):
    """Read a machine's job order straight off the circuit arcs the solver
    chose, by walking depot -> first -> ... -> last. Returns job indices.

    This used to be inferred by sorting jobs on their start time, which is
    wrong whenever two jobs on a machine share a start time - possible when
    runs have zero length (quantity 0) and the setup between them is 0. The
    inferred order then disagreed with the solver's real sequence and the
    verifier reported bogus per-job changeover mismatches."""
    info = handles["order"][machine]
    successors = {}
    for (i, j), lit in info["arc"].items():
        if solver.boolean_value(lit):
            successors[i] = j

    current = next((i for i, lit in info["first"].items()
                    if solver.boolean_value(lit)), None)

    sequence = []
    while current is not None and len(sequence) <= len(info["first"]):
        sequence.append(current)
        current = successors.get(current)
    return sequence


def _extract(jobs, handles, solver, start_time):
    """Turn a solved model into per-machine job sequences, in the order the
    solver actually chose."""
    sequences = {}
    for m in MACHINES:
        sequences[m] = []
        for i in _machine_sequence(m, handles, solver):
            start_s = solver.value(handles["start"][i])
            end_s = solver.value(handles["end"][i])
            sequences[m].append({
                "job_id": jobs[i]["job_id"],
                "po_id": jobs[i]["po_id"],
                "due_date": jobs[i]["due_date"],
                "screens": jobs[i]["screens"],
                "changeover_minutes": solver.value(handles["setup"][i]),
                "start_s": start_s,
                "end_s": end_s,
                "start": start_time + timedelta(seconds=start_s),
                "finish": start_time + timedelta(seconds=end_s),
            })
    return sequences


def solve(jobs, start_time, *, makespan_cap_seconds=None,
          time_limit=DEFAULT_TIME_LIMIT_SECONDS, hint=None):
    """Solve the scheduling problem. Tries hard due dates first; only if that
    is PROVEN infeasible does it fall back to a lexicographic relaxation
    (fewest late jobs, then least total lateness, then least changeover).

    The INFEASIBLE / UNKNOWN distinction matters and is not pedantry. Treating
    a timeout (UNKNOWN) as infeasible made the solver relax due dates it had
    simply run out of time to satisfy, and then report late jobs that a plain
    greedy schedule met comfortably - a wrong answer dressed up as a hard
    constraint. UNKNOWN now surfaces as a timeout the caller must act on.

    Returns a result dict. A dict with status "TIMEOUT" means no schedule was
    found in the time allowed - raise time_limit. None means the model was
    infeasible even with every due date relaxed, which would be a modelling
    bug rather than bad data. Raises ValueError on unusable input."""
    validate_jobs(jobs)

    infeasible_capacity = [j for j in jobs if not eligible_machines(j)]
    solvable = [j for j in jobs if eligible_machines(j)]

    empty = {
        "sequences": {m: [] for m in MACHINES}, "jobs": [],
        "infeasible_capacity": infeasible_capacity, "total_changeover": 0,
        "makespan_seconds": 0, "status": "EMPTY", "due_dates": "hard",
        "cap_applied": None, "bound": 0, "notes": [], "wall_time": 0.0,
    }
    if not solvable:
        return empty

    notes = []
    ok = (cp_model.OPTIMAL, cp_model.FEASIBLE)
    # Try with the cap, then without it. A cap can legitimately rule out every
    # on-time schedule, and reporting nothing would be less useful than
    # reporting a slower one with the trade-off spelled out.
    caps = [makespan_cap_seconds, None] if makespan_cap_seconds is not None else [None]

    # --- attempt 1: due dates hard -----------------------------------------
    proven_infeasible = False
    cap_timed_out = False
    for cap in caps:
        model, h = build_model(solvable, start_time, hard_due_dates=True,
                               makespan_cap_seconds=cap, hint=hint)
        solver, status = _solve(model, h["total_changeover"], time_limit)
        if status in ok:
            if cap is None and makespan_cap_seconds is not None:
                notes.append(
                    "makespan is UNCAPPED in this result: the capped model "
                    + ("timed out, so this makespan may exceed the greedy's"
                       if cap_timed_out else
                       "was proven infeasible - no on-time schedule fits "
                       "inside the greedy's makespan"))
            return _result(solvable, h, solver, status, start_time,
                           infeasible_capacity, "hard", cap, notes)
        if status == cp_model.INFEASIBLE:
            proven_infeasible = True
            if cap is not None:
                notes.append("no on-time schedule fits under the makespan cap; "
                             "retrying uncapped")
        else:
            # UNKNOWN: out of time, not out of options. Say so; do not relax.
            if cap is not None:
                cap_timed_out = True
            notes.append(f"hard-due-date model hit the {time_limit:g}s limit "
                         f"{'with' if cap else 'without'} the makespan cap "
                         f"(status {solver.status_name(status)})")

    if not proven_infeasible:
        empty["status"] = "TIMEOUT"
        empty["notes"] = notes + [
            "no schedule found in the time allowed, and the due dates were "
            "never proven unsatisfiable - raise --time-limit rather than "
            "concluding the job set is infeasible"]
        return empty

    # --- attempt 2: relax due dates, lexicographically ---------------------
    notes.append("due dates PROVEN unsatisfiable - relaxed (fewest late jobs, "
                 "then least total lateness in days, then least changeover)")
    for cap in caps:
        stage_notes = []

        # Stage 1: how few jobs can be late at all?
        model, h = build_model(solvable, start_time, hard_due_dates=False,
                               makespan_cap_seconds=cap, hint=hint)
        solver, status = _solve(model, sum(h["is_late"]), time_limit)
        if status not in ok:
            if cap is not None:
                notes.append("makespan cap dropped: no schedule at all fits under it")
            continue
        # Each stage's result becomes the next stage's constraint. A stage that
        # only found a feasible (not proven optimal) value still gives a valid
        # upper bound to carry forward - the schedule stays correct, it just
        # may not be the lexicographic optimum.
        if status != cp_model.OPTIMAL:
            stage_notes.append("fewest-late-jobs stage not proven optimal (time limit)")
        best_late_count = round(solver.objective_value)

        # Stage 2: given that, how few late DAYS in total?
        model, h = build_model(solvable, start_time, hard_due_dates=False,
                               makespan_cap_seconds=cap,
                               max_late_count=best_late_count, hint=hint)
        solver, status = _solve(model, sum(h["lateness"]), time_limit)
        if status not in ok:
            continue
        if status != cp_model.OPTIMAL:
            stage_notes.append("total-lateness stage not proven optimal (time limit)")
        best_lateness = round(solver.objective_value)

        # Stage 3: given both, minimise changeover - the actual objective.
        model, h = build_model(solvable, start_time, hard_due_dates=False,
                               makespan_cap_seconds=cap,
                               max_late_count=best_late_count,
                               max_total_lateness=best_lateness, hint=hint)
        solver, status = _solve(model, h["total_changeover"], time_limit)
        if status not in ok:
            continue
        return _result(solvable, h, solver, status, start_time,
                       infeasible_capacity, "relaxed", cap, notes + stage_notes)

    return None


def _result(jobs, handles, solver, status, start_time, infeasible_capacity,
            due_mode, cap, notes):
    return {
        "sequences": _extract(jobs, handles, solver, start_time),
        "jobs": jobs,
        "infeasible_capacity": infeasible_capacity,
        "total_changeover": solver.value(handles["total_changeover"]),
        "makespan_seconds": solver.value(handles["makespan"]),
        "status": solver.status_name(status),
        "due_dates": due_mode,
        "cap_applied": cap,
        "bound": solver.best_objective_bound,
        "notes": notes,
        "wall_time": solver.wall_time,
    }


# ---------------------------------------------------------------------------
# Independent verification - deliberately does NOT trust the model
# ---------------------------------------------------------------------------

def verify(result, start_time):
    """Replay the solver's schedule in plain Python and re-derive everything
    from scratch: changeover cost, timings, capacity, due dates, overlap. Any
    disagreement with what the solver reported is a bug in the model.

    Returns a list of problem strings - empty means the schedule checks out."""
    problems = []
    sequences = result["sequences"]

    scheduled = [s["job_id"] for m in MACHINES for s in sequences[m]]
    expected = [j["job_id"] for j in result["jobs"]]
    if sorted(scheduled) != sorted(expected):
        problems.append(
            f"job set mismatch: missing={sorted(set(expected) - set(scheduled))} "
            f"extra={sorted(set(scheduled) - set(expected))}")
    if len(scheduled) != len(set(scheduled)):
        problems.append("a job was scheduled more than once")

    by_id = {j["job_id"]: j for j in result["jobs"]}
    replayed_changeover = 0

    for m in MACHINES:
        loaded = set(INITIAL_LOADED_SCREENS[m])
        prev_end = 0
        for step in sequences[m]:
            job = by_id.get(step["job_id"])
            if job is None:
                continue

            if len(job["screens"]) > MACHINE_MAX_SCREENS[m]:
                problems.append(
                    f"{job['job_id']}: {len(job['screens'])} screens on {m}, "
                    f"which holds {MACHINE_MAX_SCREENS[m]}")

            expected_setup = changeover_minutes(loaded, job["screens"])
            if expected_setup != step["changeover_minutes"]:
                problems.append(
                    f"{job['job_id']} on {m}: solver says {step['changeover_minutes']} min "
                    f"changeover, hand calculation says {expected_setup} min")
            replayed_changeover += expected_setup

            # Covers both "starts before its own setup could finish" and
            # "overlaps the previous job", since setup is never negative.
            earliest = prev_end + expected_setup * SECONDS_PER_MINUTE
            if step["start_s"] < earliest:
                problems.append(
                    f"{job['job_id']} on {m}: starts at {step['start_s']}s, but the "
                    f"previous job runs to {prev_end}s and {expected_setup} min of "
                    f"setup cannot finish before {earliest}s")

            expected_dur = run_seconds(job, m)
            if step["end_s"] - step["start_s"] != expected_dur:
                problems.append(
                    f"{job['job_id']} on {m}: duration {step['end_s'] - step['start_s']}s, "
                    f"expected {expected_dur}s for {job['quantity']} units")

            # Checked on calendar dates, deliberately NOT via
            # deadline_seconds() - using the model's own deadline arithmetic
            # here would make the check agree with the model by construction,
            # which is exactly how the +24h deadline bug slipped through.
            if (result["due_dates"] == "hard"
                    and step["finish"].date() > job["due_date"].date()):
                problems.append(
                    f"{job['job_id']}: finishes {step['finish']} after its "
                    f"{job['due_date'].date()} due date, but due dates were hard")

            loaded = set(job["screens"])
            prev_end = step["end_s"]

    if replayed_changeover != result["total_changeover"]:
        problems.append(
            f"total changeover: solver reports {result['total_changeover']} min, "
            f"hand replay gives {replayed_changeover} min")

    finishes = [s["end_s"] for m in MACHINES for s in sequences[m]]
    if finishes and max(finishes) != result["makespan_seconds"]:
        problems.append(
            f"makespan: solver reports {result['makespan_seconds']}s, "
            f"last job actually finishes at {max(finishes)}s")
    if result["cap_applied"] is not None and finishes and max(finishes) > result["cap_applied"]:
        problems.append(
            f"makespan {max(finishes)}s exceeds the cap of {result['cap_applied']}s")

    for job in result["infeasible_capacity"]:
        if eligible_machines(job):
            problems.append(
                f"{job['job_id']} was flagged as not fitting any machine, but it fits "
                f"{eligible_machines(job)}")

    return problems


def all_jobs(result):
    """Every job the run covered, schedulable or not - so a job that needs
    underbase review is still surfaced even if no machine can hold it."""
    return list(result["jobs"]) + list(result["infeasible_capacity"])


def underbase_review_jobs(result):
    """Jobs whose underbase input is ambiguous or self-contradictory, per
    scheduler._annotate_underbase(): a force_on that names no screens, or an
    override sitting alongside an explicit underbase column. These are
    data-entry questions for a human - the scheduler never guesses a layer
    count to resolve them."""
    return [j for j in all_jobs(result) if j.get("needs_underbase_review")]


def late_jobs(result, start_time):
    """Recomputed from the schedule itself, using the same day-granular rule
    as Phase 1: a job is late if its finish DATE is after its due DATE."""
    out = []
    for m in MACHINES:
        for step in result["sequences"][m]:
            days = (step["finish"].date() - step["due_date"].date()).days
            if days > 0:
                out.append((step["job_id"], m, step["finish"], step["due_date"], days))
    return sorted(out)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_schedule(result, start_time):
    print(f"\n--- CP-SAT schedule  (solver status: {result['status']}, "
          f"{result['wall_time']:.2f}s) ---")
    if result["status"] == "EMPTY":
        print("  Nothing to schedule.")
    elif result["status"] == "OPTIMAL":
        print("  Proven optimal for this objective.")
    else:
        print(f"  Best found: {result['total_changeover']} min; proven lower bound "
              f"{result['bound']:.0f} min. Not proven optimal - raise --time-limit.")
    for note in result["notes"]:
        print(f"  NOTE: {note}")

    for m in MACHINES:
        header = f"\n=== {m} (cap {MACHINE_MAX_SCREENS[m]} screens) ==="
        if not result["sequences"][m]:
            print(f"{header} (idle)")
            continue
        print(header)
        for step in result["sequences"][m]:
            print(f"  {step['job_id']} (PO {step['po_id']}): "
                  f"+{step['changeover_minutes']} min changeover, "
                  f"{len(step['screens'])} screens, runs "
                  f"{step['start'].strftime('%m/%d %H:%M')} -> "
                  f"{step['finish'].strftime('%m/%d %H:%M')}, "
                  f"due {step['due_date'].date()}")

    print(f"\nTotal changeover: {result['total_changeover']} min "
          f"({result['total_changeover'] / 60:.1f} h)   "
          f"makespan: {result['makespan_seconds'] / SECONDS_PER_HOUR:.1f}h   "
          f"machines used: {machines_used(result['sequences'])}/{len(MACHINES)}")

    late = late_jobs(result, start_time)
    if late:
        # Lateness under hard due dates is impossible by construction, so if it
        # ever shows up here it is a model bug, not a scheduling outcome.
        reason = ("hard constraint relaxed because it is unsatisfiable"
                  if result["due_dates"] == "relaxed"
                  else "UNEXPECTED - due dates were hard, this is a bug")
        print(f"\n{len(late)} job(s) MISS their due date ({reason}):")
        for job_id, m, finish, due, days in late:
            print(f"  {job_id} on {m}: finishes {finish.strftime('%m/%d %H:%M')}, "
                  f"due {due.date()} - late by {days} day(s)")
    elif result["sequences"] and any(result["sequences"].values()):
        print("\nAll scheduled jobs meet their due dates.")

    review = underbase_review_jobs(result)
    if review:
        reasons = {
            "override_without_layers":
                "underbase_override=force_on but the underbase column is BLANK - "
                "it says an underbase is needed without saying which screens",
            "override_contradicts_column":
                "underbase_override is set AND the underbase column names "
                "screens - the column is the real answer, so the override is "
                "redundant or contradictory",
        }
        print(f"\n{len(review)} job(s) need MANUAL UNDERBASE REVIEW - ambiguous "
              f"or contradictory underbase input, NOT resolved by guessing:")
        for job in review:
            ub = sorted(s for s in job["screens"] if s.startswith("UB_"))
            print(f"  {job['job_id']} (PO {job['po_id']}): "
                  f"underbase column {ub if ub else '(blank)'}, "
                  f"override={job['underbase_override']} "
                  f"[{job['underbase_status']}]")
            print(f"      {reasons.get(job['underbase_status'], 'see job data')}")
        print("  Scheduled with exactly the underbase screens their column "
              "names - none invented, none dropped. Fix the data and re-run.")

    if result["infeasible_capacity"]:
        print(f"\n{len(result['infeasible_capacity'])} job(s) CANNOT BE SCHEDULED AT ALL "
              f"(more screens than any machine holds - max is "
              f"{max(MACHINE_MAX_SCREENS.values())}):")
        for job in result["infeasible_capacity"]:
            print(f"  {job['job_id']} (PO {job['po_id']}): {len(job['screens'])} screens")
        print("  These are excluded from both totals below. They need a split, a "
              "redesign, or a bigger machine.")


def print_comparison(csv_path, greedy, result, uncapped, start_time):
    """Greedy vs CP-SAT. When the makespan cap is on, the unconstrained
    optimum is shown as a third column: it is the real floor on changeover,
    and the gap between the two CP-SAT columns is exactly what the cap costs."""
    g_total, c_total = greedy["total_changeover"], result["total_changeover"]
    saved = g_total - c_total
    pct = (saved / g_total * 100) if g_total else 0.0

    cols = [("Phase 1 greedy", g_total, greedy["makespan_seconds"],
             machines_used({m: s["run"] for m, s in greedy["machines"].items()}),
             len(greedy["late"]), ""),
            ("CP-SAT (capped)" if result["cap_applied"] else "CP-SAT",
             c_total, result["makespan_seconds"],
             machines_used(result["sequences"]),
             len(late_jobs(result, start_time)), result["status"])]
    if uncapped is not None:
        cols.append(("CP-SAT (no cap)", uncapped["total_changeover"],
                     uncapped["makespan_seconds"],
                     machines_used(uncapped["sequences"]),
                     len(late_jobs(uncapped, start_time)), uncapped["status"]))

    print(f"\n{'=' * REPORT_WIDTH}")
    print(f"PHASE 1 GREEDY  vs  PHASE 2 CP-SAT   -   {csv_path}")
    print(f"{'=' * REPORT_WIDTH}")
    print(f"{'':<24}" + "".join(f"{c[0]:>18}" for c in cols))
    print(f"{'total changeover (min)':<24}" + "".join(f"{c[1]:>18}" for c in cols))
    print(f"{'total changeover (h)':<24}" + "".join(f"{c[1] / 60:>18.1f}" for c in cols))
    print(f"{'makespan (h)':<24}"
          + "".join(f"{c[2] / SECONDS_PER_HOUR:>18.1f}" for c in cols))
    print(f"{'machines used':<24}" + "".join(f"{c[3]:>18}" for c in cols))
    print(f"{'late jobs':<24}" + "".join(f"{c[4]:>18}" for c in cols))
    print(f"{'solver status':<24}" + "".join(f"{c[5]:>18}" for c in cols))

    slower = result["makespan_seconds"] - greedy["makespan_seconds"]
    if saved > 0:
        print(f"\nCP-SAT saves {saved} min ({saved / 60:.1f} h) of setup vs the "
              f"greedy ({pct:.1f}% less)", end="")
    elif saved == 0:
        print(f"\nCP-SAT matches the greedy's {g_total} min of setup", end="")
    else:
        print(f"\nCP-SAT used {-saved} min MORE setup than the greedy - it did not "
              f"reach the greedy's solution in the time allowed", end="")
    if slower <= 0:
        print(", at equal or better makespan.")
    else:
        print(f", but takes {slower / SECONDS_PER_HOUR:+.1f}h longer overall "
              f"(the makespan cap was dropped - see the notes above).")

    if uncapped is not None:
        cost = result["total_changeover"] - uncapped["total_changeover"]
        if cost > 0:
            print(f"Holding the makespan at the greedy's "
                  f"{greedy['makespan_seconds'] / SECONDS_PER_HOUR:.1f}h costs {cost} min "
                  f"of extra setup: the unconstrained optimum is "
                  f"{uncapped['total_changeover']} min but takes "
                  f"{uncapped['makespan_seconds'] / SECONDS_PER_HOUR:.1f}h on "
                  f"{machines_used(uncapped['sequences'])} machine(s).")
        else:
            print("The makespan cap costs nothing here - the capped schedule is also "
                  "the unconstrained optimum.")
    if result["status"] not in ("OPTIMAL", "EMPTY"):
        print("(CP-SAT result not proven optimal - see status above.)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

START_TIME = datetime(2026, 8, 25, 8, 0)

DEFAULT_CSVS = ["sample_jobs.csv", "messy_jobs.csv",
                "real_po_anonymized_jobs.csv"]


def run_one(csv_path, start_time, time_limit, use_cap, quiet=False):
    jobs = load_jobs(csv_path)
    validate_jobs(jobs)
    greedy = greedy_reference(jobs, start_time)

    if greedy["total_changeover"] != greedy["changeover_as_phase1_reported"]:
        print(f"  WARNING {csv_path}: Phase 1 reports "
              f"{greedy['changeover_as_phase1_reported']} min of changeover but "
              f"replaying its schedule gives {greedy['total_changeover']} min. "
              f"The two phases disagree on the cost model.")

    # The cap is the greedy's makespan measured in the model's own integer
    # seconds, so it is exact - and the greedy schedule always satisfies it.
    cap = greedy["makespan_seconds"] if use_cap else None

    result = solve(jobs, start_time, makespan_cap_seconds=cap,
                   time_limit=time_limit, hint=greedy["hint"])
    if result is None:
        print(f"\n{csv_path}: MODEL INFEASIBLE even with due dates relaxed - "
              f"this is a bug, not bad data.")
        return None
    if result["status"] == "TIMEOUT":
        print(f"\n\n{'#' * REPORT_WIDTH}\n# {csv_path}  ({len(jobs)} jobs)\n"
              f"{'#' * REPORT_WIDTH}")
        print(f"\nNo schedule found within --time-limit {time_limit:g}s.")
        for note in result["notes"]:
            print(f"  NOTE: {note}")
        return None

    problems = verify(result, start_time)

    # The uncapped optimum is the true floor on changeover for this job set.
    uncapped = None
    if cap is not None and result["cap_applied"] is not None:
        uncapped = solve(jobs, start_time, makespan_cap_seconds=None,
                         time_limit=time_limit, hint=greedy["hint"])
        if uncapped is not None and uncapped["status"] == "TIMEOUT":
            uncapped = None
        if uncapped is not None:
            problems += [f"(uncapped run) {p}" for p in verify(uncapped, start_time)]

    if not quiet:
        print(f"\n\n{'#' * REPORT_WIDTH}\n# {csv_path}  ({len(jobs)} jobs)\n"
              f"{'#' * REPORT_WIDTH}")
        print_schedule(result, start_time)
        print_comparison(csv_path, greedy, result, uncapped, start_time)
        print("\nverification (independent replay of the solver's schedule):")
        if problems:
            for p in problems:
                print(f"  FAIL {p}")
        else:
            print("  PASS - changeover math, capacities, timings, overlap and due "
                  "dates all re-derived and agree.")

    return {"csv": csv_path, "greedy": greedy, "result": result,
            "uncapped": uncapped, "problems": problems}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="*", default=DEFAULT_CSVS,
                    help="job CSV(s) to schedule (default: all three test files)")
    ap.add_argument("--time-limit", type=float, default=DEFAULT_TIME_LIMIT_SECONDS,
                    help="solver time limit in seconds per stage (default 60)")
    ap.add_argument("--no-cap", action="store_true",
                    help="drop the makespan cap and show the unconstrained "
                         "minimum-changeover optimum (tends to pile everything "
                         "onto one machine)")
    ap.add_argument("--start", default=START_TIME.strftime("%Y-%m-%dT%H:%M"),
                    help="schedule start time, ISO format (default 2026-08-25T08:00)")
    args = ap.parse_args()

    start_time = datetime.fromisoformat(args.start)
    runs = [run_one(p, start_time, args.time_limit, not args.no_cap)
            for p in args.csv]

    print(f"\n\n{'=' * REPORT_WIDTH}\nSUMMARY - total changeover, minutes\n"
          f"{'=' * REPORT_WIDTH}")
    print(f"{'file':<28}{'greedy':>9}{'CP-SAT':>9}{'saved':>9}{'no cap':>9}"
          f"{'status':>12}{'verify':>8}")
    for r in runs:
        if r is None:
            continue
        g, c = r["greedy"]["total_changeover"], r["result"]["total_changeover"]
        u = r["uncapped"]["total_changeover"] if r["uncapped"] else c
        print(f"{r['csv']:<28}{g:>9}{c:>9}{g - c:>9}{u:>9}"
              f"{r['result']['status']:>12}"
              f"{'FAIL' if r['problems'] else 'PASS':>8}")
    print("'saved' = greedy - CP-SAT, at equal-or-better makespan.")
    print("'no cap' = minimum possible changeover if makespan is allowed to grow.")


if __name__ == "__main__":
    main()
