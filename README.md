# Print Order Sequencer

Scheduling optimizer for a screen-printing shop. Given a queue of print
orders, it decides **which machine runs which job, and in what order**, to
minimize the time spent swapping screens between jobs — while still hitting
every customer due date.

Built with Google OR-Tools **CP-SAT** constraint programming, and measured
against a greedy heuristic baseline so the improvement is quantified rather
than asserted.

## The problem

A screen-printing press has to be loaded with one screen per ink color (plus
any underbase layers). Loading a screen takes **5 minutes**. If two
consecutive jobs on the same press share a color, that screen stays mounted
and costs nothing the second time.

So the order you run jobs in matters. Run two jobs that share four colors
back to back and you save 20 minutes; run them on different machines and you
pay twice. Across a day's queue that adds up, and the sequencing decision is
exactly the kind of thing a solver does better than a person with a
spreadsheet.

Constraints the schedule has to respect:

- **Machine screen capacity** — each press holds a fixed maximum number of
  screens (4 to 12 across six machines). A job needing more screens than a
  press holds simply cannot run there.
- **Due dates** — treated as hard. A job that cannot make its date is
  flagged explicitly, never silently dropped.
- **No double-booking** — a machine runs one job at a time, and screen
  changes happen between jobs, not during them.

## Approach

Two schedulers solve the same problem, and the tool runs both so you can see
the difference:

**Phase 1 — greedy baseline** (`scheduler.py`). Takes jobs in due-date
order and puts each one on whichever machine would finish it soonest. Fast,
simple, and myopic: it decides one job at a time, so it cannot notice that
deliberately grouping two jobs would have saved a screen load.

**Phase 2 — CP-SAT** (`scheduler_cp_sat.py`). Models the whole queue at
once. Machine assignment is a boolean per job-machine pair; sequencing uses
one `AddCircuit` per machine over that machine's eligible jobs plus a depot
node, so the circuit collapses into a Hamiltonian path giving a genuine
total order. Each arc carries the sequence-dependent setup cost of running
one job immediately after another. The objective minimizes total changeover.

Because CP-SAT searches the whole space, it reports whether the answer is
**proven optimal** rather than merely good — a stronger claim than "better
than the naive approach."

One wrinkle worth knowing: minimizing total setup alone pushes the optimum
toward cramming every job onto a single machine, since each additional
machine used costs a fresh full screen load. That is technically optimal and
practically useless. So changeover is minimized **subject to the makespan
not exceeding the greedy's** — the result is never slower than the baseline
and always uses less setup time. `--no-cap` shows the unconstrained optimum
for comparison.

### Verification

Solver output is not taken on trust. Every run is independently replayed in
plain Python — re-deriving changeover costs, timings, capacities, overlap and
due dates from scratch — and any disagreement is reported. The test suite
additionally brute-forces the true optimum for small instances by enumerating
every possible machine assignment and every sequence, and confirms CP-SAT
matches.

```bash
.venv/bin/python test_cp_sat.py     # 130 checks
```

## How underbase is handled

An **underbase** is a base layer printed under the design so the ink reads
correctly on the garment. It occupies a screen like any ink color.

Underbase requirements are **explicit per-order data**, entered by whoever
keys in the job via the `underbase` column (`White`, `Grey`, `Black`, or a
combination). They are **not inferred** from the garment color.

That is a deliberate design decision, and it replaced an earlier approach.
The first version estimated underbase need from the garment's color
luminance — dark blanks get an underbase, light ones don't. That turned out
to be the wrong model: underbase is a **commercial decision per order**,
frequently price-driven, since the customer chooses how many layers they are
paying for. It does not follow from how dark the garment is. The inference
module is kept in the repo (`underbase_rules.py`) marked retired, with a note
on why and on its one plausible future use — pre-filling a suggested default
in a data-entry form for a human to confirm.

Because an underbase is just a screen, it needs no special cost or reuse
logic: a `UB_white` already mounted from the previous job is not re-charged,
by exactly the same mechanism that reuses ink colors.

Ambiguous input is **flagged for review, never guessed**. If a job says
"needs underbase" without naming the layers, or sets an override that
contradicts its own underbase column, it appears in a manual-review section
of the report and is scheduled with exactly the screens its column names —
none invented, none dropped.

## Setup

Requires Python 3.9+.

```bash
git clone https://github.com/ciz29/print-order-sequencer.git
cd print-order-sequencer
python3 -m venv .venv
.venv/bin/pip install ortools
```

OR-Tools is the only dependency.

## Running it

```bash
.venv/bin/python scheduler_cp_sat.py yourfile.csv --time-limit 60
```

Use `.venv/bin/python` rather than plain `python3` so the virtualenv's
OR-Tools is picked up. Options:

