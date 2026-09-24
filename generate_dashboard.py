#!/usr/bin/env python3
"""Render a triage run as one self-contained HTML page.

    python3 generate_dashboard.py [results.json] [--alerts FILE] [--out dashboard.html]

The page answers three questions, in this order:
  1. What paged someone, and what is waiting for a human?
  2. Where did each alert land against the policy? P(page) is Jev's judgment;
     the marks at 0.20 and 0.80 are the code's decision.
  3. Can the policy be trusted yet? Outcomes against labels, calibration, and
     the safety invariants.

Reads the v2 results.json that triage.py writes. Standard library only.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
from datetime import timezone

import evaluate
import triage

ACTION_LABEL = {
    "PAGE_NOW": "Paged now", "PAGE": "Paged", "REVIEW": "Waiting for review",
    "TICKET": "Ticketed", "LOG": "Logged", "DROP": "Dropped", "DEDUP": "Linked",
}
# Rows are grouped by what a person experiences, most urgent first. Linked
# alerts sit under the incident they joined, whatever their own action.
GROUPS = [
    ("page-now", "Paged now", ("PAGE_NOW",)),
    ("page", "Paged", ("PAGE",)),
    ("review", "Waiting for a human", ("REVIEW",)),
    ("ticket", "Ticketed", ("TICKET",)),
    ("quiet", "Logged or dropped", ("LOG", "DROP")),
]
URGENCY = {"page": 0, "linked": 1, "review": 2, "ticket": 3, "quiet": 4}
LEGEND = [("page", "Paged"), ("linked", "Linked to a paged incident"),
          ("review", "Waiting for review"), ("ticket", "Ticketed"),
          ("quiet", "Logged or dropped")]
OUTCOME_LABEL = {
    "silent_miss": "Needed a human, reached no one",
    "missed_page": "Should have paged, got a ticket",
    "delayed_page": "Should have paged, got a review",
    "false_page": "Paged when it shouldn't have",
    "duplicate_page": "Paged twice for one incident",
    "misrouted": "Reached the wrong team",
    "wrong_link": "Linked to the wrong incident",
    "extra_review": "Review that wasn't needed",
    "extra_ticket": "Ticket for noise",
}
TAG_LABEL = {
    "silent_miss": "Reached no one", "missed_page": "Needed a page",
    "delayed_page": "Needed a page sooner", "false_page": "Unneeded page",
    "duplicate_page": "Duplicate page", "misrouted": "Wrong team",
    "wrong_link": "Wrong link", "extra_review": "Unneeded review",
    "extra_ticket": "Ticket for noise",
}
SEVERE = ("silent_miss", "missed_page", "delayed_page", "misrouted")
AGREEMENT_LABEL = {"actionable": "Actionable", "page": "Page or not", "severity": "Severity",
                   "team": "Owner", "duplicate_of": "Cause"}
TITLE_PREFIX = re.compile(r"^(Firing|Warning|Info|Critical|Alert):\s*", re.IGNORECASE)
# Dots closer than the gap (in P(page) units) stack upward. Wide screens need a
# smaller gap than phones; each dot gets a row for both and CSS picks one.
RAIL_GAPS = {"d": 0.018, "m": 0.035}
MAX_STACK = 10  # dots above this in one column are counted in a "+n"
STAGGER_TOTAL_MS = 900  # the whole dot entry animation finishes within this
LABEL_GAP = 0.055  # "+n" labels closer than this share one merged label

CSS = """
:root {
  --ground: #F3F5F6; --face: #E7EBEE; --ink: #1C232B; --graphite: #5A6572; --rule: #CBD2D8;
  --accent: #C21F3A; --accent-mid: #D87F8F; --accent-wash: rgba(194, 31, 58, .10); --accent-ink: #FFFFFF;
  --radius: 4px; --dot: 14px; --step: 18px;
  --sans: "Archivo", "Helvetica Neue", Helvetica, Arial, system-ui, sans-serif;
  color-scheme: light;
  box-sizing: border-box;
  padding-top: env(safe-area-inset-top, 0px);
  padding-bottom: env(safe-area-inset-bottom, 0px);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #1B2129; --face: #232A33; --ink: #E5E9ED; --graphite: #9AA5B1; --rule: #37404B;
    --accent: #FF5A72; --accent-mid: #984051; --accent-wash: rgba(255, 90, 114, .12); --accent-ink: #1B2129;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --ground: #1B2129; --face: #232A33; --ink: #E5E9ED; --graphite: #9AA5B1; --rule: #37404B;
  --accent: #FF5A72; --accent-mid: #984051; --accent-wash: rgba(255, 90, 114, .12); --accent-ink: #1B2129;
  color-scheme: dark;
}
html { scroll-padding-top: env(safe-area-inset-top, 0px); -webkit-text-size-adjust: 100%; }
*, *::before, *::after { box-sizing: inherit; }
body { margin: 0; background: var(--ground); color: var(--ink); font-family: var(--sans);
  font-size: 16px; line-height: 1.5; }
.page { max-width: 1180px; margin: 0 auto; padding: clamp(20px, 4vw, 44px) clamp(16px, 4vw, 40px) 56px; }
a { color: inherit; }
a:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 2px; }
.pin:focus-visible { border-radius: 50%; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .92em; }

/* Header: which run this is */
.top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline;
  gap: 6px 24px; padding-bottom: 14px; border-bottom: 1px solid var(--rule); }
