"""
Underbase determination logic for the Southpoint print order sequencer.

==========================================================================
RETIRED FROM THE LIVE PIPELINE. NOTHING IN THIS FILE IS CALLED ANY MORE.
==========================================================================
Neither scheduler.py nor scheduler_cp_sat.py imports this module. It is
kept, not deleted, and its data is kept exactly as measured.

WHY IT WAS RETIRED. This module decides whether a job needs an underbase by
looking at how dark the blank garment is. Confirmed with the shop: that is
not how the decision is actually made. Underbase is a per-order/per-design
commercial decision - frequently price-driven, since the customer chooses
how many layers they are paying for - and it can include white, grey and
black. So blank darkness is not the input, and no threshold tuning would
have fixed that; the mechanism itself was wrong, which is why this is a
retirement rather than a bug fix.

WHAT REPLACED IT. Underbase is now explicit, human-entered data: the
`underbase` column of the job CSV names the actual screens (White, Grey,
Black, or a combination). Those become ordinary UB_* screens, costed and
reused by the same screen mechanism as every ink colour. See
scheduler.py: load_jobs() and changeover_minutes().

WHAT IT MIGHT STILL BE GOOD FOR. A "suggested default" in a data-entry
form - pre-filling a likely underbase for a human to confirm or override
before the job is saved. A human-reviewed suggestion, never an automatic
decision. If it is ever revived for that, note that its own caveats below
still stand, and the 0.20 threshold remains unverified against real jobs.

Color luminance values were measured by sampling product photos from the
July 2026 SPS Style & Color Guide (relative luminance, 0=black, 1=white).
These are a starting point, not verified production truth -- see
MANUAL_REVIEW_COLORS below for saturated colors whose luminance score may
not match how the pressroom actually treats them (e.g. RED, ROYAL read as
"dark" by pure luminance even though they are perceived as vivid, not dark --
saturated colors often need underbase for opacity reasons the luminance
formula does not capture).
"""

# Luminance threshold below which a blank is treated as requiring underbase.
# Candidate starting value -- confirm against a handful of real known
# pressroom examples (see the conversation this came from) before trusting it.
UNDERBASE_LUMINANCE_THRESHOLD = 0.20

# name -> (average RGB sampled from catalog photos, relative luminance)
BLANK_COLOR_DATA = {
    "BLACK": ((34, 34, 36), 0.016),
    "CHOCOLATE": ((53, 40, 32), 0.0238),
    "NAVY": ((45, 53, 75), 0.0364),
    "PURPLE": ((75, 12, 140), 0.0365),
    "FOREST GREEN": ((46, 69, 53), 0.0509),
    "CHARCOAL": ((71, 76, 77), 0.0704),
    "RED": ((178, 31, 43), 0.1062),
    "ASPHALT": ((91, 92, 97), 0.1074),
    "BLUE JEAN": ((78, 97, 124), 0.1198),
    "HEATHER MILITARY GREEN": ((83, 108, 87), 0.1325),
    "MILITARY GREEN": ((93, 108, 84), 0.1369),
    "HEATHER RED": ((181, 62, 72), 0.1374),
    "ROYAL": ((68, 96, 198), 0.1377),
    "HEATHER BASIL": ((92, 112, 89), 0.1458),
    "WINE": ((132, 108, 128), 0.1719),
    "GRAPHITE HEATHER": ((114, 116, 115), 0.1731),
    "HEATHER INDIGO": ((90, 119, 144), 0.1753),
    "HEATHER PURPLE": ((126, 102, 162), 0.1762),
    "HEATHER SPORT GREEN": ((88, 126, 103), 0.1798),
    "HEATHER ROYAL": ((86, 124, 184), 0.2122),
    "ORANGE": ((224, 84, 55), 0.2246),
    "HEATHER IRISH GREEN": ((67, 147, 100), 0.2298),
    "HEATHER BERRY": ((194, 102, 164), 0.2368),
    "HELICONIA": ((220, 82, 150), 0.2374),
    "HEATHER GRAPHITE": ((138, 138, 138), 0.2542),
    "HEATHER SPORT ROYAL": ((101, 142, 203), 0.2642),
    "SANDSTONE": ((152, 146, 130), 0.2884),
    "HEATHER TERRACOTTA": ((196, 138, 119), 0.3124),
    "CAROLINA BLUE": ((117, 157, 199), 0.3202),
    "SAGE": ((140, 160, 146), 0.3282),
    "TANGERINE": ((242, 124, 99), 0.3419),
    "CORAL SILK": ((242, 124, 124), 0.3475),
    "PARAGON": ((178, 153, 158), 0.3503),
    "AZALEA": ((240, 122, 171), 0.3579),
    "TROPICAL BLUE": ((0, 188, 208), 0.4052),
    "SPORT GREY": ((177, 178, 179), 0.4468),
    "TERRACOTTA": ((251, 159, 142), 0.4726),
    "BLUSH": ((214, 186, 183), 0.5283),
    "ICE GREY": ((201, 192, 189), 0.5379),
    "CHALKY MINT": ((142, 210, 201), 0.5606),
    "SAND": ((209, 200, 181), 0.582),
    "LIGHT PINK": ((235, 194, 216), 0.612),
    "YELLOW HAZE": ((243, 205, 146), 0.6479),
    "IVORY": ((226, 217, 196), 0.6964),
    "MINT": ((178, 234, 168), 0.7118),
    "HEATHER NATURAL": ((227, 220, 211), 0.7222),
    "WHITE": ((224, 224, 224), 0.7454),
    "ASH": ((225, 225, 225), 0.7529),
    "NATURAL": ((234, 225, 208), 0.759),
    "LIGHT BLUE": ((192, 233, 252), 0.7649),
}

