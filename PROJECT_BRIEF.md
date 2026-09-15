# Print Order Sequencing Optimizer — Project Brief

For: Southpoint Sportswear (screen printing manufacturer)
Purpose: IE portfolio project — minimize print machine changeover (setup)
time while respecting customer due dates.

Read this whole file before doing anything — it captures every decision
made so far so you don't have to re-derive them.

## The business problem

Southpoint prints on 6 screen-printing machines. Switching a machine from
one design/color job to another costs setup time: **5 minutes per screen**
(ink color OR underbase) that isn't already loaded from the previous job.
If two consecutive jobs share screens, you skip re-loading those and save
time. The goal: given a queue of orders, decide which machine runs which
job and in what sequence, to minimize total changeover time while still
hitting every due date.

## Core data model

**A "job"** = one (PO, design, blank garment color) combination. A single
PO can contain multiple designs (e.g. hoodies + shirts with different
artwork) — each becomes its own job. Quantity is summed across all sizes
within that PO+design+blank-color group (screens don't change by size).

**Screens** = underbase(s) + ink colors, combined into one flat set. There
is no special-casing between underbase and ink color — they behave
identically: 5 min setup each, reusable across jobs if already loaded.

**Job CSV schema.** Required columns: `job_id`, `po_id`, `due_date`,
`quantity`, `underbase`, `colors`. The `underbase` column names the actual
underbase screens the order needs — `White`, `Grey`, `Black`, or a
`;`-separated combination — or is blank for none; anything else is rejected.
Two optional columns may be absent entirely (`sample_jobs.csv` and
`messy_jobs.csv` omit both; `real_po_anonymized_jobs.csv` carries
`blank_color`).
Neither drives a scheduling decision:
- `blank_color` — optional; the garment color as written on the PO. Carried
  through for reporting only — it does **not** determine whether an underbase
  is needed (see the retired-system note under Known limits).
- `underbase_override` — optional; `force_on`, `force_off`, or blank. Cannot
  decide anything on its own now that the `underbase` column is explicit;
  ambiguous or contradictory combinations are flagged for manual review
  rather than resolved by guessing.

**Underbase rules** — SUPERSEDED. The bullets below describe underbase as a
function of blank darkness (white underbase on most blanks, an extra grey on
Black/Navy, Graphite Heather as an exception). **Confirmed with the shop:
that is not how the decision is actually made.** Underbase is a per-order,
per-design commercial decision, frequently price-driven — the customer
chooses how many layers they are paying for — and it can include white,
grey, **and black**. It is not derivable from the blank color, and is not
always tied to blank darkness at all.

What the scheduler does instead: the `underbase` column of the job CSV is
human-entered and authoritative, naming the actual screens (`White`, `Grey`,
`Black`, or a combination). See the schema above and the retired-system note
under Known limits. The darkness heuristics are kept here only as a record
of what was believed earlier — do not implement against them.

- ~85% of orders need at least a **white** underbase — the rule of thumb
  is "if the ink is a different color than the blank, you need white
  underbase." The other ~15% skip it intentionally for a design effect.
  *(Superseded: a useful prior for a data-entry default at best, not a rule.)*
- Only **Black** and **Navy** blanks need an *additional* **grey**
  underbase (2 underbases total: white + grey). *(Superseded.)*
- **Graphite Heather does NOT need the grey underbase**, despite being a
  dark blank — this was explicitly called out as an exception.
  *(Superseded — and a good illustration of why darkness was the wrong
  input: the exceptions were the rule.)*
- The same design can use *different actual ink colors* depending on the
  blank color (confirmed visually from a real art sheet: the same logo
  printed in 1 ink color on some blanks, 2 colors — with a white
  outline — on darker/brighter blanks, for contrast/legibility). So the
  screens lookup must be keyed on **(design, blank color)**, not design
  alone.
- Because exact ink colors live in a locked internal file (.prt format,
  only viewable on specific terminals), there's no way to auto-extract
  them. The plan is a **manual "ask once, remember forever" lookup
  table**: unique (design, blank color) combos get their screen list
  entered once, saved, and reused on every future run. New/unrecognized
  combos should be flagged so only genuinely new ones need input — most
  POs have far fewer unique designs than line items, so this scales fine.
- **Color names must be entered consistently.** The scheduler lowercases
  everything on load to catch casing mismatches (e.g. "Red" vs "red"),
  but it can't catch genuinely different words for the same shade (e.g.
  "Navy" vs "Dark Blue"). Stick to one agreed name per color.