| flag | meaning |
|---|---|
| `--time-limit N` | solver budget in seconds per stage (default 60). Raise it if a run reports `FEASIBLE` instead of `OPTIMAL` |
| `--no-cap` | drop the makespan cap and show the unconstrained minimum-changeover optimum |
| `--start ISO` | schedule start time (default `2026-08-25T08:00`) |

With no filename it runs the three bundled job files.

## Input format

One row per job. A job is one (PO, design, garment color) combination, with
quantity summed across sizes — screens don't change by size.

| column | required | meaning |
|---|---|---|
| `job_id` | yes | unique identifier; duplicates are rejected |
| `po_id` | yes | purchase order this job belongs to |
| `due_date` | yes | `YYYY-MM-DD`, treated as a hard constraint |
| `quantity` | yes | units; drives run time via machine throughput |
| `underbase` | yes (may be blank) | `White`, `Grey`, `Black`, or `;`-separated. Blank means no underbase |
| `colors` | yes (may be blank) | ink colors, `;`-separated |
| `blank_color` | no | garment color. Carried for reporting only; does not affect scheduling |
| `underbase_override` | no | `force_on`, `force_off`, or blank. Cannot decide anything on its own; ambiguous combinations are flagged for review |

The two optional columns may be omitted from the file entirely.

```csv
job_id,po_id,due_date,quantity,underbase,colors
J1,PO1001,2026-09-05,480,White;Grey,Red;Navy;Black
J2,PO1001,2026-09-05,240,White,Red;Navy
J3,PO1002,2026-09-03,150,,Red;Blue
```

Names are lowercased and whitespace-trimmed on load, so `" Red "` and `red`
are the same screen. Genuinely different words for the same shade (`Navy` vs
`Dark Blue`) are not reconciled — use one name per color. An `underbase`
value outside White/Grey/Black is rejected rather than silently becoming a
screen of its own.

## Example result

`sample_jobs.csv`, 9 jobs across 6 machines:

| | greedy baseline | CP-SAT |
|---|---|---|
| total changeover | **135 min** | **85 min** |
| makespan | 11.6 h | 11.6 h |
| machines used | 6 | 3 |
| status | — | proven optimal |

**37% less setup time at an identical finish time.** The saving comes from
sequencing: on machine M6 the solver runs J7 → J1 → J2, where J7 loads 8
screens (40 min), J1 reuses three of them and pays only 10 min for its two
new colors, and J2's three screens are all already mounted so it pays
nothing and starts the moment J1 ends.

The same file also demonstrates the flagging behavior: job J8 needs 13
screens and the largest press holds 12, so it is reported as unschedulable —
needing a split, a redesign, or a bigger machine — rather than being quietly
omitted.

## Bundled job files

| file | what it is |
|---|---|
| `sample_jobs.csv` | 9 jobs, hand-made. The worked example above |
| `messy_jobs.csv` | 20 jobs, stress test: past-due dates, due-date ties, exact capacity boundaries, whitespace and casing noise, a zero-screen job, an unschedulable job |
| `real_po_anonymized_jobs.csv` | 4 jobs. Quantities, due date and garment colors are from a real purchase order; ink colors are placeholders |
| `reuse_demo.csv` | 2 jobs, hand-crafted to demonstrate screen reuse. Not a real order |
| `review_flags_demo.csv` | 2 jobs, hand-crafted to demonstrate the two manual-review flags. Not a real order |

## Scope

What this is: a scheduling and sequencing optimizer that reads a prepared
CSV and produces an optimized machine schedule, with verification.

What it is **not**, and does not claim to be:

- There is **no PDF or purchase-order import**. Vendor POs arrive in varying
  formats; turning them into the CSV above is manual, and deliberately out
  of scope.
- There is **no order-entry UI**. Input is a CSV file.
- Machine throughput figures are **placeholders** derived from historical
  daily output, not measured per-job cycle times. They affect projected
  finish times and therefore which due dates are reported as missable; they
  do **not** affect the changeover figures, which are the point of the tool.
- The design-to-ink-color lookup is not built. The color data in the bundled
  files is illustrative.

## Files

| file | role |
|---|---|
| `scheduler_cp_sat.py` | Phase 2 CP-SAT model, reporting, CLI |
| `scheduler.py` | Phase 1 greedy baseline, CSV loading, shared cost model |
| `test_cp_sat.py` | test suite: brute-force optimality checks, edge cases, regressions |
| `underbase_rules.py` | retired luminance-based inference, kept for reference |
| `PROJECT_BRIEF.md` | development history: decisions, bugs found, open questions |

`PROJECT_BRIEF.md` is the working record of how the project got here —
including the modelling bugs found during testing and why each was fixed.
It is kept deliberately separate from this README.