.mark { margin: 0; font-size: 18px; font-weight: 760; font-stretch: 125%; letter-spacing: -.01em; }
.run { margin: 0; display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 14px; color: var(--graphite);
  font-stretch: 90%; font-variant-numeric: tabular-nums; }
.note { margin: 16px 0 0; padding: 10px 14px; border-radius: var(--radius); background: var(--face);
  font-size: 14px; max-width: 80ch; }
.tag { display: inline-block; margin: 16px 0 0; padding: 4px 11px; border-radius: 999px;
  background: var(--face); border: 1px solid var(--rule); font-size: 13px; font-weight: 600;
  font-stretch: 105%; letter-spacing: .02em; text-transform: uppercase; color: var(--graphite); }
.stats { margin: 20px 0 0; padding: 0; list-style: none; display: flex; flex-wrap: wrap;
  gap: 10px 28px; font-variant-numeric: tabular-nums; }
.stats li { display: flex; flex-direction: column; gap: 2px; }
.stats b { font-size: 25px; font-weight: 680; font-stretch: 112%; letter-spacing: -.015em; }
.stats span { font-size: 13px; color: var(--graphite); font-stretch: 90%; }

/* The thesis: one sentence about who got woken up */
.hero { padding-top: 40px; }
.hero h1 { margin: 0; max-width: 30ch; font-size: clamp(27px, 4vw, 42px); line-height: 1.12;
  font-weight: 640; font-stretch: 112%; letter-spacing: -.015em; }
.follow { margin: 12px 0 0; max-width: 60ch; font-size: 18px; color: var(--graphite); }

/* The signature: P(page) as an instrument scale with the policy's detents */
.rail { margin: 36px 0 0; }
.instrument { margin: 0 calc(var(--dot) / 2 + 2px); }
.rail-plot { --rows: var(--rows-d); position: relative; height: calc(var(--rows) * var(--step) + 14px); }
.pin { --k: var(--kd); --x: var(--xd); }
.more.only-m, .hide-d { display: none; }
.pin { position: absolute; left: var(--x); bottom: calc(6px + var(--k) * var(--step));
  width: var(--dot); height: var(--dot); border-radius: 50%; transform: translateX(-50%); }
.pin::after { content: attr(data-tip); position: absolute; bottom: calc(100% + 8px); left: 50%;
  transform: translateX(-50%); width: max-content; max-width: 260px; padding: 6px 10px;
  border-radius: var(--radius); background: var(--ink); color: var(--ground); font-size: 13px;
  line-height: 1.35; font-stretch: 92%; white-space: normal; pointer-events: none;
  opacity: 0; visibility: hidden; z-index: 2; }
.pin:hover::after, .pin:focus-visible::after { opacity: 1; visibility: visible; }
.pin.tip-start::after { left: 0; transform: none; }
.pin.tip-end::after { left: auto; right: 0; transform: none; }
.more { position: absolute; bottom: calc(6px + var(--k) * var(--step)); transform: translateX(-50%);
  font-size: 12px; line-height: var(--dot); color: var(--graphite); font-variant-numeric: tabular-nums; }
.scale { position: relative; height: 36px; background: var(--face); border-radius: var(--radius); }
.zone { position: absolute; top: 0; bottom: 0; }
.zone-review { background: repeating-linear-gradient(135deg, var(--accent-wash) 0 6px, transparent 6px 12px); }
.zone-page { background: var(--accent-wash); border-radius: 0 var(--radius) var(--radius) 0; }
.ticks { position: absolute; left: 0; right: 0; top: 0; height: 7px;
  background: repeating-linear-gradient(to right, var(--graphite) 0 1px, transparent 1px 5%); opacity: .55; }
.detent { position: absolute; top: -8px; bottom: 0; width: 2px; margin-left: -1px; background: var(--ink); }
.scale-labels, .zone-labels { position: relative; height: 22px; }
.scale-labels span { position: absolute; top: 6px; transform: translateX(-50%); font-size: 13px;
  font-weight: 620; font-stretch: 118%; font-variant-numeric: tabular-nums; }
.scale-labels .at-start { transform: none; }
.scale-labels .at-end { transform: translateX(-100%); }
.zone-labels { height: 28px; }
.zone-labels > span { position: absolute; top: 8px; text-align: center; font-size: 13px; color: var(--graphite); }
.zone-labels > .z-review, .zone-labels > .z-page { color: var(--accent); font-weight: 600; }
.zone-labels .short { display: none; }
.rail figcaption { margin-top: 18px; max-width: 72ch; font-size: 14px; color: var(--graphite); }
.legend { list-style: none; margin: 14px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 8px 22px;
  font-size: 14px; color: var(--graphite); }
.legend li { display: flex; align-items: center; gap: 8px; }
.rail-empty { margin: 0; padding: 18px 0 20px; max-width: 64ch; }

/* Decision states: fill carries the state, the one accent carries urgency */
.m { display: inline-block; flex: none; border-radius: 50%; }
.legend .m, .glyph { width: 12px; height: 12px; }
.m-page { background: var(--accent); }
.m-linked { background: var(--accent-mid); }
.m-review { background: transparent; box-shadow: inset 0 0 0 2px var(--accent); }
.m-ticket { background: var(--ink); }
.m-quiet { background: transparent; box-shadow: inset 0 0 0 1.5px var(--graphite); }

.alarm { margin: 28px 0 0; padding: 14px 16px; border-radius: var(--radius); background: var(--accent);
  color: var(--accent-ink); }