- **This lookup table does not exist yet.** All color/screen data used in
  testing so far is placeholder/invented data, clearly not real. Filling
  this in with real numbers is a separate, still-open task.

## Machine constraints

Each machine has a max number of screens it can hold at once (this is the
ONLY machine eligibility rule — no other restrictions, e.g. no machine is
dedicated to certain garment types or customers):

| Machine | Max screens |
|---|---|
| M1 | 10 |
| M2 | 8 |
| M3 | 4 |
| M4 | 10 |
| M5 | 12 |
| M6 | 8 |

Machine throughput (units/hour) is currently a **rough placeholder**
derived from ~31 weeks of historical daily output per machine (from real
production reports), assuming an 8-hour shift:

| Machine | Units/hour (placeholder) |
|---|---|
| M1 | 220 |
| M2 | 230 |
| M3 | 435 |
| M4 | 210 |
| M5 | 200 |
| M6 | 195 |

Machine #3 is a real, confirmed standout in the historical data — it
consistently produces ~2x any other machine's daily output (~30% of total
volume across 31 weeks) despite having the LOWEST screen capacity (max 4).
Worth understanding why (high-speed dedicated machine for simple jobs?)
if there's a chance to ask the ops team.

## Due dates

Treated as a **hard constraint**. The design decision: the scheduler is
**stateless per run** — you feed in the current job list (with current due
dates) and current machine state (what's loaded right now) each time you
run it. A due date changing is just "edit that field, re-run" — no
special update logic needed. If a schedule can't meet a due date, it
should be flagged explicitly, never silently dropped.

## Phase 1 — DONE, tested, working

`scheduler.py` — a greedy heuristic:
- Sorts jobs by due-date urgency
- For each job, checks which machines have enough screen capacity
- Assigns to whichever eligible machine gives the **earliest projected
  finish time** (accounts for both that machine's current workload and
  changeover cost — NOT changeover cost alone)
- Flags jobs that are infeasible (no machine has enough capacity) or
  projected to finish after their due date

**Known bugs found and fixed during testing:**
1. Originally selected machines by lowest changeover cost alone, which
   caused every job to pile onto Machine 1 while the rest sat idle
   (ties always resolved to the first machine in iteration order). Fixed
   by switching to earliest-projected-finish-time as the selection rule.
2. Color names weren't case-normalized, so "Red" and "red" were treated
   as different screens — silently wrong changeover calculations, no
   error thrown. Fixed by lowercasing all screen names on load.

**Known limitation (by design, not a bug):** it's a myopic greedy — it
decides one job at a time and can miss chances to deliberately group
jobs with shared screens together, since it only ever looks at whichever
job is next, not the whole queue at once. This is the exact reason Phase
2 exists.

**Test files included** (all in this folder):
- `sample_jobs.csv` — original small test set (9 jobs)
- `messy_jobs.csv` — stress test: overdue jobs, due-date ties, exact
  capacity-boundary cases, whitespace noise, a zero-screen edge case (20
  jobs)
- `real_po_anonymized_jobs.csv` — REAL quantities and due date pulled from
  an actual customer PO (customer name and PO number withheld — this file is
  published), aggregated into 4 jobs by design+blank-color.
  Quantities were verified to sum to the PO's actual stated total (756
  units) — confirms the aggregation logic is correct on real messy PO
  data. **The screen/color data in this file is still a placeholder
  guess**, not real — it was estimated by eyeballing the art sheet image,
  which can't reliably show the true ink layer count (that lives in the
  locked file).
  `blank_color` in this file no longer affects scheduling (the color-based
  underbase system is retired), but the values are real and worth keeping.
  It was populated from the **primary source documents** — the customer PO itself and the July 2026 SPS Style & Color Guide
  — not from the job-ID color tokens. All four values are confirmed against
  those documents, not inferred: `HEATHER INDIGO` (TS and HD), `HEATHER
  MILITARY GREEN`, `HEATHER NATURAL`, each an exact key in
  `underbase_rules.BLANK_COLOR_DATA`. The mapping was cross-checked by
  quantity: the PO's line items sum to 216/168/168/204 per colorway,
  matching the four jobs and the PO's stated 756 total.
  An earlier pass had tried exact matching on the job-ID tokens alone; that
  approach was checked against the source documents and **one of its four
  results was wrong** — token `Natural` matched the key `NATURAL` literally,
  but the PO reads `HTR NATURAL` and the style guide offers only `HEATHER
  NATURAL` in the SP2200 hoodie, so it was corrected. The other two tokens
  (`INDIGO`, `MILGREEN`) had no exact key at all and were resolved the same
  way. **Note for anyone reading the published file:** the job IDs that
  carried those color tokens were genericized to `J1`–`J4` before
  publication, since the token also encoded the customer's design name. The
  tokens the story above refers to are no longer visible in the CSV — the
  narrative is kept because the lesson is worth keeping.
  Lesson worth keeping: the job-ID tokens are lossy abbreviations and
  are not a safe source for color identity, even when one happens to match a
  real key exactly.