# Colors within ~0.03 of the threshold, or saturated colors whose true
# luminance score is a poor proxy for real underbase need. Always flagged
# for manual confirmation even though they have a known luminance value.
MANUAL_REVIEW_COLORS = {
    "HEATHER SPORT GREEN",  # 0.180 -- close to threshold
    "HEATHER ROYAL",        # 0.212 -- close to threshold
    "ORANGE",               # 0.225 -- close to threshold, also saturated
    "RED",                  # saturated -- luminance formula underweights red
    "ROYAL",                # saturated -- luminance formula underweights blue
    "HELICONIA",            # saturated pink/magenta
    "CORAL SILK",           # saturated
    "TANGERINE",            # saturated
    "AZALEA",               # saturated
}


def normalize_color_name(blank_color):
    return blank_color.strip().upper()


def determine_underbase(blank_color, design_underbase_override=None):
    """
    Decide whether a job needs an underbase layer.

    blank_color: the garment color as it appears on the purchase order.
    design_underbase_override: pass True to force underbase on, False to
        force it off (e.g. a design that intentionally skips underbase for
        a distressed/vintage look, regardless of blank darkness), or None
        to fall through to the blank-color rule below. This always wins --
        a design choice overrides a blank-color guess.

    Returns (requires_underbase: bool, status: str, needs_review: bool)
        status is one of:
          "design_override"    -- design explicitly forced this
          "known_color"         -- matched a measured blank color
          "known_color_review"  -- matched, but flagged for manual check
          "unknown_color"       -- not in our data; defaulted conservatively
    """
    # 1. A design's own instruction always wins over any blank-color guess.
    if design_underbase_override is not None:
        return design_underbase_override, "design_override", False

    # 2. Look up the blank color against measured luminance data.
    name = normalize_color_name(blank_color)
    if name in BLANK_COLOR_DATA:
        rgb, luminance = BLANK_COLOR_DATA[name]
        requires = luminance < UNDERBASE_LUMINANCE_THRESHOLD
        if name in MANUAL_REVIEW_COLORS:
            return requires, "known_color_review", True
        return requires, "known_color", False

    # 3. Unknown / special-order color (e.g. a customer's custom blank).
    # Conservative default: assume underbase IS required rather than risk
    # a visibly under-printed job. Always flagged so a human confirms it
    # before the run is scheduled, and so the table can be extended later.
    return True, "unknown_color", True


def annotate_job(job):
    """
    job: a dict-like purchase-order line with at least "blank_color", and
    optionally "underbase_override" (True/False/None) set by whoever keys
    in the design if it's meant to intentionally skip/force underbase.

    Adds "requires_underbase", "underbase_status", "needs_underbase_review"
    to the job in place, and returns it.
    """
    override = job.get("underbase_override")
    requires, status, needs_review = determine_underbase(
        job["blank_color"], design_underbase_override=override
    )
    job["requires_underbase"] = requires
    job["underbase_status"] = status
    job["needs_underbase_review"] = needs_review
    return job


if __name__ == "__main__":
    # quick smoke test
    sample_jobs = [
        {"blank_color": "Black"},
        {"blank_color": "White"},
        {"blank_color": "Red"},
        {"blank_color": "Navy", "underbase_override": False},  # distressed look, skip on purpose
        {"blank_color": "Bahama Blue"},  # not in our catalog data -> unknown fallback
    ]
    for job in sample_jobs:
        annotate_job(job)
        print(job)