.alarm p, .notice p { margin: 0; }
.alarm ul { margin: 8px 0 0; padding-left: 20px; }
.notice { margin: 28px 0 0; padding: 12px 16px; border-radius: var(--radius); background: var(--face); max-width: 80ch; }

section > h2 { margin: 0 0 16px; font-size: 22px; line-height: 1.25; font-weight: 680; font-stretch: 112%;
  letter-spacing: -.01em; }

/* Alerts, grouped by what a person experiences */
.alerts { margin-top: 56px; }
.colhead, .row { display: grid; column-gap: 18px; align-items: baseline;
  grid-template-columns: 14px minmax(0, 1fr) 176px 148px 140px 60px 76px; }
.colhead { padding-bottom: 8px; border-bottom: 1px solid var(--rule); font-size: 13px; color: var(--graphite);
  font-stretch: 92%; }
.colhead .r { text-align: right; }
.group { padding-top: 26px; }
.group h3 { margin: 0 0 2px; display: flex; gap: 10px; align-items: baseline; font-size: 16px; font-weight: 680; }
.group h3 .count { font-weight: 500; color: var(--graphite); font-variant-numeric: tabular-nums; }
.group h3 .count.sub { font-size: 13px; font-stretch: 90%; }
.rows { list-style: none; margin: 0; padding: 0; }
.row { padding: 12px 0; scroll-margin-top: 24px; border-radius: var(--radius);
  transition: background-color .6s ease, box-shadow .6s ease; }
.row:target { background: var(--accent-wash); box-shadow: 0 0 0 8px var(--accent-wash); }
.glyph { align-self: start; margin-top: 6px; }
.main { min-width: 0; }
.title { font-weight: 560; line-height: 1.35; overflow-wrap: anywhere; }
.meta { display: flex; flex-wrap: wrap; gap: 2px 14px; margin-top: 2px; font-size: 13px; color: var(--graphite);
  font-stretch: 88%; font-variant-numeric: tabular-nums; }
.flags { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 4px; font-size: 13px; font-weight: 600;
  color: var(--graphite); }
.flags .severe { color: var(--accent); }
.row.child .main { position: relative; padding-left: 20px; }
.row.child .main::before { content: ""; position: absolute; left: 3px; top: -10px; width: 11px; height: 20px;
  border-left: 1.5px solid var(--rule); border-bottom: 1.5px solid var(--rule); border-bottom-left-radius: var(--radius); }
.tag { display: inline-block; padding: 1px 8px; border-radius: var(--radius); font-size: 13px; font-weight: 600;
  line-height: 1.6; white-space: nowrap; }
.tag-page { background: var(--accent); color: var(--accent-ink); }
.tag-linked { background: var(--accent-wash); color: var(--ink); }
.tag-review { box-shadow: inset 0 0 0 1.5px var(--accent); color: var(--accent); }
.tag-ticket { background: var(--ink); color: var(--ground); }
.tag-quiet { box-shadow: inset 0 0 0 1px var(--rule); color: var(--graphite); }
.sub { display: block; margin-top: 3px; font-size: 13px; color: var(--graphite); }
.owner { font-weight: 500; overflow-wrap: anywhere; }
.dist { display: flex; height: 8px; border-radius: var(--radius); overflow: hidden; align-self: center; }
.dist span { flex-basis: 0; min-width: 0; }
.s4 { background: var(--rule); } .s3 { background: var(--graphite); }
.s2 { background: var(--accent-mid); } .s1 { background: var(--accent); }
.no-dist { align-self: center; font-size: 13px; color: var(--graphite); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.num .k { display: none; }
.why { grid-column: 2 / -1; margin-top: 6px; }
.why summary { width: max-content; cursor: pointer; font-size: 13px; color: var(--graphite); }
.why-body { margin-top: 8px; padding: 12px 14px; border-radius: var(--radius); background: var(--face); font-size: 14px; }
.reasons { margin: 0 0 10px; padding-left: 18px; }
.why-body dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 3px 16px; margin: 0; }
.why-body dt { color: var(--graphite); }
.why-body dd { margin: 0; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }

/* Run facts and evaluation */
.facts, .eval { margin-top: 60px; }
.facts dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 18px 36px; margin: 0; }
.facts dt { font-size: 13px; color: var(--graphite); }
.facts dd { margin: 2px 0 0; font-variant-numeric: tabular-nums; }
.caveat { margin: 0 0 22px; max-width: 70ch; color: var(--graphite); }
.eval-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 32px 56px; }
.eval h3 { margin: 0 0 8px; font-size: 16px; font-weight: 680; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: 6px 0; text-align: left; font-size: 15px; vertical-align: baseline; }
th { font-size: 13px; font-weight: 500; color: var(--graphite); border-bottom: 1px solid var(--rule); }
th.n, td.n { text-align: right; padding-left: 18px; white-space: nowrap; }
tr.zero td.n { color: var(--graphite); }
tr.bad td { color: var(--accent); font-weight: 600; }
tr.total td { border-top: 1px solid var(--rule); }
.stat { margin: 10px 0 0; font-size: 14px; color: var(--graphite); font-variant-numeric: tabular-nums; }
.eval-side > * + * { margin-top: 28px; }
.foot { margin-top: 64px; padding-top: 16px; border-top: 1px solid var(--rule); font-size: 14px; color: var(--graphite); }
.foot p { margin: 0; max-width: 80ch; }