## Phase 2 — DONE, tested, working

`scheduler_cp_sat.py` — an OR-Tools CP-SAT model that solves the same
problem to **proven optimality**. `test_cp_sat.py` is its test suite (116
checks). Both reuse `load_jobs()` and the machine tables from
`scheduler.py`, so Phase 1 and Phase 2 read the same CSV format.

ortools is installed in a local venv (`.venv/`), so run everything with
`.venv/bin/python` rather than the system `python3`:

    .venv/bin/python scheduler_cp_sat.py          # all three test CSVs
    .venv/bin/python test_cp_sat.py               # test suite

### Results (proven optimal on all three files)

| File | Greedy | CP-SAT | Saved | Uncapped optimum |
|---|---|---|---|---|
| sample_jobs.csv | 135 min | **85 min** | 50 min (37%) | 80 min |
| messy_jobs.csv | 280 min | **165 min** | 115 min (41%) | 155 min |
| real_po_anonymized_jobs.csv | 100 min | 100 min | 0 | 25 min |

The savings come at **no cost in makespan** — see the makespan cap below.

### The model

- **Decision variables:** `x[job, machine]` for assignment; sequencing is
  one `AddCircuit` per machine over that machine's eligible jobs plus a
  depot node, with unassigned jobs taking a self-loop. The circuit
  degenerates to a Hamiltonian path `depot → first → … → last → depot`,
  which is a genuine total order — what a sequence-dependent setup cost
  needs. Arc `i → j` carries the cost `5 × |screens(j) − screens(i)|`.
- **Capacity:** a job is only given `x` variables for machines whose
  screen limit it fits.
- **No double-booking:** `start[j] ≥ end[i] + setup[j]` on every used arc,
  plus redundant optional-interval `NoOverlap` per machine for propagation.
- **Objective:** total changeover = sum of every job's own setup cost
  (each job has exactly one predecessor, so this is exact).
- Time is modelled in **whole seconds**. The makespan cap is derived by
  replaying the greedy's own schedule through the model's integer
  arithmetic, so the cap is the greedy's makespan *exactly* — no rounding
  slack — and the greedy schedule always satisfies it, meaning the cap can
  never be the reason a solution isn't found.
