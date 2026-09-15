"""
Print Order Sequencing Optimizer - Phase 1

A greedy heuristic scheduler that assigns print jobs to machines and
sequences them to minimize screen changeover time, while respecting due
dates.

Phase 1 scope (by design):
- Input is a simple CSV of jobs (see sample_jobs.csv for the format).
- One row = one job. Splitting a single PO into multiple jobs (when a PO
  has several different designs) is NOT done here - parsing the real vendor
  PDFs into this CSV format is deferred to a later phase, and is not part
  of Phase 2 either. Phase 2 takes the same pre-built CSV this does.
- Machine assignment + sequencing uses a greedy heuristic (due-date order,
  earliest-projected-finish machine). Phase 2 (scheduler_cp_sat.py) solves
  the same problem to proven optimality with OR-Tools CP-SAT; this file is
  kept as the baseline it is measured against, not replaced by it.
"""

import csv
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Config - edit these as real numbers become available
# ---------------------------------------------------------------------------

# An underbase screen costs exactly the same as an ink screen: 5 min to load.
# That covers underbase too, so there is no separate underbase time constant.
# (The shop's confirmed "5 minutes per underbase layer" is this number - an
# underbase IS a screen, so it is priced by the same rule.)
SETUP_MINUTES_PER_SCREEN = 5

# Underbase is EXPLICIT, per-order, human-entered data: whoever keys the job
# in writes which underbase screens it needs. It is a commercial decision per
# order/design - often price-driven, since the customer decides how many
# layers they are paying for - so it is NOT derivable from the blank colour.
# Anything outside this set is a data-entry error and is rejected rather than
# silently turned into a screen nobody meant to load.
VALID_UNDERBASE_COLORS = {"white", "grey", "black"}

# The optional underbase_override column. Underbase is explicit now, so an
# override cannot decide anything on its own - it can only agree with the
# underbase column, contradict it, or assert a need without naming the
# screens. See _annotate_underbase(): the ambiguous combinations are flagged
# for a human, never resolved by guessing.
UNDERBASE_OVERRIDE_VALUES = {"force_on": True, "force_off": False, "": None}

# Max number of screens (ink colors + underbases combined) each machine can
# hold at once.
MACHINE_MAX_SCREENS = {
    "M1": 10,
    "M2": 8,
    "M3": 4,
    "M4": 10,
    "M5": 12,
    "M6": 8,
}