@media (max-width: 900px) {
  .rail-plot { --rows: var(--rows-m); }
  .pin { --k: var(--km); --x: var(--xm); }
  .hide-d { display: block; } .hide-m, .more.only-d { display: none; } .more.only-m { display: block; }
  .colhead { display: none; }
  .row { grid-template-columns: 14px minmax(0, 1fr) auto auto; row-gap: 8px;
    grid-template-areas: "glyph main main main" ". decision owner owner" ". dist p a" ". why why why"; }
  .glyph { grid-area: glyph; } .main { grid-area: main; } .decision { grid-area: decision; }
  .owner { grid-area: owner; text-align: right; } .dist, .no-dist { grid-area: dist; }
  .num-page { grid-area: p; } .num-act { grid-area: a; } .why { grid-area: why; }
  .num .k { display: inline; margin-right: 4px; font-size: 12px; color: var(--graphite); }
  .eval-grid { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 560px) {
  :root { --dot: 11px; --step: 14px; }
  .zone-labels .long { display: none; } .zone-labels .short { display: inline; }
  .hero { padding-top: 28px; }
  .follow { font-size: 16px; }
}
@media (prefers-reduced-motion: no-preference) {
  .pin { animation: settle 520ms cubic-bezier(.16, 1, .3, 1) backwards;
    animation-delay: calc(var(--i) * var(--stagger, 28ms)); }
  @keyframes settle { from { opacity: 0; transform: translate(-50%, -16px); } }
}
@media (prefers-reduced-motion: reduce) { .row { transition: none; } }
"""


# --------------------------------------------------------------------------
# Small helpers

def esc(value):
    return html.escape(str(value), quote=True)


def count_word(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def display_title(alert):
    return TITLE_PREFIX.sub("", alert.get("title") or alert["id"])


def clock(value):
    t = triage.parse_time(value)
    return t.astimezone(timezone.utc).strftime("%H:%M UTC") if t else ""


def run_date(value):
    t = triage.parse_time(value)
    if not t:
        return "Date unknown"
    t = t.astimezone(timezone.utc)
    return f"{t:%b} {t.day}, {t:%Y}, {t:%H:%M} UTC"


def kind(decision):
    """The visual state of a decision: fill carries state, the accent carries urgency."""
    if decision.action in triage.PAGING:
        return "page"
    return {"DEDUP": "linked", "REVIEW": "review", "TICKET": "ticket"}.get(decision.action, "quiet")


def fmt(p):
    return f"{p:.2f}"


# --------------------------------------------------------------------------
# Sections

def thesis(decisions):
    counts = {}
    for d in decisions.values():
        counts[kind(d)] = counts.get(kind(d), 0) + 1
    paged, review = counts.get("page", 0), counts.get("review", 0)
    linked, quiet = counts.get("linked", 0), counts.get("ticket", 0) + counts.get("quiet", 0)

    def waiting(n):
        return f"{n} {'is' if n == 1 else 'are'} waiting for a human"

    if paged and review:
        lead = f"{paged} flagged for paging; {review} flagged for review."
    elif paged:
        lead = f"{paged} flagged for paging. Nothing was flagged for review."
    elif review:
        lead = f"Nothing was flagged for paging, but {count_word(review, 'alert')} went to review."
    else:
        lead = "Nothing was flagged for paging, and nothing went to review."
    rest = []
    if linked:
        rest.append(f"{linked} more {'was' if linked == 1 else 'were'} linked to an incident that already paged")
    if quiet:
        rest.append(f"{quiet} {'was' if quiet == 1 else 'were'} ticketed, logged or dropped")
    follow = ", and ".join(rest) + "." if rest else ""
    return lead, follow[:1].upper() + follow[1:]


def rail_bin(x, lo, hi, counts):
    """Which column an alert falls in, and the column's span.

    Bins never straddle a policy bar: the three regions are binned separately
    using the same comparisons triage applies, so an alert at 0.79 can never be
    drawn on the 0.80 side of the mark.
    """
    nlo, nmid, nhi = counts
    if x >= hi:
        j = min(nhi - 1, int((x - hi) / (1.0 - hi) * nhi)) if hi < 1.0 else 0
        w = (1.0 - hi) / nhi
        return nlo + nmid + j, hi + j * w, w
    if x > lo:
        j = min(nmid - 1, int((x - lo) / (hi - lo) * nmid))
        w = (hi - lo) / nmid
        return nlo + j, lo + j * w, w
    j = min(nlo - 1, int(x / lo * nlo)) if lo > 0 else 0
    w = lo / nlo
    return j, j * w, w


def rail(alerts, decisions, judgments, errors, policy):
    lo, hi = policy.no_page_bar, policy.page_bar
    judged = [a for a in alerts if a["id"] in judgments]
    skipped = len(alerts) - len(judged)

    if judged:
        # Alerts are stacked into columns, most urgent at the bottom. One "+n"
        # per column keeps the overflow labels from landing on top of each other
        # when hundreds of alerts share a probability.
        order = sorted(judged, key=lambda a: (min(1.0, max(0.0, judgments[a["id"]].p_page)),
                                              URGENCY[kind(decisions[a["id"]])], a["id"]))
        rows, rowvars, centers, more = {}, {}, {}, []
        for mode, gap in RAIL_GAPS.items():
            counts = tuple(max(1, round(span / gap)) for span in (lo, hi - lo, 1.0 - hi))
            columns, spans = {}, {}
            for a in order:
                x = min(1.0, max(0.0, judgments[a["id"]].p_page))
                b, start, w = rail_bin(x, lo, hi, counts)
                spans[b] = (start, w)
                k = len(columns.setdefault(b, []))
                columns[b].append(a["id"])
                rowvars.setdefault(a["id"], {})[mode] = k if k < MAX_STACK else None
                centers.setdefault(a["id"], {})[mode] = start + w / 2
            rows[mode] = min(MAX_STACK, max(len(c) for c in columns.values()))
            rows[mode] += 1 if any(len(c) > MAX_STACK for c in columns.values()) else 0
            # Neighbouring columns can both overflow. Merge their labels when
            # they would be drawn on top of each other.
            spill = sorted((spans[b][0] + spans[b][1] / 2, len(ids) - MAX_STACK)
                           for b, ids in columns.items() if len(ids) > MAX_STACK)
            merged = []
            for x, n in spill:
                if merged and x - merged[-1][0] < LABEL_GAP:
                    px, pn = merged[-1]
                    merged[-1] = ((px * pn + x * n) / (pn + n), pn + n)
                else:
                    merged.append((x, n))
            for x, n in merged:
                more.append(f'<span class="more only-{mode}" '
                            f'style="left:{x * 100:.2f}%;--k:{MAX_STACK}">+{n}</span>')
        pins = []
        for a in order:
            x = min(1.0, max(0.0, judgments[a["id"]].p_page))
            d, j, ks = decisions[a["id"]], judgments[a["id"]], rowvars[a["id"]]
            cs = centers[a["id"]]
            hide = "".join(f" hide-{m}" for m, k in ks.items() if k is None)
            edge = " tip-start" if x < 0.12 else (" tip-end" if x > 0.88 else "")
            label = f"{a['id']}, {ACTION_LABEL[d.action].lower()}, P(page) {fmt(j.p_page)}"
            pins.append(
                f'<a class="pin m m-{kind(d)}{edge}{hide}" href="#alert-{esc(a["id"])}" '
                f'style="--xd:{cs["d"] * 100:.2f}%;--xm:{cs["m"] * 100:.2f}%;'
                f'--kd:{ks["d"] or 0};--km:{ks["m"] or 0};--i:{len(pins)}" '
                f'aria-label="{esc(label)}" data-tip="{esc(a["id"] + ": " + display_title(a))}"></a>')
        pins += more
        # The dots fade in one after another. The step shrinks as the run grows
        # so the last dot never waits seconds to appear.
        step = min(28.0, STAGGER_TOTAL_MS / max(1, len(order)))
        plot = (f'<div class="rail-plot" style="--rows-d:{rows["d"]};--rows-m:{rows["m"]};'
                f'--stagger:{step:.2f}ms">{"".join(pins)}</div>')
    else:
        reason = next(iter(errors.values()), "no production alerts")
        plot = (f'<p class="rail-empty">Jev didn\'t judge any alert in this run, so there is nothing to '
                f'place on the scale. Every alert was routed by rule or configured severity '
                f'({esc(reason)}).</p>')

    caption = ("Each dot is an alert, placed at P(page): the probability Jev gives SEV1 or SEV2. "
               f"The marks at {fmt(lo)} and {fmt(hi)} are the policy. Select a dot to jump to its alert.")
    if judged and skipped:
        caption += (f" {count_word(skipped, 'alert')} skipped the model and "
                    f"{'is' if skipped == 1 else 'are'} listed below only.")
    legend = "".join(f'<li><span class="m m-{k}" aria-hidden="true"></span>{esc(text)}</li>'
                     for k, text in LEGEND)
    zone = lambda left, width: f"left:{left * 100:.2f}%;width:{width * 100:.2f}%"  # noqa: E731
    return f"""
<figure class="rail">
  <div class="instrument">
  {plot}
  <div class="scale" aria-hidden="true">
    <div class="zone zone-review" style="{zone(lo, hi - lo)}"></div>
    <div class="zone zone-page" style="{zone(hi, 1 - hi)}"></div>
    <div class="ticks"></div>
    <div class="detent" style="left:{lo * 100:.2f}%"></div>
    <div class="detent" style="left:{hi * 100:.2f}%"></div>
  </div>
  <div class="scale-labels" aria-hidden="true">
    <span class="at-start" style="left:0">0</span>
    <span style="left:{lo * 100:.2f}%">{fmt(lo)}</span>
    <span style="left:{hi * 100:.2f}%">{fmt(hi)}</span>
    <span class="at-end" style="left:100%">1</span>
  </div>
  <div class="zone-labels" aria-hidden="true">
    <span class="z-quiet" style="{zone(0, lo)}">No page</span>
    <span class="z-review" style="{zone(lo, hi - lo)}"><span class="long">A human decides within {policy.review_ack_min} min</span><span class="short">Human decides</span></span>
    <span class="z-page" style="{zone(hi, 1 - hi)}">Page</span>
  </div>
  </div>
  <figcaption>{esc(caption)}</figcaption>
  <ul class="legend">{legend}</ul>
</figure>"""


def notices(results, decisions, errors, report):
    out = []
    problems = results["summary"].get("invariant_violations") or []
    if problems:
        items = "".join(f"<li>{esc(p)}</li>" for p in problems)
        out.append(f'<div class="alarm" role="alert"><p><strong>Safety check failed.</strong> '
                   f'Fix these before trusting any decision below.</p><ul>{items}</ul></div>')
    fallback = [d for d in decisions.values() if d.source == "fallback"]
    if fallback:
        first = next((errors[d.id] for d in fallback if d.id in errors), "unknown error")
        out.append(f'<div class="notice"><p>{count_word(len(fallback), "alert")} fell back to '
                   f'configured severity because Jev was unavailable ({esc(first)}).</p></div>')
    if report.get("drift"):
        out.append(f'<div class="notice"><p>Re-routing the stored answers today changes '
                   f'{esc(", ".join(report["drift"]))}. The code or policy changed after this run; '
                   f'run triage.py again.</p></div>')
    return "".join(out)


def why_panel(alert, d, j, record, lab):
    reasons = "".join(f"<li>{esc(r)}</li>" for r in d.reasons if not r.startswith("P(page)="))
    facts = []
    if j:
        facts.append(("Severity", ", ".join(f"{lvl} {fmt(j.severity[lvl])}"
                                            for lvl in reversed(triage.SEV_LEVELS))))
        teams = sorted(j.team.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
        facts.append(("Owner", ", ".join(f"{t} {fmt(p)}" for t, p in teams if p > 0)))
        if j.duplicate_of:
            cause = triage.top(j.duplicate_of)
            facts.append(("Cause", f"{cause} at {fmt(j.duplicate_of[cause])}"))
            facts.append(("Candidates", ", ".join(record.get("candidates") or [])))
    if record.get("error"):
        facts.append(("Jev error", record["error"]))
    call = record.get("call")
    if call:
        tokens = call.get("usage", {}).get("input_tokens")
        facts.append(("Call", f"{tokens if tokens is not None else 'unknown'} input tokens, {call['ms']} ms"))
    facts.append(("Configured", alert.get("configured_severity") or "not set"))
    if alert.get("title") and display_title(alert) != alert["title"]:
        facts.append(("Full title", alert["title"]))
    if lab:
        labeled = [("actionable" if lab["actionable"] else "not actionable"), lab["severity"] or "no severity",
                   lab["team"] or "no owner"]
        if lab["duplicate_of"]:
            labeled.append(f"caused by {lab['duplicate_of']}")
        facts.append(("Labeled", ", ".join(labeled)))
    dl = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in facts)
    return (f'<details class="why"><summary>Why</summary><div class="why-body">'
            f'{f"<ul class=reasons>{reasons}</ul>" if reasons else ""}<dl>{dl}</dl></div></details>')


def alert_row(alert, d, j, record, tags, lab, child=False):
    aid = alert["id"]
    bits = [aid, alert.get("service"), clock(alert.get("started_at"))]
    if not triage.is_prod(alert):
        bits.append(alert.get("env"))
    meta = "".join(f"<span>{esc(b)}</span>" for b in bits if b)
    flags = ""
    if tags:
        flags = '<div class="flags">' + "".join(
            f'<span class="{"severe" if t in SEVERE else ""}">{esc(TAG_LABEL[t])}</span>' for t in tags) + "</div>"

    label = f"Linked to {d.linked_to}" if d.action == "DEDUP" else ACTION_LABEL[d.action]
    subs = []
    if d.linked_to and d.action != "DEDUP":
        subs.append(f"Linked to {d.linked_to}")
    if d.source == "rule":
        subs.append("Rule: not production")
    elif d.source == "fallback":
        subs.append("Fallback: Jev unavailable")
    decision = f'<span class="tag tag-{kind(d)}">{esc(label)}</span>' + "".join(
        f'<span class="sub">{esc(s)}</span>' for s in subs)
    owner = esc(d.team) + "".join(f'<span class="sub">Also notifies {esc(t)}</span>' for t in d.notify)

    if j:
        aria = ", ".join(f"{lvl} {fmt(j.severity[lvl])}" for lvl in reversed(triage.SEV_LEVELS))
        segments = "".join(f'<span class="s{lvl[-1]}" style="flex-grow:{j.severity[lvl]:.4f}"></span>'
                           for lvl in triage.SEV_LEVELS)
        dist = f'<div class="dist" role="img" aria-label="Severity: {esc(aria)}">{segments}</div>'
        p_page, p_act = fmt(j.p_page), fmt(j.p_actionable)
    else:
        dist = '<span class="no-dist">No model answer</span>'
        p_page = p_act = "-"
    return f"""
<li class="row{' child' if child else ''}" id="alert-{esc(aid)}">
  <span class="glyph m m-{kind(d)}" aria-hidden="true"></span>
  <div class="main"><div class="title">{esc(display_title(alert))}</div><div class="meta">{meta}</div>{flags}</div>
  <div class="decision">{decision}</div>
  <div class="owner">{owner}</div>
  {dist}
  <div class="num num-page"><span class="k">P(page)</span>{p_page}</div>
  <div class="num num-act"><span class="k">Actionable</span>{p_act}</div>
  {why_panel(alert, d, j, record, lab)}
</li>"""


def alert_groups(alerts, decisions, judgments, records, report):
    children = {}
    for a in alerts:
        root = decisions[a["id"]].linked_to
        if root:
            children.setdefault(root, []).append(a)

    def render(a, child=False):
        lab = evaluate.labels(a)
        return alert_row(a, decisions[a["id"]], judgments.get(a["id"]), records.get(a["id"], {}),
                         report["tags"].get(a["id"], []), lab, child)

    sections = []
    for slug, title, actions in GROUPS:
        roots = [a for a in alerts
                 if not decisions[a["id"]].linked_to and decisions[a["id"]].action in actions]
        if not roots:
            continue
        rows, linked = [], 0
        for a in roots:
            rows.append(render(a))
            for c in children.get(a["id"], []):
                rows.append(render(c, child=True))
                linked += 1
        tally = f'<span class="count">{len(roots)}</span>'
        if linked:
            tally += f'<span class="count sub">+{linked} linked</span>'
        sections.append(f'<section class="group" aria-labelledby="g-{slug}"><h3 id="g-{slug}">{esc(title)}'
                        f'{tally}</h3><ul class="rows">{"".join(rows)}</ul></section>')
    colhead = ('<div class="colhead" aria-hidden="true"><span></span><span>Alert</span><span>Decision</span>'
               '<span>Owner</span><span>Severity, low to high</span><span class="r">P(page)</span>'
               '<span class="r">Actionable</span></div>')
    return f'<section class="alerts" aria-labelledby="alerts-h"><h2 id="alerts-h">Alerts by outcome</h2>{colhead}{"".join(sections)}</section>'


def headline_stats(results):
    """The run's scale, cost, and speed, before any of the routing detail."""
    s = results["summary"]
    cells = [(f"{s['alerts']}", "alerts"), (f"{s['jev_answered']}", "Jev calls")]
    if s.get("cost_usd"):
        cells.append((f"${s['cost_usd']:.4f}", "total cost"))
    if s.get("latency_ms_p50") is not None:
        cells.append((f"{s['latency_ms_p50']}ms", "p50 per call"))
    if s.get("latency_ms_p95") is not None:
        cells.append((f"{s['latency_ms_p95']}ms", "p95 per call"))
    items = "".join(f"<li><b>{esc(v)}</b><span>{esc(k)}</span></li>" for v, k in cells)
    return f'<ul class="stats">{items}</ul>'


def run_facts(results):
    meta, s, p = results["meta"], results["summary"], results["meta"]["policy"]
    answered = ", ".join(meta.get("models_answered") or [])
    model = answered or "No answers"
    if meta.get("model_requested") and meta["model_requested"] != answered:
        model += f" (requested {meta['model_requested']})"
    calls = s["latency_ms_p50"] is not None
    problems = s.get("invariant_violations") or []
    rows = [
        ("Model", model),
        ("Alerts", f"{s['alerts']}: {s['jev_answered']} judged by Jev, {s['rule']} by rule, "
                   f"{s['fallback']} by fallback"),
        ("Cost", f"${s['cost_usd']:.5f} for {s['input_tokens']:,} input tokens" if calls else "No model calls"),
        ("Latency per call", f"{s['latency_ms_p50']} ms median, {s['latency_ms_p95']} ms p95" if calls
         else "No model calls"),
        ("Policy", f"Page at {fmt(p['page_bar'])}, review above {fmt(p['no_page_bar'])}, drop at "
                   f"{fmt(p['drop_bar'])} actionable, link at {fmt(p['dedup_bar'])}"),
        ("Safety checks", "All invariants held" if not problems
         else f"{count_word(len(problems), 'violation')}, listed at the top"),
    ]
    dl = "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in rows)
    return f'<section class="facts" aria-labelledby="facts-h"><h2 id="facts-h">About this run</h2><dl>{dl}</dl></section>'


def evaluation(report):
    n = report["n"]
    if not n:
        return ('<section class="eval" aria-labelledby="eval-h"><h2 id="eval-h">Against the labels</h2>'
                '<p class="caveat">No alert in this run has an expected label, so there is nothing to score. '
                'Add an "expected" block to alerts to compare decisions with what should have happened.</p></section>')
    caveat = ""
    if n < evaluate.MIN_CALIBRATION_N:
        caveat = (f'<p class="caveat">{n} labeled alerts is a smoke test. Trust these numbers, and the '
                  f'calibration especially, only after replaying a few hundred labeled alerts.</p>')

    ours, theirs = report["ours"], report["theirs"]
    rows = []
    for key, _ in evaluate.OUTCOMES:
        other = "n/a" if key in evaluate.NOT_APPLICABLE_TO_BASELINE else theirs[key]
        cls = "bad" if key in SEVERE and ours[key] else ("zero" if not ours[key] else "")
        rows.append(f'<tr class="{cls}"><td>{esc(OUTCOME_LABEL[key])}</td><td class="n">{ours[key]}</td>'
                    f'<td class="n">{other}</td></tr>')
    rows.append(f'<tr class="total"><td>Pages sent</td><td class="n">{ours["pages"]}</td>'
                f'<td class="n">{theirs["pages"]}</td></tr>')
    rows.append(f'<tr><td>Reviews sent</td><td class="n">{ours["reviews"]}</td>'
                f'<td class="n">{theirs["reviews"]}</td></tr>')
    outcomes = (f'<div><h3>Outcomes</h3><div class="table-wrap"><table><thead><tr><th>What happened</th>'
                f'<th class="n">With Jev</th><th class="n">Without Jev</th></tr></thead><tbody>'
                f'{"".join(rows)}</tbody></table></div></div>')

    side = []
    tallies = report.get("tallies", {})
    agree_rows = []
    for q, (k, m) in tallies.items():
        if m:
            lo, hi = evaluate.wilson(k, m)
            agree_rows.append(f'<tr><td>{AGREEMENT_LABEL[q]}</td><td class="n">{k} of {m}</td>'
                              f'<td class="n">{lo:.0%} to {hi:.0%}</td></tr>')
    if agree_rows:
        note = ""
        if report.get("never_offered"):
            note = (f'<p class="stat">{count_word(report["never_offered"], "labeled cause")} never offered '
                    f'as a candidate. Check the time window and topology.</p>')
        side.append(f'<div><h3>Agreement with labels</h3><div class="table-wrap"><table><thead><tr>'
                    f'<th>Question</th><th class="n">Matched</th><th class="n">95% interval</th></tr></thead>'
                    f'<tbody>{"".join(agree_rows)}</tbody></table></div>{note}</div>')
    calib = report.get("calibration", {}).get("P(page)")
    if calib:
        brier, ece, table = calib
        crow = "".join(f'<tr><td>{lo:.1f} to {hi:.1f}</td><td class="n">{k}</td><td class="n">{fmt(mp)}</td>'
                       f'<td class="n">{fmt(rate)}</td></tr>' for lo, hi, k, mp, rate in table)
        side.append(f'<div><h3>Calibration of P(page)</h3><div class="table-wrap"><table><thead><tr>'
                    f'<th>P(page)</th><th class="n">Alerts</th><th class="n">Predicted</th>'
                    f'<th class="n">Paged by label</th></tr></thead><tbody>{crow}</tbody></table></div>'
                    f'<p class="stat">Brier score {brier:.3f}, calibration error {ece:.3f}. '
                    f'Lower is better for both.</p></div>')
    if not side:
        side.append('<p class="caveat">Jev answered no labeled alert, so agreement and calibration '
                    'have nothing to measure.</p>')
    return (f'<section class="eval" aria-labelledby="eval-h"><h2 id="eval-h">Against the labels</h2>{caveat}'
            f'<div class="eval-grid">{outcomes}<div class="eval-side">{"".join(side)}</div></div></section>')


def render(results, alerts, results_name="results.json", label=None, footer=None,
           refresh_s=None):
    """The whole page as a string. `footer` replaces the how-to-refresh line;
    `refresh_s` makes the browser reload the page, for a live server."""
    meta = results["meta"]
    policy = triage.Policy(**meta["policy"])
    records = {r["id"]: r for r in results["alerts"]}
    known = {a["id"] for a in alerts}
    # Alerts missing from the alerts file still render, by id.
    alerts = list(alerts) + [{"id": i, "env": "prod"} for i in records if i not in known]
    alerts = [a for a in alerts if a["id"] in records]
    report = evaluate.compute_report(results, alerts)
    decisions = report["decisions"]
    judgments = {i: triage.Judgment(**r["judgment"]) for i, r in records.items() if r.get("judgment")}
    errors = {i: r["error"] for i, r in records.items() if r.get("error")}

    lead, follow = thesis(decisions)
    date = run_date(meta.get("generated_at"))
    note = f'<p class="note">{esc(meta["note"])}</p>' if meta.get("note") else ""
    follow_html = f'<p class="follow">{esc(follow)}</p>' if follow else ""
    tag = f'<p class="tag">{esc(label)}</p>' if label else ""
    stats_html = headline_stats(results)
    refresh = f'<meta http-equiv="refresh" content="{int(refresh_s)}">' if refresh_s else ""
    foot = esc(footer) if footer else (
        f"Built from {esc(results_name)} by generate_dashboard.py. To refresh, run\n"
        "    <code>python3 triage.py</code>, then <code>python3 generate_dashboard.py</code>.")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{refresh}
<title>Triage run, {esc(date)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&amp;display=swap">
<style>{CSS}</style>
</head>
<body>
<main class="page">
  <header class="top">
    <p class="mark">jev-oncall</p>
    <p class="run"><span>Triage run</span><span>{esc(date)}</span></p>
  </header>
  {note}
  {tag}
  <section class="hero" aria-labelledby="thesis">
    <h1 id="thesis">{esc(lead)}</h1>
    {follow_html}
    {stats_html}
    {rail(alerts, decisions, judgments, errors, policy)}
  </section>
  {notices(results, decisions, errors, report)}
  {alert_groups(alerts, decisions, judgments, records, report)}
  {run_facts(results)}
  {evaluation(report)}
  <footer class="foot"><p>{foot}</p></footer>
</main>
</body>
</html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render a triage run as one HTML page.")
    ap.add_argument("results", nargs="?", default=os.path.join(triage.BASE, "results.json"))
    ap.add_argument("--alerts", help="alerts file (default: the one triage.py read)")
    ap.add_argument("--out", default=os.path.join(triage.BASE, "dashboard.html"))
    ap.add_argument("--label", help="badge above the headline, e.g. 'Synthetic benchmark'")
    args = ap.parse_args(argv)

    if not os.path.exists(args.results):
        sys.exit(f"{args.results} not found. Run python3 triage.py first, then generate the dashboard.")
    results = triage.load_json(args.results)
    if not isinstance(results, dict) or results.get("meta", {}).get("version") != 2:
        sys.exit(f"{args.results} is from v1 and has no stored answers. Run python3 triage.py again.")
    path = args.alerts or results["meta"].get("alerts_path", "")
    if path and not os.path.exists(path):  # the repo moved since the run
        path = os.path.join(triage.BASE, os.path.basename(path))
    alerts = triage.load_alerts(path) if path and os.path.exists(path) else []
    if not alerts:
        print("warning: alerts file not found, so alerts are shown by id only", file=sys.stderr)

    page = render(results, alerts, os.path.basename(args.results), args.label)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