- **Input validation** up front: negative quantities, duplicate job ids,
  and mismatched machine tables are rejected with a clear error rather
  than producing a confident wrong schedule.

### Decisions made in Phase 2 (each one is a knob, not a hidden assumption)

1. **Machines start with no screens loaded**, so the first job on each
   machine pays full setup — same as Phase 1, which keeps the comparison
   honest. `INITIAL_LOADED_SCREENS` in the script is the hook for the real
   "what's racked right now" input once that data exists.
2. **Minimising total changeover alone wants to use ONE machine**, because
   each extra machine costs a fresh full setup. So changeover is minimised
   subject to **makespan ≤ the Phase 1 greedy's makespan**: never slower
   than Phase 1, always less setup. `--no-cap` shows the unconstrained
   optimum, and every report prints both so the trade-off is visible. On
   the anonymized real PO that trade-off is stark: all four jobs share an identical
   5-screen set, so one machine costs 25 min of setup but takes 4.2 h,
   while the greedy's 1.4 h needs four machines and 100 min. Under the cap,
   100 min is provably the best possible — the greedy was already optimal
   *for that makespan*.
3. **Due dates stay hard, and are relaxed only when proven unsatisfiable**
   (messy_jobs has jobs whose due date predates the start time). The
   relaxation is lexicographic: fewest late jobs, then least total
   lateness, then least changeover. Nothing is ever silently dropped.
4. **Lateness is measured in whole days**, not seconds, because the
   due-date rule is itself day-granular. This is not cosmetic: measured in
   seconds, shaving a few hours off jobs already ~19 days overdue outbid 75
   minutes of real setup savings, and the solver split four
   identical-screen jobs across four machines to do it.
5. **Jobs no machine can hold** are excluded and flagged loudly (J8 at 13
   screens, K13 at 13 screens — the biggest machine holds 12). Phase 1
   excludes them too, so both totals cover the same job set.
6. The greedy schedule is fed to CP-SAT as a **solution hint**. It is
   always feasible for the model, so the solver never has to search for a
   first solution and CP-SAT can never come out worse than the greedy.
   A hint is advisory — it cannot change the optimum, and none of the
   settings that would turn a hint into a constraint
   (`fix_variables_to_their_hinted_value`, `repair_hint`) are set. The test
   suite checks this against brute force both hinted and unhinted, and with
   a deliberately bad hint, rather than taking the docs' word for it.

### Bugs found and fixed during Phase 2 testing

1. **Lateness in seconds instead of days** made the relaxation actively
   harmful — see decision 4 above. Found by running the real PO file and
   noticing CP-SAT "optimally" paid 100 min where 25 was available.
2. **Lateness variables bounded at the horizon.** A due date already in
   the past gives a negative deadline, so lateness can exceed the horizon.
   The whole relaxed model went infeasible and the script reported an
   internal error on real data.
3. **A solver timeout (`UNKNOWN`) was treated as `INFEASIBLE`.** On a
   60-job stress instance the hard-due-date model ran out of time, the
   script concluded the due dates were unsatisfiable, relaxed them, and
   reported 5 late jobs that the plain greedy met comfortably — a wrong
   answer presented as a hard constraint. Now only *proven* infeasibility
   triggers relaxation; a timeout is reported as a timeout.
4. **Deadline computed as `due_date + 24h`** instead of end-of-due-*date*.
   `load_jobs()` parses due dates as midnight so the CSVs hid it, but any
   due date carrying a time-of-day put the deadline on the next calendar
   day, and the hard constraint then accepted jobs the lateness report
   called late. The two halves of the program disagreed silently.

5. **The per-machine job sequence was inferred by sorting on start time.**
   Zero-length runs (quantity 0) tie on start time, so the reported order
   didn't match the solver's actual sequence and the verifier flagged bogus
   per-job changeover mismatches — the total was right, the attribution
   wrong. The order is now read back off the circuit arcs the solver chose,
   which is the solver's real decision rather than an inference from timing.