# Rough throughput baseline in units/hour, derived from ~31 weeks of
# historical daily output per machine, assuming an 8-hour shift. This is a
# PLACEHOLDER - swap in real per-job cycle time data once it's collected.
# It only exists so the scheduler can estimate finish times and flag jobs
# that won't make their due date.
MACHINE_UNITS_PER_HOUR = {
    "M1": 220,
    "M2": 230,
    "M3": 435,
    "M4": 210,
    "M5": 200,
    "M6": 195,
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_jobs(csv_path):
    """Read the job CSV and return a list of job dicts, each with a
    'screens' set combining underbase(s) and ink colors. Underbase names are
    prefixed (UB_) so they're never confused with a same-named ink color.

    Required columns: job_id, po_id, due_date, quantity, underbase, colors.
    The `underbase` column holds the underbase screens this order actually
    needs - White, Grey, Black, or a ';'-separated combination - or is blank
    for no underbase. It is human-entered, per-order data, not inferred.

    Two OPTIONAL columns, both defaulting to blank:
      blank_color        - the garment colour as written on the PO. Carried
                           through for reporting only; it does NOT determine
                           whether an underbase is needed (see the RETIRED
                           note in underbase_rules.py).
      underbase_override - "force_on" / "force_off" / blank. Cannot decide
                           anything on its own now that underbase is
                           explicit; ambiguous combinations are flagged for a
                           human by _annotate_underbase()."""
    jobs = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            underbase = [_underbase_screen(c, row["job_id"], csv_path)
                         for c in row["underbase"].split(";") if c.strip()]
            colors = [c.strip().lower() for c in row["colors"].split(";") if c.strip()]
            job = {
                "job_id": row["job_id"],
                "po_id": row["po_id"],
                "due_date": datetime.strptime(row["due_date"], "%Y-%m-%d"),
                "quantity": int(row["quantity"]),
                "screens": set(underbase) | set(colors),
                "blank_color": (row.get("blank_color") or "").strip(),
            }

            raw = (row.get("underbase_override") or "").strip().lower()
            if raw not in UNDERBASE_OVERRIDE_VALUES:
                raise ValueError(
                    f"{csv_path}: job {job['job_id']} has underbase_override "
                    f"{raw!r}; allowed values are "
                    f"{sorted(k for k in UNDERBASE_OVERRIDE_VALUES if k)} or blank")
            job["underbase_override"] = UNDERBASE_OVERRIDE_VALUES[raw]

            _annotate_underbase(job)
            jobs.append(job)
    return jobs


def _underbase_screen(raw_value, job_id, csv_path):
    """Turn one entry from the `underbase` column into a UB_ screen name.

    Rejects anything outside VALID_UNDERBASE_COLORS. A typo here used to pass
    straight through and silently become a screen of its own ("Whit" ->
    UB_whit), which both costs setup time for a screen that does not exist
    and breaks reuse against the correctly-spelled UB_white on neighbouring
    jobs - a wrong answer with no error.

    NOT VALIDATED, DELIBERATELY: whether Grey (or Black) requires White to
    also be present. That was raised as an open question and never settled,
    so no such constraint is enforced here."""
    name = raw_value.strip().lower()
    if name not in VALID_UNDERBASE_COLORS:
        raise ValueError(
            f"{csv_path}: job {job_id} has underbase {raw_value.strip()!r}; "
            f"allowed values are {sorted(VALID_UNDERBASE_COLORS)} "
            f"(';'-separated for more than one) or blank")
    return f"UB_{name}"


def has_explicit_underbase(job):
    """True if this job names its underbase screen(s) in the `underbase`
    column. That column is the ONLY source of underbase truth."""
    return any(s.startswith("UB_") for s in job["screens"])


def _annotate_underbase(job):
    """Flag rows whose underbase input is ambiguous or self-contradictory.

    Underbase is explicit, per-order, human-entered data, so there is nothing
    left to infer - the `underbase` column already states the answer, and its
    screens are costed by the ordinary screen mechanism like any other screen.
    The only open question per row is whether the input makes sense:

      - blank column, no override (or force_off)  -> no underbase. Fine.
        force_off is redundant with a blank column but harmless, and is NOT
        required for the skip to work.
      - column names screens, no override         -> explicit. Fine.
      - force_on with a BLANK column              -> AMBIGUOUS. It asserts an
        underbase is needed without saying how many layers or which colours.
        Flagged for a human; a layer count is never guessed.
      - any override alongside a NON-blank column -> CONTRADICTORY or
        redundant: the column already gives the real answer. Flagged rather
        than silently resolved in either direction."""
    override = job["underbase_override"]
    explicit = has_explicit_underbase(job)

    if override is not None and explicit:
        status, review = "override_contradicts_column", True
    elif override is True:
        status, review = "override_without_layers", True
    elif explicit:
        status, review = "explicit", False
    else:
        status, review = "none", False

    job["underbase_status"] = status
    job["needs_underbase_review"] = review
    return job
    return job


# ---------------------------------------------------------------------------
# Setup cost - the ONE definition of what a changeover costs, shared by the
# Phase 1 greedy and the Phase 2 CP-SAT model so they cannot drift apart.
# ---------------------------------------------------------------------------

def changeover_minutes(prev_screens, screens):
    """Setup cost of running `screens` right after `prev_screens` on the same
    machine: 5 min for every screen not already loaded.

    THIS IS THE WHOLE COST MODEL. Underbase screens (UB_white, UB_grey,
    UB_black) are ordinary members of `screens`, so they are priced here like
    any other screen AND they get reuse here for free: a UB_white already
    loaded from the previous job is not in the set difference, so it is not
    re-charged. No underbase-specific cost or reuse logic exists or is needed.
    The retired colour-guessing path had a separate scalar underbase cost
    precisely because its underbase was not a screen; now that underbase is
    always explicit, that whole mechanism is gone."""
    return len(screens - prev_screens) * SETUP_MINUTES_PER_SCREEN


def schedule_jobs(jobs, start_time):
    """Greedy heuristic: process jobs in due-date order. For each job, assign
    it to whichever eligible machine (enough screen capacity) gives the
    EARLIEST projected finish time - accounting for both that machine's
    current workload (clock) and its changeover cost. Using changeover cost
    alone (an earlier version of this) caused every job to pile onto one
    machine while the rest sat idle, since an idle machine's changeover
    isn't the only thing that matters - its availability does too."""

    machines = {
        m: {"clock": start_time, "loaded_screens": set(), "run": []}
        for m in MACHINE_MAX_SCREENS
    }
    flagged_jobs = []

    for job in sorted(jobs, key=lambda j: j["due_date"]):
        num_screens = len(job["screens"])
        eligible = [m for m in machines if num_screens <= MACHINE_MAX_SCREENS[m]]

        if not eligible:
            flagged_jobs.append((job, "no machine has enough screen capacity"))
            continue

        best_machine, best_finish, best_changeover = None, None, None
        for m in eligible:
            setup = changeover_minutes(machines[m]["loaded_screens"], job["screens"])
            run_minutes = (job["quantity"] / MACHINE_UNITS_PER_HOUR[m]) * 60
            projected_finish = (machines[m]["clock"]
                                 + timedelta(minutes=setup)
                                 + timedelta(minutes=run_minutes))
            if best_finish is None or projected_finish < best_finish:
                best_machine, best_finish, best_changeover = m, projected_finish, setup

        # Named `best_setup`, not `changeover_minutes`: a local of that name
        # would shadow the changeover_minutes() function called just above and
        # make it unreachable for the whole of this function.
        best_setup = best_changeover
        mstate = machines[best_machine]
        job_start = mstate["clock"] + timedelta(minutes=best_setup)
        job_finish = best_finish

        mstate["run"].append({
            "job_id": job["job_id"], "po_id": job["po_id"],
            "changeover_minutes": best_setup,
            "start": job_start, "finish": job_finish, "due_date": job["due_date"],
        })
        mstate["clock"] = job_finish
        mstate["loaded_screens"] = job["screens"]

        if job_finish.date() > job["due_date"].date():
            flagged_jobs.append((job, f"projected finish {job_finish.date()} is after due date"))

    return machines, flagged_jobs


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(machines, flagged_jobs):
    total_changeover = 0

    for m, state in machines.items():
        print(f"\n=== {m} ===")
        if not state["run"]:
            print("  (no jobs assigned)")
            continue
        for step in state["run"]:
            total_changeover += step["changeover_minutes"]
            print(f"  {step['job_id']} (PO {step['po_id']}): "
                  f"+{step['changeover_minutes']} min changeover, "
                  f"runs {step['start'].strftime('%m/%d %H:%M')} -> "
                  f"{step['finish'].strftime('%m/%d %H:%M')}, "
                  f"due {step['due_date'].date()}")

    print(f"\nTotal changeover time across all machines: "
          f"{total_changeover} minutes ({total_changeover / 60:.1f} hours)")

    if flagged_jobs:
        print(f"\n{len(flagged_jobs)} job(s) flagged:")
        for job, reason in flagged_jobs:
            print(f"  {job['job_id']} (PO {job['po_id']}): {reason}")
    else:
        print("\nAll jobs meet their due dates.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    jobs = load_jobs("sample_jobs.csv")
    machines, flagged_jobs = schedule_jobs(jobs, start_time=datetime(2026, 8, 25, 8, 0))
    print_report(machines, flagged_jobs)