6. **A negative quantity** made the model infeasible for no stated reason;
   `solve()` returned `None` and callers died with a `TypeError`. Bad input
   is now rejected up front with a message naming the offending job.

Bugs 3–6 were all found by generated or probe data, not by the three CSVs
— worth remembering: the sample files are too clean to exercise this.

### How it's verified

- `verify()` **replays the solver's schedule in plain Python** and
  re-derives changeover, timings, capacity, overlap and due dates from
  scratch. It deliberately does *not* reuse the model's deadline
  arithmetic — doing so is exactly how bug 4 slipped through.
- `test_cp_sat.py` **brute-forces the true optimum** for 52 small random
  instances (every assignment × every sequence) and confirms CP-SAT
  matches — capped, uncapped, hinted and unhinted. This is the part that
  proves optimality; `verify()` only proves self-consistency.
- Edge cases covered: ties, exact capacity boundaries (12 screens → only
  M5), 13-screen impossible jobs, zero screens, zero quantity, zero-length
  runs that tie on start time, a single job, every job infeasible, an empty
  file, duplicate job ids, negative quantities, casing and whitespace
  noise, underbase-vs-ink name collisions, past due dates, same-day due
  dates, due dates carrying a time-of-day, binding and absurd makespan
  caps, deliberately bad solution hints, determinism, explicit underbase
  handling (Black parses and costs like White/Grey, misspellings rejected,
  Grey-without-White accepted, force_on-without-layers and
  override-alongside-column both flagged for review), and underbase screen
  reuse through the ordinary screen mechanism.
- The greedy's own reported changeover total is cross-checked against a
  replay through Phase 2's shared cost helper on every run, so the two
  phases can't silently drift apart on what a changeover costs.
- Hand-verified changeover chains on sample_jobs M6
  (10+5+10+10+25 = 60) and messy_jobs M6 (30+0+10+15+0 = 55).

### Known limits

- **Scale.** All three real files solve to proven optimality in under 4
  seconds. Around 30+ jobs the solver still beats the greedy by 35–45% but
  stops *proving* optimality inside 60 s — it returns `FEASIBLE` with a
  lower bound, and the report says so. Raise `--time-limit` for a tighter
  proof. Worth revisiting if real queues run large.
- **KNOWN SIMPLIFICATION, pending confirmation from the ops team: does the
  rack actually get stripped between jobs?** A machine's "loaded screens"
  is modelled as exactly the screen set of the job that ran immediately
  before — per the objective as stated above — so a machine does not
  accumulate a pool of screens across several earlier jobs. Two
  consequences: a job that shares screens with two jobs back gets no credit
  for them; and a 0-screen job (messy_jobs K19) presents an empty screen
  set to whatever follows it, so the next job pays full setup as if the rack
  had been stripped. CP-SAT exploits this by scheduling K19 first, where it
  costs nothing. **Phase 1 behaves identically**, so the greedy-vs-CP-SAT
  comparison is fair either way, and this is deliberately left alone until
  ops answers. If they confirm screens stay racked until something displaces
  them, this becomes an accumulating-pool model bounded by each machine's
  capacity — a change to **both** phases, not a bug in one of them. The same
  note is on `INITIAL_LOADED_SCREENS` in `scheduler_cp_sat.py`.
- **Underbase: explicit, per-order, human-entered data.** How many underbase
  layers a job gets, and which colors, is a commercial decision made per
  order/design — frequently **price-driven**, since the customer chooses how
  many layers they are paying for. It is **not** a function of how dark the
  blank garment is, and it is not always tied to blank darkness at all.
  The `underbase` column of the job CSV carries the real answer: `White`,
  `Grey`, `Black`, or a `;`-separated combination, or blank for none. Those
  become ordinary `UB_*` screens in the job's screen set, which means:
  - they cost `SETUP_MINUTES_PER_SCREEN` (5 min) each, exactly like an ink
    screen — an underbase *is* a screen, so there is no separate underbase
    time constant;
  - they are **reused by the ordinary screen mechanism**: a `UB_white`
    already loaded from the previous job on that machine is not in the set
    difference, so it is not re-charged. No underbase-specific reuse logic
    exists, or is needed. `reuse_demo.csv` demonstrates this end to end.
  A value outside {White, Grey, Black} is **rejected** at load rather than
  silently becoming a screen of its own — a typo like `Whit` used to pass
  through as `UB_whit`, costing setup for a screen that doesn't exist and
  breaking reuse against correctly-spelled neighbours.
  **NOT VALIDATED, deliberately:** whether Grey or Black require White to
  also be present. That was raised and never settled, so no such constraint
  is enforced. (In all current data, Grey never appears without White — 8
  rows are `White;Grey`, 14 are `White` alone, 13 are blank — so the
  question is untestable from the data we have.)
- **`underbase_override` can no longer decide anything on its own.** Now
  that underbase is explicit, an override can only agree with the column,
  contradict it, or assert a need without naming screens. The ambiguous
  combinations are **flagged for a human, never guessed**:
  - blank column + no override, or + `force_off` → no underbase. `force_off`
    is redundant with a blank column but harmless, and is **not** required
    for the skip to work.
  - column names screens, no override → explicit. Fine.
  - `force_on` + **blank** column → ambiguous: asserts an underbase without
    saying how many layers or which colors. **Flagged.** No layer count is
    ever invented.
  - any override + **non-blank** column → contradictory or redundant; the
    column is the real answer. **Flagged**, not silently resolved either way.
  Flagged jobs appear in the report's manual-review section alongside the
  other flagged-job lists, and are still scheduled with exactly the screens
  their column names — none invented, none dropped.
- **RETIRED: the color/luminance-based underbase system.** `underbase_rules.py`
  decided underbase from blank-garment luminance against a 0.20 threshold.
  Confirmed with the shop that this is simply not how the decision is made,
  so it was **retired, not fixed** — no threshold tuning addresses a wrong
  input variable. Nothing in the live pipeline imports or calls it; the
  scalar underbase cost and its bespoke reuse rule are gone with it, along
  with the three assumptions that used to hang off them (the unverified 0.20
  threshold, the placeholder per-layer constant, and the binary reuse rule).
  The file is **kept, not deleted**, with `BLANK_COLOR_DATA`,
  `MANUAL_REVIEW_COLORS` and the threshold constant untouched, and carries a
  module-level RETIRED notice explaining why. Its plausible future is as a
  **"suggested default" in a data-entry form** — pre-filling a likely
  underbase for a human to confirm or override before saving — a
  human-reviewed suggestion, never an automatic decision. Its own caveats
  (saturated colors read as "dark", the threshold never checked against real
  jobs) would still apply if it were ever revived.
  `blank_color` remains an optional column and is still carried through for
  reporting, but it **does not drive any scheduling decision**.
- Throughput numbers are still the Phase 1 placeholders, so all *times*
  (and therefore which due dates are missable, and the makespan cap) are
  only as good as those. The *changeover* numbers don't depend on them.

## Not yet done (future phases, don't start unless asked)

- Real design+blank-color→screens lookup table (currently empty). This is
  now the biggest thing standing between the solver and real use: the
  optimizer is correct, but it is optimizing over placeholder screen data.
- Multi-design-per-PO automatic splitting from actual PDF POs (Phase 1
  takes a pre-built CSV; parsing the real, differently-formatted vendor
  PDFs into that CSV automatically is separate, deliberately deferred
  work)
- Auto-detecting ink colors from art sheet images (a "nice to have"
  idea, explicitly shelved until the core pipeline works end to end)
