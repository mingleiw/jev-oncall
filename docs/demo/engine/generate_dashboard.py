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
import json
import os
import re
import sys
from datetime import timezone

import evaluate
import shadow
import triage

ACTION_LABEL = {
    "PAGE_NOW": "Paged now", "PAGE": "Paged", "REVIEW": "Sent to review",
    "TICKET": "Ticketed", "LOG": "Logged", "DROP": "Dropped", "DEDUP": "Linked",
}
# Rows are grouped by what a person experiences, most urgent first. Linked
# alerts sit under the incident they joined, whatever their own action.
GROUPS = [
    ("page-now", "Paged now", ("PAGE_NOW",)),
    ("page", "Paged", ("PAGE",)),
    ("review", "Sent to review", ("REVIEW",)),
    ("ticket", "Ticketed", ("TICKET",)),
    ("quiet", "Logged or dropped", ("LOG", "DROP")),
]
URGENCY = {"page": 0, "linked": 1, "review": 2, "ticket": 3, "quiet": 4}
LEGEND = [("page", "Paged"), ("linked", "Linked to a paged incident"),
          ("review", "Sent to review"), ("ticket", "Ticketed"),
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
# Dots closer than the gap (in P(page) units) stack upward. Each dot is a
# 44x44px target, so a column must be at least 44px wide at the narrowest
# screen of its layout: desktop (>900px), tablet (561-900px), phone (320px+).
# Each dot gets a column and row for all three and CSS picks one.
RAIL_GAPS = {"d": 0.056, "t": 0.094, "m": 0.18}
MAX_STACK = 6  # dots above this in one column are counted in a "+n"
STAGGER_TOTAL_MS = 900  # the whole dot entry animation finishes within this
LABEL_GAP = 0.055  # "+n" labels closer than this share one merged label

CSS = """
:root {
  --ground: #F3F5F6; --face: #E7EBEE; --ink: #1C232B; --graphite: #5A6572; --rule: #CBD2D8;
  --accent: #C21F3A; --accent-mid: #D87F8F; --accent-wash: rgba(194, 31, 58, .10); --accent-ink: #FFFFFF;
  --review: #9A5B00; --review-wash: rgba(232, 163, 61, .20);
  --radius: 4px; --dot: 14px; --hit: 44px; --step: 44px;
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
    --review: #E8A33D; --review-wash: rgba(232, 163, 61, .16);
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --ground: #1B2129; --face: #232A33; --ink: #E5E9ED; --graphite: #9AA5B1; --rule: #37404B;
  --accent: #FF5A72; --accent-mid: #984051; --accent-wash: rgba(255, 90, 114, .12); --accent-ink: #1B2129;
  --review: #E8A33D; --review-wash: rgba(232, 163, 61, .16);
  color-scheme: dark;
}
html { scroll-padding-top: env(safe-area-inset-top, 0px); -webkit-text-size-adjust: 100%; }
*, *::before, *::after { box-sizing: inherit; }
body { margin: 0; background: var(--ground); color: var(--ink); font-family: var(--sans);
  font-size: 16px; line-height: 1.5; }
.page { max-width: 1180px; margin: 0 auto; padding: clamp(20px, 4vw, 44px) clamp(16px, 4vw, 40px) 56px; }
a { color: inherit; }
a:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 2px; }
.why summary { padding: 12px 0; }
.pin:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; border-radius: 50%; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .92em; }

/* Header: which run this is */
.top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline;
  gap: 6px 24px; padding-bottom: 14px; border-bottom: 1px solid var(--rule); }
.mark { margin: 0; font-size: 18px; font-weight: 760; font-stretch: 125%; letter-spacing: -.01em; }
.run { margin: 0; display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 14px; color: var(--graphite);
  font-stretch: 90%; font-variant-numeric: tabular-nums; }
.note { margin: 16px 0 0; padding: 10px 14px; border-radius: var(--radius); background: var(--face);
  font-size: 14px; max-width: 80ch; }
.badge { display: inline-block; margin: 16px 0 0; padding: 3px 10px; border-radius: var(--radius);
  background: var(--face); font-size: 13px; font-weight: 600; color: var(--graphite); }
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
.instrument { margin: 0 calc(var(--hit) / 2); }
.rail-plot { --rows: var(--rows-d); position: relative; height: calc(var(--rows) * var(--step)); }
.pin { --k: var(--kd); --x: var(--xd); --w: var(--wd); }
.more.only-t, .more.only-m, .hide-d { display: none; }
/* The link is the 44px target; the dot inside stays small. A target is never
   wider than its column, so neighbouring targets never overlap. */
.pin { position: absolute; left: var(--x); bottom: calc(var(--k) * var(--step));
  width: min(var(--hit), var(--w)); height: var(--hit); transform: translateX(-50%); }
.pin > .m { position: absolute; left: 50%; top: 50%; width: var(--dot); height: var(--dot);
  margin: calc(var(--dot) / -2) 0 0 calc(var(--dot) / -2); }
.pin:hover > .m { transform: scale(1.25); }
.pin::after { content: attr(data-tip); position: absolute; bottom: calc(100% - 6px); left: 50%;
  transform: translateX(-50%); width: max-content; max-width: 260px; padding: 6px 10px;
  border-radius: var(--radius); background: var(--ink); color: var(--ground); font-size: 13px;
  line-height: 1.35; font-stretch: 92%; white-space: normal; pointer-events: none;
  opacity: 0; visibility: hidden; z-index: 2; display: none; }
.pin:hover::after, .pin:focus-visible::after { opacity: 1; visibility: visible; display: block; }
.pin.tip-start::after { left: 0; transform: none; }
.pin.tip-end::after { left: auto; right: 0; transform: none; }
.more { position: absolute; bottom: calc(var(--k) * var(--step)); transform: translateX(-50%);
  font-size: 12px; line-height: var(--hit); color: var(--graphite); font-variant-numeric: tabular-nums; }
.scale { position: relative; height: 36px; background: var(--face); border-radius: var(--radius); }
.zone { position: absolute; top: 0; bottom: 0; }
.zone-review { background: repeating-linear-gradient(135deg, var(--review-wash) 0 6px, transparent 6px 12px); }
.zone-page { background: var(--accent-wash); border-radius: 0 var(--radius) var(--radius) 0; }
.ticks { position: absolute; left: 0; right: 0; top: 0; height: 7px;
  background: repeating-linear-gradient(to right, var(--graphite) 0 1px, transparent 1px 5%); opacity: .55; }
.detent { position: absolute; top: -8px; bottom: 0; width: 2px; margin-left: -1px; background: var(--ink); }
.scale-labels, .zone-labels { position: relative; height: 22px; }
.scale-labels span { position: absolute; top: 6px; transform: translateX(-50%); font-size: 13px;
  font-weight: 620; font-stretch: 118%; font-variant-numeric: tabular-nums; }
.scale-labels .at-start { transform: none; }
.scale-labels .at-end { transform: translateX(-100%); }
.zone-labels { height: 30px; }
.zone-labels > span { position: absolute; top: 8px; text-align: center; font-size: 13px; line-height: 1.25;
  color: var(--graphite); }
.zone-labels > .z-review, .zone-labels > .z-page { color: var(--accent); font-weight: 600; }
.zone-labels > .z-review { color: var(--review); }
.zone-labels .short { display: none; }
.rail figcaption { margin-top: 18px; max-width: 72ch; font-size: 14px; color: var(--graphite); }
.legend { list-style: none; margin: 14px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 8px 22px;
  font-size: 14px; color: var(--graphite); }
.legend li { display: flex; align-items: center; gap: 8px; }
.rail-empty { margin: 0; padding: 18px 0 20px; max-width: 64ch; }

/* Decision states: fill carries the state. Red means paged, and only that;
   amber means a human is asked to decide. */
.m { display: inline-block; flex: none; border-radius: 50%; }
.legend .m, .glyph { width: 12px; height: 12px; }
.m-page { background: var(--accent); }
.m-linked { background: var(--accent-mid); }
.m-review { background: transparent; box-shadow: inset 0 0 0 2px var(--review); }
.m-ticket { background: var(--ink); }
.m-quiet { background: transparent; box-shadow: inset 0 0 0 1.5px var(--graphite); }
.m { transition: transform .15s ease; }

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
.tag-review { box-shadow: inset 0 0 0 1.5px var(--review); color: var(--review); }
.tag-ticket { background: var(--ink); color: var(--ground); }
.tag-quiet { box-shadow: inset 0 0 0 1px var(--rule); color: var(--graphite); }
.sub { display: block; margin-top: 3px; font-size: 13px; color: var(--graphite); }
.owner { font-weight: 500; overflow-wrap: anywhere; }
.dist { display: flex; height: 8px; border-radius: var(--radius); overflow: hidden; align-self: center; }
.dist span { flex-basis: 0; min-width: 0; }
.s4 { background: var(--rule); } .s3 { background: var(--graphite); }
.s2 { background: var(--accent-mid); } .s1 { background: var(--accent); }
.no-dist { align-self: center; font-size: 13px; color: var(--graphite); }
.sub.override { color: var(--ink); font-weight: 600; }
.state { display: block; margin-top: 5px; font-size: 13px; font-weight: 600; line-height: 1.35; }
.state-pending { color: var(--review); } .state-escalated { color: var(--accent); }
.state-acked, .state-cancelled { color: var(--ink); }
.state-acked::before, .state-cancelled::before { content: "✓ "; }
.site-nav { display: flex; flex-wrap: wrap; gap: 0 4px; margin-right: auto; font-size: 14px; }
.site-nav a { display: inline-flex; align-items: center; min-height: 44px; padding: 0 10px; color: var(--graphite);
  text-decoration: none; border-radius: var(--radius); }
.site-nav a:hover { color: var(--ink); background: var(--face); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.num .k { display: none; }
.why { grid-column: 2 / -1; margin-top: 6px; }
.why summary { width: max-content; cursor: pointer; font-size: 13px; color: var(--graphite); }
.why summary:hover { color: var(--ink); }
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
  .rail-plot { --rows: var(--rows-t); }
  .pin { --k: var(--kt); --x: var(--xt); --w: var(--wt); }
  .hide-d { display: block; } .hide-t, .more.only-d { display: none; } .more.only-t { display: block; }
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
  :root { --dot: 12px; }
  .rail-plot { --rows: var(--rows-m); }
  .pin { --k: var(--km); --x: var(--xm); --w: var(--wm); }
  .hide-t { display: block; } .hide-m, .more.only-t { display: none; } .more.only-m { display: block; }
  .zone-labels .long { display: none; } .zone-labels .short { display: inline; }
  .hero { padding-top: 28px; }
  .follow { font-size: 16px; }
}
@media (prefers-reduced-motion: no-preference) {
  .pin { animation: settle 520ms cubic-bezier(.16, 1, .3, 1) backwards;
    animation-delay: calc(var(--i) * var(--stagger, 28ms)); }
  @keyframes settle { from { opacity: 0; transform: translate(-50%, -16px); } }
  .row.flash { animation: flash 1.6s ease; }
  @keyframes flash { from { background: var(--accent-wash); } }
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


def kind(decision, live=None):
    """The visual state of a decision: fill carries state, the accent carries urgency."""
    if decision.action in triage.PAGING:
        return "page"
    if decision.action == "REVIEW" and live:
        st = review_state(decision.id, live)
        if st and st["state"] == "escalated":
            return "page"
    return {"DEDUP": "linked", "REVIEW": "review", "TICKET": "ticket"}.get(decision.action, "quiet")


def fmt(p):
    return f"{p:.2f}"


# --------------------------------------------------------------------------
# Sections

def thesis(decisions, live=None):
    counts = {}
    for d in decisions.values():
        counts[kind(d)] = counts.get(kind(d), 0) + 1
    paged, review = counts.get("page", 0), counts.get("review", 0)
    linked, quiet = counts.get("linked", 0), counts.get("ticket", 0) + counts.get("quiet", 0)

    states = {}
    for d in decisions.values():
        st = review_state(d.id, live) if d.action == "REVIEW" else None
        if st:
            states[st["state"]] = states.get(st["state"], 0) + 1
    escalated = states.get("escalated", 0)
    current_paged = paged + escalated
    current_review = review - escalated

    if current_paged and current_review > 0:
        lead = f"{current_paged} paged; {current_review} in review."
    elif current_paged:
        lead = f"{current_paged} paged."
    elif current_review > 0:
        lead = f"Nothing paged, but {count_word(current_review, 'alert')} in review."
    else:
        lead = "Nothing paged, and nothing in review."
    rest = []
    if linked:
        rest.append(f"{linked} linked to an incident")
    if quiet:
        rest.append(f"{quiet} ticketed, logged or dropped")
    follow = ", and ".join(rest) + "." if rest else ""
    follow = follow[:1].upper() + follow[1:]
    if states:
        parts = [f"{states[s]} {words}" for s, words in (
            ("pending", "still waiting"), ("acked", "acked"),
            ("cancelled", "cleared before ack"), ("escalated", "escalated to page"))
            if states.get(s)]
        follow += (" " if follow else "") + f"Of {review} sent to review, {', '.join(parts)}."
    return lead, follow


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


def override(d, decisions):
    """Why an alert's final action differs from what its P(page) alone gave
    it, in a few words, or None. Thresholds come first; the dedup graph's
    ownership rules can then change the action."""
    if d.action == d.standalone or d.action == "DEDUP":
        return None  # a link says itself: "Linked to <root>"
    if d.action == "REVIEW" and d.linked_to:
        return "Review: different owning team"
    if triage.RANK.get(d.action, -1) > triage.RANK.get(d.standalone, -1):
        return f"Raised to {ACTION_LABEL[d.action].lower()}: a linked alert needs it"
    return None


def override_detail(d, decisions):
    """The longer form, for the Why panel."""
    if not override(d, decisions):
        return None
    first = f"On its own, its P(page) says {THRESHOLD_SAYS[d.standalone]}."
    if d.action == "REVIEW":
        root = decisions.get(d.linked_to)
        owner = f"{root.team} owns {d.linked_to}" if root else f"another team owns {d.linked_to}"
        return (f"{first} It joined incident {d.linked_to}, and {owner}, so "
                f"{d.team} gets a review instead of a page: a person on the right team decides, "
                "and nobody is paged twice for one incident.")
    return (f"{first} An alert linked to it needs "
            f"{ACTION_LABEL[d.action].lower()}, and the incident's root carries that for everyone.")


# What the threshold step alone says, before dedup and ownership rules.
THRESHOLD_SAYS = {"PAGE_NOW": "page now", "PAGE": "page", "REVIEW": "review", "TICKET": "ticket",
                  "LOG": "log", "DROP": "drop"}

REVIEW_STATE = {
    "pending": "waiting for an ack",
    "acked": "acked, won't page",
    "cancelled": "cleared before an ack, won't page",
    "escalated": "nobody acked, so it paged",
}


def review_state(aid, live):
    return ((live or {}).get("reviews") or {}).get(aid)


def review_story(st, live):
    """What happened to a review, in a sentence."""
    window = (live or {}).get("ack_min", 15)
    if st["state"] == "pending":
        return (f"Waiting for an ack since {clock(st['opened_at'])}. If nobody acks by "
                f"{clock(st['deadline_at'])}, it pages.")
    if st["state"] == "acked":
        return (f"Acked by {st['by'] or 'unknown'} at {clock(st['at'])}: someone is on it, so it won't "
                "escalate. The alert itself isn't resolved.")
    if st["state"] == "cancelled":
        return f"Cancelled at {clock(st['at'])}: the alert cleared before anyone acked, so no page."
    return f"Nobody acked within {window} min, so it paged at {clock(st['at'])}."


def state_badge(st, live):
    """A review's current state, under the decision it got on arrival."""
    if st["state"] == "pending":
        left = max(0, int(st["seconds_left"]))
        demo = " (demo clock)" if (live or {}).get("demo_clock") else ""
        return (f'<span class="state state-pending">Now: waiting, '
                f'<span class="left" data-left="{left}">{_minutes(left)} left</span>{demo}</span>')
    text = {"acked": f"Now: acked by {st['by'] or 'unknown'}, won't page",
            "cancelled": "Now: alert cleared, won't page",
            "escalated": f"Now: paged at {clock(st['at'])}, nobody acked"}[st["state"]]
    return f'<span class="state state-{st["state"]}">{esc(text)}</span>'


def rail(alerts, decisions, judgments, errors, policy, scripted=False, live=None):
    lo, hi = policy.no_page_bar, policy.page_bar
    judged = [a for a in alerts if a["id"] in judgments]
    skipped = len(alerts) - len(judged)

    if judged:
        # Alerts are stacked into columns, most urgent at the bottom. One "+n"
        # per column keeps the overflow labels from landing on top of each other
        # when hundreds of alerts share a probability.
        order = sorted(judged, key=lambda a: (min(1.0, max(0.0, judgments[a["id"]].p_page)),
                                              URGENCY[kind(decisions[a["id"]])], a["id"]))
        rows, rowvars, centers, widths, more = {}, {}, {}, {}, []
        for mode, gap in RAIL_GAPS.items():
            # Round down, so a column is never narrower than the gap (44px).
            counts = tuple(max(1, int(span / gap + 1e-9)) for span in (lo, hi - lo, 1.0 - hi))
            columns, spans = {}, {}
            for a in order:
                x = min(1.0, max(0.0, judgments[a["id"]].p_page))
                b, start, w = rail_bin(x, lo, hi, counts)
                spans[b] = (start, w)
                k = len(columns.setdefault(b, []))
                columns[b].append(a["id"])
                rowvars.setdefault(a["id"], {})[mode] = k if k < MAX_STACK else None
                centers.setdefault(a["id"], {})[mode] = start + w / 2
                widths.setdefault(a["id"], {})[mode] = w
            rows[mode] = min(MAX_STACK, max(len(c) for c in columns.values()))
            rows[mode] += 1 if any(len(c) > MAX_STACK for c in columns.values()) else 0
            # Neighbouring columns can both overflow. Merge their labels when
            # they would be drawn on top of each other.
            spill = sorted((spans[b][0] + spans[b][1] / 2, len(ids) - MAX_STACK)
                           for b, ids in columns.items() if len(ids) > MAX_STACK)
            merged = []
            for x, n in spill:
                if merged and x - merged[-1][0] < max(LABEL_GAP, gap):
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
            cs, ws = centers[a["id"]], widths[a["id"]]
            hide = "".join(f" hide-{m}" for m, k in ks.items() if k is None)
            edge = " tip-start" if x < 0.12 else (" tip-end" if x > 0.88 else "")
            final = f"linked to {d.linked_to}" if d.action == "DEDUP" else ACTION_LABEL[d.action].lower()
            why = override(d, decisions)
            state = review_state(a["id"], live) if d.action == "REVIEW" else None
            said = (f"P(page) {fmt(j.p_page)}, threshold says {THRESHOLD_SAYS[d.standalone]}; "
                    f"final: {final}" + (f" ({why.split(': ', 1)[-1]})" if why else "")
                    + (f"; now {REVIEW_STATE[state['state']]}" if state else ""))
            label = f"{a['id']}, {display_title(a)}. {said}. Jump to the alert."
            pins.append(
                f'<a class="pin{edge}{hide}" href="#alert-{esc(a["id"])}" '
                f'style="--xd:{cs["d"] * 100:.2f}%;--xt:{cs["t"] * 100:.2f}%;--xm:{cs["m"] * 100:.2f}%;'
                f'--wd:{ws["d"] * 100:.2f}%;--wt:{ws["t"] * 100:.2f}%;--wm:{ws["m"] * 100:.2f}%;'
                f'--kd:{ks["d"] or 0};--kt:{ks["t"] or 0};--km:{ks["m"] or 0};--i:{len(pins)}" '
                f'aria-label="{esc(label)}" data-tip="{esc(a["id"] + ": " + said)}">'
                f'<span class="m m-{kind(d)}" aria-hidden="true"></span></a>')
        pins += more
        # The dots fade in one after another. The step shrinks as the run grows
        # so the last dot never waits seconds to appear.
        step = min(28.0, STAGGER_TOTAL_MS / max(1, len(order)))
        plot = (f'<div class="rail-plot" role="group" aria-label="Alerts on the P(page) scale" '
                f'style="--rows-d:{rows["d"]};--rows-t:{rows["t"]};--rows-m:{rows["m"]};'
                f'--stagger:{step:.2f}ms">{"".join(pins)}</div>')
    else:
        reason = next(iter(errors.values()), "no production alerts")
        plot = (f'<p class="rail-empty">Jev didn\'t judge any alert in this run, so there is nothing to '
                f'place on the scale. Every alert was routed by rule or configured severity '
                f'({esc(reason)}).</p>')

    who = "the scripted answer" if scripted else "Jev"
    caption = (f"Each dot is an alert, placed at P(page): the probability {who} gives SEV1 or SEV2. "
               f"The zones are the first step, the policy's thresholds at {fmt(lo)} and {fmt(hi)}. "
               "Dedup and ownership rules run next, so a dot's fill is the final decision: a dot in "
               "the page zone may be linked to an incident that already pages, or sent to review "
               "because another team owns that incident. Select a dot to jump to its alert.")
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
    <span class="z-quiet" style="{zone(0, lo)}"><span class="long">Threshold: no page</span><span class="short">No page</span></span>
    <span class="z-review" style="{zone(lo, hi - lo)}"><span class="long">Threshold: review, a human decides within {policy.review_ack_min} min</span><span class="short">Review</span></span>
    <span class="z-page" style="{zone(hi, 1 - hi)}"><span class="long">Threshold: page</span><span class="short">Page</span></span>
  </div>
  </div>
  <figcaption>{esc(caption)}</figcaption>
  <ul class="legend" aria-label="Final decision">{legend}</ul>
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


def why_panel(alert, d, j, record, lab, live=None, decisions=None, known=None):
    reasons = "".join(f"<li>{esc(r)}</li>" for r in d.reasons if not r.startswith("P(page)="))
    facts = []
    detail = override_detail(d, decisions or {})
    if detail:
        facts.append(("Policy override", detail))
    st = review_state(alert["id"], live) if d.action == "REVIEW" else None
    if st:
        facts.append(("Review", review_story(st, live)))
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
    form = (label_form(alert["id"], live["teams"], lab, known or [])
            if live and live.get("shadow_on") else "")
    return (f'<details class="why"><summary>Why</summary><div class="why-body">'
            f'{f"<ul class=reasons>{reasons}</ul>" if reasons else ""}<dl>{dl}</dl>{form}</div></details>')


def alert_row(alert, d, j, record, tags, lab, child=False, live=None, decisions=None, known=None):
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
    why = override(d, decisions or {})
    if why:
        subs.append(why)
    if d.linked_to and d.action != "DEDUP":
        subs.append(f"Linked to {d.linked_to}")
    if d.source == "rule":
        subs.append("Rule: not production")
    elif d.source == "fallback":
        subs.append("Fallback: Jev unavailable")
    decision = f'<span class="tag tag-{kind(d)}">{esc(label)}</span>' + "".join(
        f'<span class="sub{" override" if s == why else ""}">{esc(s)}</span>' for s in subs)
    st = review_state(aid, live) if d.action == "REVIEW" else None
    if st:
        decision += state_badge(st, live)
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
  {why_panel(alert, d, j, record, lab, live, decisions, known)}
</li>"""


def alert_groups(alerts, decisions, judgments, records, report, live=None):
    known = [a["id"] for a in alerts]
    children = {}
    for a in alerts:
        root = decisions[a["id"]].linked_to
        if root:
            children.setdefault(root, []).append(a)

    def render(a, child=False):
        lab = evaluate.labels(a)
        return alert_row(a, decisions[a["id"]], judgments.get(a["id"]), records.get(a["id"], {}),
                         report["tags"].get(a["id"], []), lab, child, live, decisions, known)

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
        if slug == "review" and live:
            waiting = sum(1 for a in roots if (review_state(a["id"], live) or {}).get("state") == "pending")
            tally += f'<span class="count sub">{waiting} still waiting</span>'
        sections.append(f'<section class="group" aria-labelledby="g-{slug}"><h3 id="g-{slug}">{esc(title)}'
                        f'{tally}</h3><ul class="rows">{"".join(rows)}</ul></section>')
    colhead = ('<div class="colhead" aria-hidden="true"><span></span><span>Alert</span><span>Decision</span>'
               '<span>Owner</span><span>Severity, low to high</span><span class="r">P(page)</span>'
               '<span class="r">Actionable</span></div>')
    return f'<section class="alerts" aria-labelledby="alerts-h"><h2 id="alerts-h">Alerts by outcome</h2>{colhead}{"".join(sections)}</section>'


def scripted(results):
    """A demo whose answers were written by hand, not returned by Jev."""
    return results["meta"].get("answer_source") == "scripted"


def headline_stats(results):
    """The run's scale, cost, and speed, before any of the routing detail."""
    s = results["summary"]
    answered = "scripted answers, no model calls" if scripted(results) else "answered by Jev"
    cells = [(f"{s['alerts']}", "alerts"), (f"{s['jev_answered']}", answered)]
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
    by = "scripted answers" if scripted(results) else "Jev"
    if scripted(results):
        model = f"None: the answers are scripted (a real run asks {meta.get('model_requested')})"
    rows = [
        ("Model", model),
        ("Alerts", f"{s['alerts']}: {s['jev_answered']} judged by {by}, {s['rule']} by rule, "
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


BASELINE_NOTE = ("Your current routing means configured severity only: critical pages, warning "
                 "tickets, info logs, in every environment. jev-oncall also never pages for "
                 "non-production alerts, which is part of the difference.")


def evaluation(report, scripted_answers=False):
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
    total = len(report["decisions"])
    rows.append(f'<tr class="total"><td>Pages sent, all {total} alerts</td><td class="n">{ours["pages"]}</td>'
                f'<td class="n">{theirs["pages"]}</td></tr>')
    rows.append(f'<tr><td>Reviews sent, all {total} alerts</td><td class="n">{ours["reviews"]}</td>'
                f'<td class="n">{theirs["reviews"]}</td></tr>')
    outcomes = (f'<div><h3>Outcomes</h3><div class="table-wrap"><table><thead><tr><th>What happened</th>'
                f'<th class="n">jev-oncall</th><th class="n">Your current routing</th></tr></thead><tbody>'
                f'{"".join(rows)}</tbody></table></div>'
                f'<p class="stat">Rows above the line count the {n} labeled alerts; pages and reviews count '
                f'every alert, as decided on arrival. {BASELINE_NOTE}</p></div>')

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
    elif scripted_answers:
        side.insert(0, '<p class="caveat">These score the scripted answers against the labels. They show '
                       'how scoring works, not how accurate Jev is.</p>')
    return (f'<section class="eval" aria-labelledby="eval-h"><h2 id="eval-h">Against the labels</h2>{caveat}'
            f'<div class="eval-grid">{outcomes}<div class="eval-side">{"".join(side)}</div></div></section>')


# --------------------------------------------------------------------------
# Live server only: acting on reviews and labeling alerts
#
# Everything here calls the server's existing /ack and /label endpoints. None
# of it can change routing or config.

ACK_MEANING = ("Ack means someone is on it: the review closes and won't escalate to a page. "
               "It doesn't resolve the incident; the alert stays open until your monitoring clears it.")
NOT_DELIVERED = ("jev-oncall records each decision, including a page. Sending pages to PagerDuty, "
                 "Slack or Jira isn't built yet.")


def label_form(aid, teams, lab, known=()):
    """'What was this really?' inside an alert's Why panel (shadow mode).
    Saving again replaces the alert's label."""
    lab = lab or {}
    base = f"lf-{re.sub(r'[^A-Za-z0-9_-]', '_', aid)}"

    def options(values, chosen, blank):
        opts = [f'<option value="">{esc(blank)}</option>'] if blank else []
        for value, text in values:
            sel = " selected" if value == chosen else ""
            opts.append(f'<option value="{esc(value)}"{sel}>{esc(text)}</option>')
        return "".join(opts)

    actionable = {True: "true", False: "false"}.get(lab.get("actionable"), "")
    sev = options([(lvl, lvl) for lvl in reversed(triage.SEV_LEVELS)], lab.get("severity"), "Choose")
    act = options([("true", "Yes"), ("false", "No")], actionable, "Choose")
    team = options([(t, t) for t in teams], lab.get("team"), "Not sure")
    causes = [c for c in known if c != aid]
    if lab.get("duplicate_of") and lab["duplicate_of"] not in causes:
        causes.append(lab["duplicate_of"])
    dup = options([(c, c) for c in causes], lab.get("duplicate_of"), "Nothing")
    verb = "Update label" if lab else "Save label"
    return f"""
<form class="label-form" data-id="{esc(aid)}" novalidate aria-label="Label {esc(aid)}">
  <p class="lf-title">What was this really?</p>
  <div class="lf-grid">
    <label for="{base}-sev">Severity (required)<select id="{base}-sev" name="severity" required>{sev}</select></label>
    <label for="{base}-act">Needed a human? (required)<select id="{base}-act" name="actionable" required>{act}</select></label>
    <label for="{base}-team">Owner<select id="{base}-team" name="team">{team}</select></label>
    <label for="{base}-dup">Caused by<select id="{base}-dup" name="duplicate_of">{dup}</select></label>
  </div>
  <div class="lf-actions"><button type="submit" class="live-btn" id="{base}-save">{verb}</button><span class="live-status" role="status"></span></div>
</form>"""


def _minutes(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s"


def live_controls(live):
    token = ""
    if live.get("require_token"):
        token = ('<label for="you-token">Token<input id="you-token" type="password" '
                 'autocomplete="off" placeholder="JEV_WEBHOOK_SECRET"></label>')
    return f"""
<section class="live-you" aria-label="Who is acting">
  <label for="you-name">Your name<input id="you-name" autocomplete="name" placeholder="shown on acks and labels"></label>
  {token}
</section>"""


def _dom_id(prefix, aid):
    return f"{prefix}-{re.sub(r'[^A-Za-z0-9_-]', '_', aid)}"


def pending_section(live, alerts_by_id, demo=None):
    items = live.get("pending") or []

    def title_of(it):
        return display_title(alerts_by_id.get(it["id"], {"id": it["id"], "title": it.get("title") or it["id"]}))

    if not items:
        body = ('<p class="live-empty">No reviews are waiting. Unsure alerts land here, and page if nobody '
                'acks them in time.</p>')
    else:
        rows = []
        for it in items:
            aid, title = it["id"], title_of(it)
            resolve = ""
            if demo:
                resolve = (f'<button type="button" class="live-btn ghost resolve" id="{_dom_id("resolve", aid)}" '
                           f'data-id="{esc(aid)}" aria-label="Simulate: {esc(title)} clears before anyone acks">'
                           f'Alert clears</button>')
            ticking = "" if demo else f' data-left="{int(it["seconds_left"])}"'
            rows.append(f"""
<li class="pend">
  <div class="pend-main"><a href="#alert-{esc(aid)}">{esc(title)}</a><span class="pend-meta"><span>{esc(aid)}</span><span>{esc(it.get("team") or "no owner")}</span><span>pages at {esc(clock(it["deadline_at"]))} unless acked</span></span></div>
  <span class="pend-left"{ticking}>{_minutes(it["seconds_left"])} left</span>
  <div class="pend-btns"><button type="button" class="live-btn ack" id="{_dom_id("ack", aid)}" data-id="{esc(aid)}" aria-label="Ack the review for {esc(title)}">Ack</button>{resolve}</div>
  <span class="live-status" role="status"></span>
</li>""")
        body = f'<ul class="pend-list">{"".join(rows)}</ul>'
    closed = live.get("closed") or []
    history = ""
    if closed:
        lines = []
        for c in closed[:20]:
            title = title_of(c)
            lines.append(f'<li class="closed closed-{esc(c["state"])}"><a href="#alert-{esc(c["id"])}">{esc(title)}</a>'
                         f'<span class="pend-meta">{esc(review_story(c, live))}</span></li>')
        history = (f'<h3 class="closed-h">Closed reviews <span class="count">{len(closed)}</span></h3>'
                   f'<ul class="closed-list">{"".join(lines)}</ul>')
    durable = ""
    if not live.get("durable") and not demo:
        durable = ('<p class="live-warn">Reviews are held in memory on this server: a restart drops the ones '
                   'waiting, and they never page. Set <code>[reviews] store</code> to keep them.</p>')
    return f"""
<section class="live-box" id="reviews" aria-labelledby="pending-h">
  <h2 id="pending-h" tabindex="-1">Reviews waiting for an ack <span class="count">{len(items)}</span></h2>
  <p class="live-lede">{esc(ACK_MEANING)} If nobody acks before the deadline, the review escalates to a page.</p>
  {body}
  {history}
  {durable}
</section>"""


def shadow_section(summary, alerts_by_id):
    if summary is None:
        return ""
    if not summary["alerts"]:
        return """
<section class="live-box" aria-labelledby="shadow-h">
  <h2 id="shadow-h">Compared with your current routing</h2>
  <p class="live-empty">Shadow mode is on. Nothing has been logged yet.</p>
</section>"""
    pages = summary["pages"]
    since = run_date(summary["since"]) if summary.get("since") else "the start"
    counts = "".join(
        f'<li class="{"cmp-drop" if c["key"] == "dropped" else ""}"><span class="n">{c["count"]}</span>{esc(c["label"])}</li>'
        for c in summary["comparisons"] if c["count"])
    diffs = sorted(summary["recent_differences"], key=lambda d: d["comparison"] != COMPARISON_DROP)
    rows = []
    for d in diffs:
        title = display_title(alerts_by_id.get(d["id"], {"id": d["id"], "title": d["title"]}))
        link = f'<a href="#alert-{esc(d["id"])}">{esc(title)}</a>' if d["id"] in alerts_by_id else esc(title)
        reason = d["reasons"][0] if d["reasons"] else ""
        rows.append(f'<tr><td>{link}<span class="pend-meta">{esc(reason)}</span></td>'
                    f'<td class="act">{esc(d["your_routing"])} → {esc(d["jev_oncall"])}</td></tr>')
    table = ""
    if rows:
        table = (f'<div class="table-wrap"><table class="diffs"><thead><tr><th>Latest differences</th>'
                 f'<th>Your routing → jev-oncall</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    later = pages.get("escalated_reviews", 0)
    escalated = (f", plus {count_word(later, 'review')} nobody acked, which then paged" if later else "")
    labeled = summary["labeled"]
    return f"""
<section class="live-box" aria-labelledby="shadow-h">
  <h2 id="shadow-h">Compared with your current routing</h2>
  <p class="live-lede">Since {esc(since)}: {summary["alerts"]} alerts. Your current routing paged {pages["your_routing"]};
    jev-oncall paged {pages["jev_oncall"]} on arrival{esc(escalated)}. {count_word(labeled, "alert")} labeled so far.
    Open <b>Why</b> on any alert to label it.</p>
  <p class="live-lede">{esc(BASELINE_NOTE)} The evaluation below compares the same two.</p>
  <ul class="cmp-list">{counts}</ul>
  {table}
</section>"""


COMPARISON_DROP = shadow.COMPARISON_LABELS["dropped"]


def demo_banner(demo):
    return f"""
<section class="demo-banner" aria-label="About this demo">
  <p><b>Demo.</b> Scripted answers, real pipeline (routing, dedup, review queue, evaluation)
    running in your browser. {esc(NOT_DELIVERED)}</p>
</section>"""


def demo_controls(demo):
    minutes = demo.get("advance_min", 15)
    timeout_off = " disabled" if demo.get("timeout_used") else ""
    return f"""
<section class="live-box demo-controls" id="demo-controls" aria-labelledby="demo-h">
  <h2 id="demo-h">Try the safety paths</h2>
  <p class="live-lede">Demo clock: <b id="demo-clock">{esc(clock(demo["clock"]))}</b>. It only moves when you advance it.</p>
  <div class="demo-btns">
    <div><button type="button" class="live-btn" id="demo-advance" data-demo="advance">Advance {minutes} minutes</button>
      <span class="hint">Reviews nobody acked reach their deadline and page.</span></div>
    <div><button type="button" class="live-btn" id="demo-timeout" data-demo="timeout"{timeout_off}>Simulate a Jev timeout</button>
      <span class="hint">A new critical alert arrives while Jev times out: it's routed by configured severity.</span></div>
    <div><button type="button" class="live-btn ghost" id="demo-reset">Start over</button>
      <span class="hint">Restores the original labels, reviews and clock.</span></div>
  </div>
  <p class="hint">To resolve a review before its deadline, use <b>Alert clears</b> beside it above.</p>
  <p class="live-status" id="demo-status" role="status"></p>
</section>"""


LIVE_CSS = """
.live-you { display: flex; flex-wrap: wrap; gap: 12px 20px; margin-top: 24px; font-size: 14px; color: var(--graphite); }
.live-you label, .label-form label { display: grid; gap: 4px; font-size: 13px; color: var(--graphite); }
.live-you input, .label-form select, .label-form input {
  font: inherit; font-size: 15px; color: var(--ink); background: var(--ground); border: 1px solid var(--rule);
  border-radius: var(--radius); padding: 7px 9px; min-width: 0; min-height: 44px; }
.live-box { margin-top: 56px; padding-top: 22px; border-top: 2px solid var(--ink); }
.live-box h2 { margin: 0; font-size: 22px; font-weight: 680; font-stretch: 112%; letter-spacing: -.01em; }
.live-box .count { font-weight: 500; color: var(--graphite); }
.live-lede, .live-empty { margin: 8px 0 0; color: var(--graphite); font-size: 15px; max-width: 80ch; }
.live-warn { margin: 14px 0 0; padding: 8px 12px; border-left: 3px solid var(--review); font-size: 14px; }
.pend-list, .cmp-list, .closed-list { list-style: none; margin: 14px 0 0; padding: 0; display: grid; gap: 8px; }
.pend { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 6px 18px; align-items: center;
  padding: 14px 0; border-bottom: 1px solid var(--rule); }
.pend-list { gap: 0; border-top: 1px solid var(--rule); }
.pend-main { display: grid; gap: 2px; min-width: 0; }
.pend-main a, .diffs a, .closed a { color: var(--ink); font-weight: 600; }
.pend-meta { display: flex; flex-wrap: wrap; gap: 0 12px; font-size: 13px; color: var(--graphite); overflow-wrap: anywhere; }
.pend-left { font-variant-numeric: tabular-nums; color: var(--review); font-weight: 600; white-space: nowrap; }
.pend-btns { display: flex; gap: 8px; flex-wrap: wrap; }
.pend .live-status { grid-column: 1 / -1; }
.pend .live-status:empty { display: none; }
.closed-h { margin: 20px 0 0; font-size: 15px; font-weight: 680; }
#pending-h:focus { outline: none; }
#pending-h:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
.closed { padding: 8px 12px; border-left: 3px solid var(--rule); }
.closed-escalated { border-left-color: var(--accent); }
.live-btn { font: inherit; font-size: 14px; font-weight: 600; cursor: pointer; color: var(--accent-ink);
  background: var(--ink); border: 0; border-radius: var(--radius); padding: 8px 14px; min-height: 44px; min-width: 44px; }
.live-btn { transition: filter .15s ease, background-color .15s ease, transform .1s ease; }
.live-btn:hover:not(:disabled) { filter: brightness(1.25); }
.live-btn:active:not(:disabled) { transform: translateY(1px); }
.live-btn.ghost:hover:not(:disabled) { filter: none; background: var(--face); }
.live-btn:disabled { opacity: .5; cursor: default; }
.live-btn:focus-visible, .live-you input:focus-visible, .label-form select:focus-visible, .label-form input:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; }
.live-status { font-size: 13px; color: var(--graphite); }
.live-status.ok { color: var(--ink); } .live-status.bad { color: var(--accent); }
.cmp-list li { display: flex; gap: 10px; align-items: baseline; font-size: 15px; }
.cmp-list .n { min-width: 3ch; text-align: right; font-weight: 700; font-variant-numeric: tabular-nums; }
.cmp-list .cmp-drop { color: var(--accent); }
.diffs { margin-top: 16px; width: 100%; border-collapse: collapse; font-size: 14px; }
.diffs th, .diffs td { text-align: left; padding: 8px 10px; border-top: 1px solid var(--rule); vertical-align: top; }
.diffs th { font-size: 12px; color: var(--graphite); font-weight: 600; }
.diffs td.act { white-space: nowrap; font-weight: 600; }
.label-form { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--rule); }
.demo-banner { margin-top: 20px; padding: 14px 18px; display: grid; gap: 8px;
  border-left: 3px solid var(--ink); border-radius: 0 var(--radius) var(--radius) 0; background: var(--face); }
.demo-banner p { margin: 0; max-width: 90ch; font-size: 15px; }
.demo-btns { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px 20px; margin-top: 14px; }
.demo-btns > div { display: grid; gap: 6px; align-content: start; justify-items: start; }
.hint { font-size: 13px; color: var(--graphite); }
p.hint { margin: 12px 0 0; }
#demo-status { margin: 12px 0 0; font-size: 14px; }
#demo-status:empty { display: none; }
.live-btn.ghost { color: var(--ink); background: transparent; box-shadow: inset 0 0 0 1.5px var(--rule); }
.lf-title { margin: 0 0 10px; font-weight: 600; }
.lf-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.lf-actions { display: flex; gap: 12px; align-items: center; margin-top: 12px; }
.sr-only { position: absolute; width: 1px; height: 1px; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@media (max-width: 700px) {
  .lf-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .pend { grid-template-columns: minmax(0, 1fr) auto; }
  .pend-btns { grid-column: 1 / -1; }
  .demo-btns { grid-template-columns: minmax(0, 1fr); }
}
"""

LIVE_JS = r"""
(function () {
  // One set of handlers on the document, so they keep working after the page
  // body is swapped for a fresh render (live refresh, or the demo engine).
  var DEMO = window.JEV_DEMO || null, KEY = "jev-oncall-demo-v2";
  var store = {
    get: function (k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) {} }
  };
  store.del("jev-oncall-demo");  // the old demo's state; it can't be replayed
  var announcer = document.getElementById("live-announce");
  function field(id) { return document.getElementById(id); }
  function who() { var n = field("you-name"); return (n && n.value.trim()) || ""; }
  function remember() {
    [["you-name", "jev-oncall-name"], ["you-token", "jev-oncall-token"]].forEach(function (p) {
      var el = field(p[0]);
      if (el && !el.value) el.value = store.get(p[1]);
    });
  }
  document.addEventListener("change", function (e) {
    if (e.target.id === "you-name") store.set("jev-oncall-name", e.target.value.trim());
    if (e.target.id === "you-token") store.set("jev-oncall-token", e.target.value.trim());
  });
  function show(el, text, good) {
    if (el) { el.textContent = text; el.className = el.className.replace(/ ?(ok|bad)\b/g, "") + (good ? " ok" : " bad"); }
    if (announcer) { announcer.textContent = ""; announcer.textContent = text; }
  }

  // Replace the page body with a fresh render, keeping the reader's place:
  // scroll position, open Why panels, unsaved labels and keyboard focus.
  function swap(html, after) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    var fresh = doc.querySelector("main.page"), cur = document.querySelector("main.page");
    if (!fresh || !cur) return;
    var open = [], drafts = {}, focus = document.activeElement && document.activeElement.id;
    cur.querySelectorAll("details.why[open]").forEach(function (d) {
      var row = d.closest("[id]"); if (row) open.push(row.id);
    });
    cur.querySelectorAll("form.label-form[data-dirty]").forEach(function (f) {
      drafts[f.dataset.id] = [].map.call(f.elements, function (el) { return [el.name, el.value]; });
    });
    var y = window.scrollY;
    fresh = document.importNode(fresh, true);
    cur.replaceWith(fresh);
    open.forEach(function (id) { var d = document.querySelector("#" + CSS.escape(id) + " details.why"); if (d) d.open = true; });
    Object.keys(drafts).forEach(function (id) {
      var f = document.querySelector('form.label-form[data-id="' + CSS.escape(id) + '"]');
      if (!f) return;
      drafts[id].forEach(function (p) { if (p[0] && f.elements[p[0]]) f.elements[p[0]].value = p[1]; });
      f.dataset.dirty = "1";
    });
    document.title = doc.title;
    remember();
    window.scrollTo(0, y);
    var target = (after && field(after)) || (focus && field(focus));
    if (target) target.focus({preventScroll: true});
  }

  // ---- A live server: act through its endpoints, then re-render from it.
  function headers(extra) {
    var h = {"Content-Type": "application/json"}, t = field("you-token");
    if (t && t.value.trim()) h["Authorization"] = "Bearer " + t.value.trim();
    Object.keys(extra || {}).forEach(function (k) { if (extra[k]) h[k] = extra[k]; });
    return h;
  }
  function post(url, payload, extra) {
    return fetch(url, {method: "POST", headers: headers(extra), body: JSON.stringify(payload)})
      .then(function (r) { return r.json().catch(function () { return {}; })
        .then(function (b) { return {status: r.status, body: b}; }); });
  }
  function refresh(after) {
    return fetch(location.pathname, {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (html) { swap(html, after); });
  }
  function explain(status, body) {
    if (status === 401) return "This server needs the token. Enter it above.";
    return (body && body.error) || ("Failed (" + status + ")");
  }

  // ---- The demo: the same Python, in the browser, on a demo clock.
  var engine = null, session = null;
  function actions() {
    try { return JSON.parse(store.get(KEY) || "[]"); } catch (e) { return []; }
  }
  function demoStatus(text, good) { show(field("demo-status"), text, good); }
  function setBusy(busy) {
    document.querySelectorAll(".live-btn, .label-form select").forEach(function (b) {
      if (b.id === "demo-reset") return;
      if (busy) { b.dataset.wasDisabled = b.disabled ? "1" : ""; b.disabled = true; }
      else if (b.dataset.wasDisabled !== undefined) { b.disabled = b.dataset.wasDisabled === "1"; delete b.dataset.wasDisabled; }
    });
  }
  function loadEngine() {
    if (engine) return engine;
    var e = DEMO.engine;
    engine = new Promise(function (ok, fail) {
      if (window.loadPyodide) return ok();
      var s = document.createElement("script");
      s.src = e.pyodide + "pyodide.js";
      s.onload = ok;
      s.onerror = function () { fail(new Error("couldn't download Pyodide from " + e.pyodide)); };
      document.head.appendChild(s);
    }).then(function () { return window.loadPyodide({indexURL: e.pyodide}); })
      .then(function (py) {
        py.FS.mkdirTree("/demo/demo");
        return Promise.all(e.files.map(function (f) {
          return fetch(e.base + f).then(function (r) {
            if (!r.ok) throw new Error("couldn't load " + f);
            return r.text();
          }).then(function (text) { py.FS.writeFile("/demo/" + f, text); });
        })).then(function () {
          py.runPython("import sys, types\n" +
            "sys.modules.setdefault('ssl', types.ModuleType('ssl'))  # only real Jev calls need it\n" +
            "sys.path.insert(0, '/demo')\nimport json, build_demo");
          return py;
        });
      });
    engine.catch(function () { engine = null; });
    return engine;
  }
  function startSession(py, list) {
    py.globals.set("replay_json", JSON.stringify(list));
    py.runPython("session = build_demo.DemoSession(json.loads(replay_json))");
    var kept = JSON.parse(py.runPython("json.dumps(session.actions)"));
    store.set(KEY, JSON.stringify(kept));
    return py;
  }
  function run(action, statusSel, after) {
    setBusy(true);
    if (!session) demoStatus("Starting the demo engine. The first action downloads about 12 MB, once.", true);
    return loadEngine().then(function (py) {
      if (!session) session = startSession(py, actions());
      py.globals.set("action_json", JSON.stringify(action));
      var result = JSON.parse(py.runPython("json.dumps(session.apply(json.loads(action_json)))"));
      if (result.ok) {
        store.set(KEY, py.runPython("json.dumps(session.actions)"));
        swap(py.runPython("session.render()"), after);
      }
      setBusy(false);
      var el = statusSel && document.querySelector(statusSel);  // in the fresh render
      if (el) show(el, result.message, result.ok);
      show(field("demo-status"), result.message, result.ok);
    }).catch(function (err) {
      setBusy(false);
      demoStatus("The demo engine couldn't start (" + err.message + "). The page still shows the " +
                 "starting state; reload to try again.", false);
    });
  }

  // ---- Buttons and forms, for both.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button");
    if (!btn || btn.disabled) return;
    var status = btn.closest(".pend") && btn.closest(".pend").querySelector(".live-status");
    if (btn.classList.contains("ack")) {
      var id = btn.dataset.id;
      if (DEMO) return run({type: "ack", id: id, by: who()}, null, "pending-h");
      btn.disabled = true;
      post("/ack/" + encodeURIComponent(id), {}, {"X-Acked-By": who()}).then(function (r) {
        if (r.status === 200) {
          show(status, "Acked by " + r.body.acked_by + ". The review is closed and won't page; the alert isn't resolved.", true);
          return refresh("pending-h");
        }
        show(status, r.status === 409 || r.status === 404 ? explain(r.status, r.body) + ". Refreshing."
                                                         : explain(r.status, r.body), false);
        btn.disabled = false;
        if (r.status === 409 || r.status === 404) return refresh();
      }).catch(function () { show(status, "Couldn't reach the server.", false); btn.disabled = false; });
    } else if (btn.classList.contains("resolve") && DEMO) {
      run({type: "resolve", id: btn.dataset.id}, null, "pending-h");
    } else if (btn.dataset.demo && DEMO) {
      var after = btn.id;
      run({type: btn.dataset.demo}, null, after);
    } else if (btn.id === "demo-reset" && DEMO) {
      store.del(KEY);
      location.reload();
    }
  });
  document.addEventListener("input", function (e) {
    var form = e.target.closest && e.target.closest("form.label-form");
    if (form) form.dataset.dirty = "1";
  });
  document.addEventListener("submit", function (e) {
    var form = e.target.closest("form.label-form");
    if (!form) return;
    e.preventDefault();
    var status = form.querySelector(".live-status"), f = form.elements;
    if (!f.severity.value || !f.actionable.value) {
      show(status, "Choose a severity and whether it needed a human.", false);
      (f.severity.value ? f.actionable : f.severity).focus();
      return;
    }
    var label = {severity: f.severity.value, actionable: f.actionable.value === "true",
                 team: f.team.value || null, duplicate_of: f.duplicate_of.value || null};
    var id = form.dataset.id, save = form.querySelector("button");
    delete form.dataset.dirty;
    if (DEMO) return run({type: "label", id: id, label: label, by: who()},
                         'form.label-form[data-id="' + CSS.escape(id) + '"] .live-status', save && save.id);
    save.disabled = true;
    post("/label/" + encodeURIComponent(id), label, {"X-Labeled-By": who()}).then(function (r) {
      save.disabled = false;
      if (r.status === 201) { show(status, "Saved. Updating the evaluation.", true); return refresh(save.id); }
      form.dataset.dirty = "1";
      show(status, explain(r.status, r.body), false);
    }).catch(function () { save.disabled = false; form.dataset.dirty = "1"; show(status, "Couldn't reach the server.", false); });
  });

  remember();
  if (DEMO) {
    // Restore what this visitor already did: replay it on the engine.
    if (actions().length) {
      setBusy(true);
      demoStatus("Restoring your demo: replaying what you did on the demo engine.", true);
      loadEngine().then(function (py) {
        session = startSession(py, actions());
        swap(py.runPython("session.render()"));
        setBusy(false);
        demoStatus("Restored " + actions().length + " earlier action(s). Start over to reset.", true);
      }).catch(function (err) {
        setBusy(false);
        demoStatus("Couldn't restore your demo (" + err.message + "). Showing the starting state.", false);
      });
    }
    return;
  }

  // Live: review clocks count down between refreshes, and the page re-renders
  // from the server every 15 seconds unless someone is typing.
  setInterval(function () {
    var due = false;
    document.querySelectorAll("[data-left]").forEach(function (el) {
      var left = Math.max(0, parseInt(el.dataset.left, 10) - 1);
      el.dataset.left = left;
      if (!left && !el.dataset.done) { el.dataset.done = "1"; due = true; }
      el.textContent = left ? Math.floor(left / 60) + "m " + ("0" + left % 60).slice(-2) + "s left" : "deadline passed, escalating";
    });
    if (due) setTimeout(function () { refresh().catch(function () {}); }, 11000);  // after the next sweep
  }, 1000);
  setInterval(function () {
    var a = document.activeElement;
    var busy = document.querySelector("form.label-form[data-dirty]") ||
      (a && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName));
    if (!busy) refresh().catch(function () {});
  }, 15000);
})();
"""


def render(results, alerts, results_name="results.json", label=None, footer=None,
           refresh_s=None, live=None, demo=None, nav=None):
    """The whole page as a string. `footer` replaces the how-to-refresh line;
    `refresh_s` makes the browser reload the page, for a live server.
    `live` (server.py's live_view()) adds the review queue with Ack buttons,
    each review's current state, the shadow comparison and label forms. It
    re-renders itself from the server.
    `demo` (build_demo.py) adds a banner and demo controls, and runs the
    buttons on the demo engine in the browser: a dict with "clock",
    "timeout_used", "advance_min" and "engine". `nav` is [(text, href)]."""
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

    lead, follow = thesis(decisions, live)
    date = run_date(meta.get("generated_at"))
    note = f'<p class="note">{esc(meta["note"])}</p>' if meta.get("note") else ""
    follow_html = f'<p class="follow">{esc(follow)}</p>' if follow else ""
    tag = f'<p class="badge">{esc(label)}</p>' if label else ""
    stats_html = headline_stats(results)
    refresh = f'<meta http-equiv="refresh" content="{int(refresh_s)}">' if refresh_s and not live else ""
    alerts_by_id = {a["id"]: a for a in alerts}
    live_html = live_style = live_script = ""
    if live:
        live_html = (live_controls(live) + pending_section(live, alerts_by_id, demo)
                     + (demo_controls(demo) if demo else "")
                     + shadow_section(live.get("shadow"), alerts_by_id))
        live_style = LIVE_CSS
        live_script = ('<div id="live-announce" class="sr-only" role="status" aria-live="polite"></div>'
                       f"<script>{LIVE_JS}</script>")
        if demo:
            note = demo_banner(demo) + note
            config = json.dumps(demo["engine"]).replace("</", "<\\/")
            live_script = f"<script>window.JEV_DEMO = {{engine: {config}}};</script>" + live_script
    nav_html = ""
    if nav:
        links = "".join(f'<a href="{esc(href)}">{esc(text)}</a>' for text, href in nav)
        nav_html = f'<nav class="site-nav" aria-label="jev-oncall site">{links}</nav>'
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
<style>{CSS}{live_style}</style>
</head>
<body>
<main class="page">
  <header class="top">
    <p class="mark">jev-oncall</p>
    {nav_html}
    <p class="run"><span>Triage run</span><span>{esc(date)}</span></p>
  </header>
  {note}
  {tag}
  <section class="hero" aria-labelledby="thesis">
    <h1 id="thesis">{esc(lead)}</h1>
    {follow_html}
    {stats_html}
    {rail(alerts, decisions, judgments, errors, policy, scripted(results), live)}
  </section>
  {live_html}
  {notices(results, decisions, errors, report)}
  {alert_groups(alerts, decisions, judgments, records, report, live)}
  {run_facts(results)}
  {evaluation(report, scripted(results))}
  <footer class="foot"><p>{foot}</p></footer>
</main>
{live_script}
</body>
</html>
"""


# --------------------------------------------------------------------------
# App shell: three-column observability dashboard for the demo page
# --------------------------------------------------------------------------

APP_CSS = """
/* Warm observability palette — light by default, dark follows OS */
body.app {
  --ground: #FFF8F4; --face: #FFF0EA; --ink: #2C1810; --graphite: #7A5C4F; --rule: #E8D5CB;
  --accent: #D4421E; --accent-mid: #E88A70; --accent-wash: rgba(212, 66, 30, .10); --accent-ink: #FFFFFF;
  --review: #B8860B; --review-wash: rgba(184, 134, 11, .14);
  --c-page: #D4421E; --c-review: #E8A33D; --c-ticket: #6B8E23; --c-quiet: #7A5C4F; --c-linked: #9B8EC4;
  --panel-bg: #FFF0EA; --ticker-bg: #2C1810; --ticker-text: #FFD5C8;
  color-scheme: light;
  display: flex; flex-direction: column; height: 100vh; overflow: hidden; margin: 0;
}
@media (prefers-color-scheme: dark) {
  body.app:not([data-theme="light"]) {
    --ground: #1A1210; --face: #241A16; --ink: #F0E0D8; --graphite: #A08878; --rule: #3A2A22;
    --accent: #FF6B47; --accent-mid: #C85A3A; --accent-wash: rgba(255, 107, 71, .14); --accent-ink: #1A1210;
    --review: #E8A33D; --review-wash: rgba(232, 163, 61, .16);
    --c-page: #FF6B47; --c-review: #E8A33D; --c-ticket: #8FBC5A; --c-quiet: #A08878; --c-linked: #B8AAE0;
    --panel-bg: #241A16; --ticker-bg: #0E0A08; --ticker-text: #C89080;
    color-scheme: dark;
  }
}

/* Ticker bar */
.ticker { height: 28px; background: var(--ticker-bg); color: var(--ticker-text); overflow: hidden;
  font-size: 12px; font-weight: 500; display: flex; align-items: center; flex-shrink: 0;
  font-variant-numeric: tabular-nums; letter-spacing: .01em; }
.ticker-inner { display: flex; gap: 32px; white-space: nowrap; animation: tickerScroll 30s linear infinite;
  padding-left: 100%; }
.ticker-item { display: flex; align-items: center; gap: 6px; }
.ticker-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
@keyframes tickerScroll { 0% { transform: translateX(0); } 100% { transform: translateX(-100%); } }

/* Three-column layout */
.app-body { display: flex; flex: 1; min-height: 0; }
.col-left { width: 280px; flex-shrink: 0; overflow-y: auto; padding: 16px; border-right: 1px solid var(--rule);
  background: var(--ground); display: flex; flex-direction: column; gap: 16px; }
.col-center { flex: 1; min-width: 0; overflow-y: auto; padding: 0; background: var(--ground); }
.col-right { width: 360px; flex-shrink: 0; overflow-y: auto; border-left: 1px solid var(--rule);
  background: var(--panel-bg); display: flex; flex-direction: column; }

/* Left column panels */
.lp { background: var(--face); border-radius: 6px; padding: 14px; border: 1px solid var(--rule); }
.lp-title { font-size: 12px; font-weight: 600; color: var(--graphite); text-transform: uppercase;
  letter-spacing: .05em; margin: 0 0 10px; display: flex; align-items: center; gap: 6px; }
.lp-big { font-size: 32px; font-weight: 700; font-stretch: 112%; letter-spacing: -.02em; line-height: 1;
  font-variant-numeric: tabular-nums; }
.lp-sub { font-size: 12px; color: var(--graphite); margin-top: 2px; }
.lp-row { display: flex; align-items: center; gap: 8px; }
.lp-row .lp-big { flex-shrink: 0; }

/* Donut chart */
.donut-wrap { position: relative; width: 64px; height: 64px; flex-shrink: 0; }
.donut-wrap svg { width: 100%; height: 100%; }
.donut-center { position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; }
.donut-center .donut-num { font-size: 18px; font-weight: 700; line-height: 1; }
.donut-center .donut-label { font-size: 9px; color: var(--graphite); text-transform: uppercase;
  letter-spacing: .04em; font-weight: 600; }

/* Decision breakdown mini-legend */
.breakdown { display: grid; grid-template-columns: 1fr 1fr; gap: 3px 12px; margin-top: 8px; }
.breakdown-item { display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--graphite); }
.breakdown-dot { width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }
.breakdown-item strong { color: var(--ink); font-weight: 600; margin-right: 2px; }

/* Latency bars */
.lat-bars { display: grid; gap: 3px; margin-top: 6px; }
.lat-row { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.lat-label { width: 50px; text-align: right; color: var(--graphite); font-weight: 500; flex-shrink: 0; }
.lat-bar { flex: 1; height: 10px; border-radius: 3px; background: var(--rule); overflow: hidden; }
.lat-fill { height: 100%; border-radius: 3px; }
.lat-val { width: 50px; font-variant-numeric: tabular-nums; font-weight: 600; font-size: 11px; }

/* Demo controls in left column */
.lp-actions { display: grid; gap: 4px; }
.lp-btn { font: inherit; font-size: 12px; font-weight: 600; cursor: pointer; width: 100%;
  text-align: left; padding: 6px 10px; border: none; border-radius: var(--radius);
  background: var(--face); color: var(--graphite); min-height: 30px; border: 1px solid var(--rule);
  transition: background .12s, color .12s; }
.lp-btn:hover:not(:disabled) { background: var(--ground); color: var(--ink); border-color: var(--ink); }
.lp-btn:disabled { opacity: .4; cursor: default; }
.lp-btn.danger { color: var(--accent); border-color: var(--accent-wash); }
.lp-btn.danger:hover:not(:disabled) { background: var(--accent-wash); }
.lp-clock { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.lp-speed { font-size: 11px; color: var(--review); font-weight: 600; }

/* Center column: event feed */
.feed-header { padding: 16px 24px 12px; border-bottom: 1px solid var(--rule);
  position: sticky; top: 0; background: var(--ground); z-index: 3; }
.feed-header h1 { margin: 0; font-size: 16px; font-weight: 680; font-stretch: 112%; }
.feed-header .feed-sub { font-size: 13px; color: var(--graphite); margin-top: 2px; }
.feed-scroll { padding: 0 24px 40px; }

/* Event cards */
.ev-card { border: 1px solid var(--rule); border-radius: 6px; margin-top: 12px; background: var(--face);
  cursor: pointer; transition: border-color .12s, box-shadow .12s; overflow: hidden; }
.ev-card:hover { border-color: var(--accent-mid); box-shadow: 0 2px 8px rgba(0,0,0,.06); }
.ev-card.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.ev-card.auto-entering { animation: cardPulse .25s ease-out; }
@keyframes cardPulse { 0% { box-shadow: 0 0 0 3px var(--accent-wash); } 100% { box-shadow: 0 0 0 1px var(--accent); } }
.ev-top { display: flex; align-items: center; gap: 8px; padding: 10px 14px; }
.ev-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.ev-kind-badge { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: var(--radius);
  text-transform: uppercase; letter-spacing: .04em; }
.ev-num { font-size: 12px; color: var(--graphite); font-weight: 500; }
.ev-arrow { color: var(--graphite); font-size: 11px; margin-left: auto; }
.ev-decision { font-size: 13px; font-weight: 600; }
.ev-latency { font-size: 12px; color: var(--graphite); font-variant-numeric: tabular-nums; }
.ev-title { font-weight: 580; font-size: 14px; flex: 1; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }

/* Probability distribution bar */
.prob-bar { display: flex; height: 28px; border-radius: var(--radius); overflow: hidden; margin: 0 14px; }
.prob-seg { display: flex; align-items: center; justify-content: center; gap: 3px; padding: 0 6px;
  font-size: 11px; font-weight: 700; white-space: nowrap; min-width: 0; overflow: hidden;
  transition: flex-grow .3s; }
.prob-seg .prob-label { opacity: .85; font-weight: 600; }
.prob-seg .prob-val { font-variant-numeric: tabular-nums; }

/* Event bottom row */
.ev-bottom { padding: 8px 14px; font-size: 12px; color: var(--graphite); display: flex;
  align-items: center; gap: 8px; border-top: 1px solid var(--rule); }
.ev-bottom code { font-size: 11px; padding: 1px 5px; border-radius: 3px; background: var(--ground); }

/* Pending review cards in feed */
.ev-review-banner { padding: 6px 14px; background: var(--review-wash); border-top: 1px solid var(--rule);
  display: flex; align-items: center; gap: 8px; font-size: 13px; }
.ev-review-banner .attn-time { color: var(--review); font-weight: 600; font-variant-numeric: tabular-nums; }
.ev-ack-btn { font: inherit; font-size: 12px; font-weight: 700; padding: 4px 12px;
  border-radius: var(--radius); border: none; cursor: pointer;
  background: var(--ink); color: var(--ground); margin-left: auto; }

/* Right column: inspector panel */
.insp-header { padding: 14px 16px; border-bottom: 1px solid var(--rule); position: sticky;
  top: 0; background: var(--panel-bg); z-index: 2; }
.insp-header h2 { margin: 0; font-size: 14px; font-weight: 680; display: flex; align-items: center; gap: 8px; }
.insp-header .insp-sub { font-size: 12px; color: var(--graphite); margin-top: 2px; }
.auto-play-btn { background: none; border: 1px solid var(--rule); border-radius: 50%; width: 24px; height: 24px;
  font-size: 11px; cursor: pointer; color: var(--graphite); display: flex; align-items: center; justify-content: center;
  padding: 0; line-height: 1; }
.auto-play-btn:hover { background: var(--accent-wash); color: var(--accent); border-color: var(--accent); }
.insp-tabs { display: flex; gap: 0; border-bottom: 1px solid var(--rule); position: sticky;
  top: 50px; background: var(--panel-bg); z-index: 2; }
.insp-tab { font: inherit; font-size: 12px; font-weight: 600; padding: 8px 14px; border: none;
  border-bottom: 2px solid transparent; background: none; color: var(--graphite); cursor: pointer; }
.insp-tab:hover { color: var(--ink); }
.insp-tab.active { color: var(--ink); border-bottom-color: var(--accent); }
.insp-body { padding: 16px; flex: 1; overflow-y: auto; }
.insp-pane { display: none; }
.insp-pane.active { display: block; }

/* Inspector: decision detail */
.insp-decision { padding: 14px; background: var(--face); border-radius: 6px;
  border: 1px solid var(--rule); margin-bottom: 16px; }
.insp-decision .insp-dec-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.insp-decision .prob-ring { position: relative; width: 56px; height: 56px; flex-shrink: 0; }
.insp-decision .prob-ring svg { width: 100%; height: 100%; }
.insp-decision .prob-ring-val { position: absolute; inset: 0; display: flex;
  align-items: center; justify-content: center; font-size: 16px; font-weight: 700; }
.insp-options { display: grid; gap: 5px; }
.insp-opt { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.insp-opt-rank { width: 14px; height: 14px; border-radius: 50%; font-size: 10px; font-weight: 700;
  display: flex; align-items: center; justify-content: center; color: #fff; flex-shrink: 0; }
.insp-opt-bar { flex: 1; height: 6px; background: var(--rule); border-radius: 3px; overflow: hidden; }
.insp-opt-fill { height: 100%; border-radius: 3px; }
.insp-opt-val { font-weight: 600; font-variant-numeric: tabular-nums; width: 36px; text-align: right; }

/* Inspector: facts */
.insp-facts { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 4px 12px; }
.insp-facts dt { font-size: 12px; color: var(--graphite); }
.insp-facts dd { margin: 0; font-size: 13px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }

/* Inspector: decision log */
.dec-log { width: 100%; border-collapse: collapse; }
.dec-log th { font-size: 11px; font-weight: 600; color: var(--graphite); text-transform: uppercase;
  letter-spacing: .04em; padding: 5px 6px; border-bottom: 1px solid var(--rule); text-align: left; }
.dec-log td { font-size: 12px; padding: 5px 6px; border-bottom: 1px solid var(--rule);
  font-variant-numeric: tabular-nums; }
.dec-log tr { cursor: pointer; transition: background .1s; }
.dec-log tr:hover { background: var(--face); }
.dec-log tr.selected { background: var(--accent-wash); }
.dec-log .log-dots { display: flex; gap: 2px; }
.dec-log .log-dot { width: 8px; height: 8px; border-radius: 2px; }

/* Review banner in inspector */
.insp-review-banner { padding: 10px 14px; border-radius: var(--radius);
  border-left: 3px solid var(--review); background: var(--review-wash); font-size: 13px; margin-bottom: 12px; }
.insp-review-banner.escalated { border-left-color: var(--accent); background: var(--accent-wash); }

/* Severity dist in inspector */
.insp-dist { height: 12px; border-radius: 4px; overflow: hidden; display: flex; margin-top: 8px; }

/* P(page) gauge */
.p-gauge { position: relative; height: 18px; border-radius: 9px; background: var(--rule);
  overflow: hidden; margin-top: 6px; }
.p-gauge-fill { height: 100%; border-radius: 9px; transition: width .3s; }
.p-gauge-label { position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
  font-size: 11px; font-weight: 700; }
.p-gauge-marks { position: absolute; inset: 0; }
.p-gauge-mark { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--ground); opacity: .5; }

/* Site nav in header area */
.app-header { display: flex; align-items: center; gap: 12px; padding: 8px 20px;
  border-bottom: 1px solid var(--rule); background: var(--ground); flex-shrink: 0; }
.app-header .mark { margin: 0; }
.app-header .site-nav { display: flex; gap: 4px; margin-left: auto; }
.app-header .site-nav a { font-size: 13px; color: var(--graphite); text-decoration: none;
  padding: 4px 8px; border-radius: var(--radius); }
.app-header .site-nav a:hover { color: var(--ink); background: var(--face); }

/* Override .page styles inside app shell */
body.app .page { max-width: none; margin: 0; padding: 0; }
body.app .hero { padding-top: 0; }
body.app .foot { display: none; }
body.app .live-box { margin-top: 0; padding-top: 0; border-top: none; }
body.app .eval { margin-top: 0; }
body.app .facts { margin-top: 0; }

body.app .pend { grid-template-columns: 1fr; gap: 6px; }
body.app .pend-left { order: -1; }
body.app .pend-btns { margin-top: 4px; }
body.app .live-box h2 { font-size: 15px; }
body.app .live-lede { font-size: 13px; }
@media (max-width: 1100px) {
  .col-right { width: 300px; }
  .col-left { width: 240px; }
}
@media (max-width: 900px) {
  .app-body { flex-direction: column; }
  .col-left { display: none; }
  .col-right { display: none; }
  .col-right.mobile-open { display: flex; position: fixed; inset: 0; width: 100% !important;
    z-index: 100; background: var(--panel-bg); }
  .col-center { border: none; }
  .feed-scroll { padding: 0 16px 40px; }
}
"""

THRESHOLD_SAYS = {"PAGE_NOW": "page now", "PAGE": "page", "REVIEW": "review",
                  "TICKET": "ticket", "LOG": "log", "DROP": "drop"}

KIND_COLORS = {"page": "var(--c-page)", "review": "var(--c-review)",
               "ticket": "var(--c-ticket)", "quiet": "var(--c-quiet)", "linked": "var(--c-linked)"}
KIND_LABEL = {"page": "Page", "review": "Review", "ticket": "Ticket", "quiet": "Quiet", "linked": "Linked"}


def donut_svg(counts, total):
    if total == 0:
        return '<svg viewBox="0 0 36 36"><circle cx="18" cy="18" r="14" fill="none" stroke="var(--rule)" stroke-width="4"/></svg>'
    segments = []
    offset = 25
    order = ["page", "review", "ticket", "quiet", "linked"]
    for k in order:
        n = counts.get(k, 0)
        if n == 0:
            continue
        pct = n / total * 100
        segments.append(f'<circle cx="18" cy="18" r="14" fill="none" stroke="{KIND_COLORS.get(k, "var(--rule)")}" '
                        f'stroke-width="4" stroke-dasharray="{pct:.1f} {100 - pct:.1f}" '
                        f'stroke-dashoffset="{offset:.1f}"/>')
        offset -= pct
    return f'<svg viewBox="0 0 36 36">{"".join(segments)}</svg>'


def event_card(idx, a, d, j, record, live=None):
    aid = a["id"]
    k = kind(d, live)
    color = KIND_COLORS.get(k, "var(--graphite)")
    st = review_state(aid, live) if d.action == "REVIEW" else None
    if st and st["state"] == "escalated":
        label = "Paged (escalated)"
    elif d.action == "DEDUP":
        label = f"Linked to {d.linked_to}"
    else:
        label = ACTION_LABEL[d.action]
    call = record.get("call")
    latency = f"{call['ms']} ms" if call else ""

    prob_bar = ""
    if j:
        segs = [
            (j.p_page, "page", "var(--c-page)"),
            (max(0, j.p_actionable - j.p_page), "act", "var(--c-review)"),
            (max(0, 1 - j.p_actionable), "quiet", "var(--c-quiet)"),
        ]
        prob_bar = '<div class="prob-bar">' + "".join(
            f'<div class="prob-seg" style="flex-grow:{max(v, .01):.4f};background:{c}">'
            f'<span class="prob-label">{lbl}</span> <span class="prob-val">{v:.2f}</span></div>'
            for v, lbl, c in segs if v > 0.005) + '</div>'

    st = review_state(aid, live) if d.action == "REVIEW" else None
    review_html = ""
    if st:
        story = review_story(st, live)
        pending = (live or {}).get("pending") or []
        time_html = ""
        ack_html = ""
        for p in pending:
            if p["id"] == aid:
                time_html = f'<span class="attn-time">{_minutes(p["seconds_left"])}</span>'
                ack_html = f'<button type="button" class="ev-ack-btn ack" data-id="{esc(aid)}">Ack</button>'
                break
        cls = " escalated" if st["state"] == "escalated" else ""
        review_html = (f'<div class="ev-review-banner{cls}">'
                       f'{esc(story)} {time_html}{ack_html}</div>')

    bottom_parts = []
    if d.team:
        bottom_parts.append(f'<code>{esc(d.team)}</code>')
    if d.reasons:
        first = next((r for r in d.reasons if not r.startswith("P(page)=")), None)
        if first:
            bottom_parts.append(esc(first))
    bottom_html = ""
    if bottom_parts:
        bottom_html = f'<div class="ev-bottom">{" ".join(bottom_parts)}</div>'

    return (f'<div class="ev-card" data-id="{esc(aid)}" data-idx="{idx}">'
            f'<div class="ev-top">'
            f'<span class="ev-dot" style="background:{color}"></span>'
            f'<span class="ev-kind-badge" style="background:{color};color:#fff">{esc(KIND_LABEL.get(k, k))}</span>'
            f'<span class="ev-num">#{idx + 1}</span>'
            f'<span class="ev-title">{esc(display_title(a))}</span>'
            f'<span class="ev-arrow">&rarr;</span>'
            f'<span class="ev-decision">{esc(label)}</span>'
            f'<span class="ev-latency">{esc(latency)}</span>'
            f'</div>'
            f'{prob_bar}{review_html}{bottom_html}</div>')


def inspector_data(alerts, decisions, judgments, records, report, live=None):
    known = [a["id"] for a in alerts]
    details = []
    for idx, a in enumerate(alerts):
        aid = a["id"]
        d = decisions.get(aid)
        if not d:
            continue
        j = judgments.get(aid)
        record = records.get(aid, {})
        lab = evaluate.labels(a)
        k = kind(d, live)
        color = KIND_COLORS.get(k, "var(--graphite)")
        ist = review_state(aid, live) if d.action == "REVIEW" else None
        if ist and ist["state"] == "escalated":
            label = "Paged (escalated)"
        elif d.action == "DEDUP":
            label = f"Linked to {d.linked_to}"
        else:
            label = ACTION_LABEL[d.action]
        bits = [aid, a.get("service"), clock(a.get("started_at"))]
        if not triage.is_prod(a):
            bits.append(a.get("env"))
        meta_items = " &middot; ".join(esc(b) for b in bits if b)

        st = review_state(aid, live) if d.action == "REVIEW" else None
        banner = ""
        if st:
            story = review_story(st, live)
            cls = " escalated" if st["state"] == "escalated" else ""
            banner = f'<div class="insp-review-banner{cls}">{esc(story)}</div>'

        # Decision ring + options
        p_val = j.p_page if j else 0
        ring_pct = p_val * 100
        ring_color = color
        ring_html = (f'<div class="prob-ring"><svg viewBox="0 0 36 36">'
                     f'<circle cx="18" cy="18" r="14" fill="none" stroke="var(--rule)" stroke-width="3.5"/>'
                     f'<circle cx="18" cy="18" r="14" fill="none" stroke="{ring_color}" stroke-width="3.5" '
                     f'stroke-dasharray="{ring_pct:.1f} {100 - ring_pct:.1f}" stroke-dashoffset="25"/>'
                     f'</svg><span class="prob-ring-val">{p_val:.2f}</span></div>')

        options = []
        if j:
            ranked = [
                (j.p_page, "page", "var(--c-page)"),
                (j.p_actionable, "actionable", "var(--c-review)"),
            ]
            for lvl in reversed(triage.SEV_LEVELS):
                ranked.append((j.severity[lvl], lvl, "var(--graphite)"))
            teams = sorted(j.team.items(), key=lambda kv: -kv[1])[:3]
            for t, p in teams:
                ranked.append((p, t, "var(--c-ticket)"))
            ranked.sort(key=lambda x: -x[0])
            for i, (val, lbl, c) in enumerate(ranked[:6]):
                pct = val * 100
                options.append(
                    f'<div class="insp-opt">'
                    f'<span class="insp-opt-rank" style="background:{c}">{i + 1}</span>'
                    f'<span style="min-width:70px;font-size:12px">{esc(lbl)}</span>'
                    f'<span class="insp-opt-bar"><span class="insp-opt-fill" style="width:{pct:.1f}%;background:{c}"></span></span>'
                    f'<span class="insp-opt-val">{val:.2f}</span></div>')

        decision_html = (f'<div class="insp-decision"><div class="insp-dec-head">{ring_html}'
                         f'<div><div style="font-size:14px;font-weight:680">{esc(label)}</div>'
                         f'<div style="font-size:12px;color:var(--graphite)">{meta_items}</div></div></div>'
                         f'<div class="insp-options">{"".join(options)}</div></div>')

        facts = []
        if d.notify:
            facts.append(("Also notifies", ", ".join(d.notify)))
        detail = override_detail(d, decisions)
        if detail:
            facts.append(("Policy override", detail))
        if st:
            facts.append(("Review", review_story(st, live)))
        if j:
            facts.append(("Severity", ", ".join(f"{lvl} {fmt(j.severity[lvl])}"
                                                for lvl in reversed(triage.SEV_LEVELS))))
            teams_list = sorted(j.team.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
            facts.append(("Owner", ", ".join(f"{t} {fmt(p)}" for t, p in teams_list if p > 0)))
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
        facts.append(("Configured", a.get("configured_severity") or "not set"))
        if a.get("title") and display_title(a) != a["title"]:
            facts.append(("Full title", a["title"]))
        if lab:
            labeled = [("actionable" if lab["actionable"] else "not actionable"), lab["severity"] or "no severity",
                       lab["team"] or "no owner"]
            if lab["duplicate_of"]:
                labeled.append(f"caused by {lab['duplicate_of']}")
            facts.append(("Labeled", ", ".join(labeled)))

        dl = "".join(f"<dt>{esc(k2)}</dt><dd>{esc(v)}</dd>" for k2, v in facts)

        dist_html = ""
        if j:
            aria = ", ".join(f"{lvl} {fmt(j.severity[lvl])}" for lvl in reversed(triage.SEV_LEVELS))
            segs = "".join(f'<span class="s{lvl[-1]}" style="flex-grow:{j.severity[lvl]:.4f}"></span>'
                           for lvl in triage.SEV_LEVELS)
            dist_html = (f'<div style="margin-top:12px"><div style="font-size:12px;color:var(--graphite);'
                         f'font-weight:600;margin-bottom:4px">SEVERITY</div>'
                         f'<div class="insp-dist dist" role="img" aria-label="Severity: {esc(aria)}">{segs}</div></div>')

        reasons_html = ""
        if d.reasons:
            filtered = [r for r in d.reasons if not r.startswith("P(page)=")]
            if filtered:
                items = "".join(f"<li style=\"font-size:13px;margin-bottom:2px\">{esc(r)}</li>" for r in filtered)
                reasons_html = f'<div style="margin-top:12px"><div style="font-size:12px;color:var(--graphite);font-weight:600;margin-bottom:4px">ROUTING REASONS</div><ul class="reasons" style="margin:0;padding-left:18px">{items}</ul></div>'

        form = (label_form(aid, live["teams"], lab, known)
                if live and live.get("shadow_on") else "")
        form_html = f'<div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--rule)">{form}</div>' if form else ""

        details.append(
            f'<div class="insp-item" data-id="{esc(aid)}" data-idx="{idx}">'
            f'{banner}{decision_html}'
            f'<dl class="insp-facts" style="margin-top:12px">{dl}</dl>'
            f'{dist_html}{reasons_html}{form_html}</div>')
    return f'<div id="detail-data" style="display:none">{"".join(details)}</div>'


REVIEW_STATE_APP = {"pending": "waiting for an ack", "acked": "acked",
                    "cancelled": "alert cleared", "escalated": "paged, nobody acked"}


APP_LIVE_JS = r"""
(function () {
  var DEMO = window.JEV_DEMO || null, KEY = "jev-oncall-demo-v2";
  var store = {
    get: function (k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) {} }
  };
  store.del("jev-oncall-demo");
  var announcer = document.getElementById("live-announce");
  function field(id) { return document.getElementById(id); }
  function who() { var n = field("you-name"); return (n && n.value.trim()) || ""; }
  function remember() {
    [["you-name", "jev-oncall-name"], ["you-token", "jev-oncall-token"]].forEach(function (p) {
      var el = field(p[0]);
      if (el && !el.value) el.value = store.get(p[1]);
    });
  }
  document.addEventListener("change", function (e) {
    if (e.target.id === "you-name") store.set("jev-oncall-name", e.target.value.trim());
    if (e.target.id === "you-token") store.set("jev-oncall-token", e.target.value.trim());
  });
  function show(el, text, good) {
    if (el) { el.textContent = text; el.className = el.className.replace(/ ?(ok|bad)\b/g, "") + (good ? " ok" : " bad"); }
    if (announcer) { announcer.textContent = ""; announcer.textContent = text; }
  }

  // ---- Partial swap: replace columns, keep shell ----
  function swap(html, after) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    var ids = ["feed-header", "left-content", "feed-content", "detail-data", "log-content", "ticker-inner"];
    var drafts = {};
    document.querySelectorAll("form.label-form[data-dirty]").forEach(function (f) {
      drafts[f.dataset.id] = [].map.call(f.elements, function (el) { return [el.name, el.value]; });
    });
    var focus = document.activeElement && document.activeElement.id;
    ids.forEach(function (id) {
      var fresh = doc.getElementById(id), cur = document.getElementById(id);
      if (fresh && cur) {
        var node = document.importNode(fresh, true);
        cur.replaceWith(node);
      }
    });
    Object.keys(drafts).forEach(function (id) {
      var f = document.querySelector('form.label-form[data-id="' + CSS.escape(id) + '"]');
      if (!f) return;
      drafts[id].forEach(function (p) { if (p[0] && f.elements[p[0]]) f.elements[p[0]].value = p[1]; });
      f.dataset.dirty = "1";
    });
    var newBody = doc.querySelector("body");
    if (newBody) {
      var clk = newBody.dataset.demoClock;
      var ce = field("lp-clock-val");
      if (ce && clk) ce.textContent = clk;
    }
    document.title = doc.title;
    remember();
    if (selectedId) selectCard(selectedId);
    var target = (after && field(after)) || (focus && field(focus));
    if (target) target.focus({preventScroll: true});
  }

  // ---- Card selection → inspector ----
  var selectedId = null;
  function selectCard(id, autoRotating) {
    selectedId = id;
    document.querySelectorAll(".ev-card").forEach(function (c) { c.classList.toggle("selected", c.dataset.id === id); });
    document.querySelectorAll(".dec-log tr[data-id]").forEach(function (r) { r.classList.toggle("selected", r.dataset.id === id); });
    var data = document.querySelector('#detail-data .insp-item[data-id="' + CSS.escape(id) + '"]');
    var pane = field("insp-analysis");
    if (pane && data) { pane.innerHTML = data.innerHTML; }
    if (!autoRotating) switchTab("analysis");
    remember();
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest(".ack") || e.target.closest(".ev-ack-btn") ||
        e.target.closest("button[data-demo]") || e.target.closest("#demo-reset")) return;
    var card = e.target.closest(".ev-card");
    if (card) { selectCard(card.dataset.id); return; }
    var logRow = e.target.closest(".dec-log tr[data-id]");
    if (logRow) { selectCard(logRow.dataset.id); return; }
  });

  // ---- Inspector tabs ----
  function switchTab(name) {
    document.querySelectorAll(".insp-tab").forEach(function (t) { t.classList.toggle("active", t.dataset.tab === name); });
    document.querySelectorAll(".insp-pane").forEach(function (p) { p.classList.toggle("active", p.id === "insp-" + name); });
  }
  document.addEventListener("click", function (e) {
    var tab = e.target.closest(".insp-tab");
    if (tab) switchTab(tab.dataset.tab);
  });

  // ---- Auto-rotate cards to show triage speed ----
  var autoTimer = null, autoRunning = false, autoIdx = -1;
  var SCALE = 8;
  function cardEls() { return [].slice.call(document.querySelectorAll(".ev-card[data-id]")); }
  function cardMs(card) { var m = card.querySelector(".ev-latency"); return m ? parseInt(m.textContent, 10) || 40 : 40; }
  function updatePlayBtn() {
    var btn = field("auto-play");
    if (btn) { btn.textContent = autoRunning ? "⏸" : "▶"; btn.title = autoRunning ? "Pause" : "Play"; }
    var sub = document.querySelector(".insp-sub");
    if (sub) sub.textContent = autoRunning ? "Auto-cycling" : "Paused";
  }
  function autoNext() {
    if (!autoRunning) return;
    var cards = cardEls();
    if (!cards.length) return;
    autoIdx = (autoIdx + 1) % cards.length;
    var card = cards[autoIdx];
    selectCard(card.dataset.id, true);
    card.classList.remove("auto-entering");
    void card.offsetWidth;
    card.classList.add("auto-entering");
    card.scrollIntoView({behavior: "smooth", block: "nearest"});
    var delay = cardMs(card) * SCALE;
    autoTimer = setTimeout(autoNext, delay);
  }
  function startAutoRotate() {
    if (autoRunning) return;
    autoRunning = true;
    updatePlayBtn();
    autoTimer = setTimeout(autoNext, 300);
  }
  function stopAutoRotate() {
    autoRunning = false;
    clearTimeout(autoTimer);
    autoTimer = null;
    updatePlayBtn();
  }
  function toggleAutoRotate() { autoRunning ? stopAutoRotate() : startAutoRotate(); }
  document.addEventListener("click", function (e) {
    if (e.target.closest("#auto-play")) { toggleAutoRotate(); return; }
    if (e.target.closest(".ev-card") || e.target.closest(".dec-log tr[data-id]") ||
        e.target.closest(".insp-tab") || e.target.closest(".label-form")) stopAutoRotate();
  });
  startAutoRotate();

  // ---- Live server endpoints ----
  function headers(extra) {
    var h = {"Content-Type": "application/json"}, t = field("you-token");
    if (t && t.value.trim()) h["Authorization"] = "Bearer " + t.value.trim();
    Object.keys(extra || {}).forEach(function (k) { if (extra[k]) h[k] = extra[k]; });
    return h;
  }
  function post(url, payload, extra) {
    return fetch(url, {method: "POST", headers: headers(extra), body: JSON.stringify(payload)})
      .then(function (r) { return r.json().catch(function () { return {}; })
        .then(function (b) { return {status: r.status, body: b}; }); });
  }
  function refresh(after) {
    return fetch(location.pathname, {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (html) { swap(html, after); });
  }
  function explain(status, body) {
    if (status === 401) return "This server needs the token. Enter it above.";
    return (body && body.error) || ("Failed (" + status + ")");
  }

  // ---- Demo engine ----
  var engine = null, session = null;
  function actions() {
    try { return JSON.parse(store.get(KEY) || "[]"); } catch (e) { return []; }
  }
  function demoStatus(text, good) { show(field("demo-status"), text, good); }
  function setBusy(busy) {
    document.querySelectorAll(".live-btn, .lp-btn, .ev-ack-btn, .label-form select").forEach(function (b) {
      if (b.id === "demo-reset") return;
      if (busy) { b.dataset.wasDisabled = b.disabled ? "1" : ""; b.disabled = true; }
      else if (b.dataset.wasDisabled !== undefined) { b.disabled = b.dataset.wasDisabled === "1"; delete b.dataset.wasDisabled; }
    });
  }
  function loadEngine() {
    if (engine) return engine;
    var e = DEMO.engine;
    engine = new Promise(function (ok, fail) {
      if (window.loadPyodide) return ok();
      var s = document.createElement("script");
      s.src = e.pyodide + "pyodide.js";
      s.onload = ok;
      s.onerror = function () { fail(new Error("couldn't download Pyodide from " + e.pyodide)); };
      document.head.appendChild(s);
    }).then(function () { return window.loadPyodide({indexURL: e.pyodide}); })
      .then(function (py) {
        py.FS.mkdirTree("/demo/demo");
        return Promise.all(e.files.map(function (f) {
          return fetch(e.base + f).then(function (r) {
            if (!r.ok) throw new Error("couldn't load " + f);
            return r.text();
          }).then(function (text) { py.FS.writeFile("/demo/" + f, text); });
        })).then(function () {
          py.runPython("import sys, types\n" +
            "sys.modules.setdefault('ssl', types.ModuleType('ssl'))  # only real Jev calls need it\n" +
            "sys.path.insert(0, '/demo')\nimport json, build_demo");
          return py;
        });
      });
    engine.catch(function () { engine = null; });
    return engine;
  }
  function startSession(py, list) {
    py.globals.set("replay_json", JSON.stringify(list));
    py.runPython("session = build_demo.DemoSession(json.loads(replay_json))");
    var kept = JSON.parse(py.runPython("json.dumps(session.actions)"));
    store.set(KEY, JSON.stringify(kept));
    return py;
  }
  function run(action, statusSel, after) {
    setBusy(true);
    if (!session) demoStatus("Starting the demo engine. The first action downloads about 12 MB, once.", true);
    return loadEngine().then(function (py) {
      if (!session) session = startSession(py, actions());
      py.globals.set("action_json", JSON.stringify(action));
      var result = JSON.parse(py.runPython("json.dumps(session.apply(json.loads(action_json)))"));
      if (result.ok) {
        store.set(KEY, py.runPython("json.dumps(session.actions)"));
        swap(py.runPython("session.render()"), after);
      }
      setBusy(false);
      var el = statusSel && document.querySelector(statusSel);
      if (el) show(el, result.message, result.ok);
      show(field("demo-status"), result.message, result.ok);
    }).catch(function (err) {
      setBusy(false);
      demoStatus("The demo engine couldn't start (" + err.message + "). The page still shows the " +
                 "starting state; reload to try again.", false);
    });
  }

  // ---- Buttons and forms ----
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button");
    if (!btn || btn.disabled) return;
    var status = btn.closest(".pend") && btn.closest(".pend").querySelector(".live-status");
    if (btn.classList.contains("ack")) {
      var id = btn.dataset.id;
      if (DEMO) return run({type: "ack", id: id, by: who()}, null, "pending-h");
      btn.disabled = true;
      post("/ack/" + encodeURIComponent(id), {}, {"X-Acked-By": who()}).then(function (r) {
        if (r.status === 200) {
          show(status, "Acked by " + r.body.acked_by + ".", true);
          return refresh("pending-h");
        }
        show(status, r.status === 409 || r.status === 404 ? explain(r.status, r.body) + ". Refreshing."
                                                           : explain(r.status, r.body), false);
        btn.disabled = false;
        if (r.status === 409 || r.status === 404) return refresh();
      }).catch(function () { show(status, "Couldn't reach the server.", false); btn.disabled = false; });
    } else if (btn.classList.contains("resolve") && DEMO) {
      run({type: "resolve", id: btn.dataset.id}, null, "pending-h");
    } else if (btn.dataset.demo && DEMO) {
      run({type: btn.dataset.demo}, null, btn.id);
    } else if (btn.id === "demo-reset" && DEMO) {
      store.del(KEY);
      location.reload();
    }
  });
  document.addEventListener("input", function (e) {
    var form = e.target.closest && e.target.closest("form.label-form");
    if (form) form.dataset.dirty = "1";
  });
  document.addEventListener("submit", function (e) {
    var form = e.target.closest("form.label-form");
    if (!form) return;
    e.preventDefault();
    var status = form.querySelector(".live-status"), f = form.elements;
    if (!f.severity.value || !f.actionable.value) {
      show(status, "Choose a severity and whether it needed a human.", false);
      (f.severity.value ? f.actionable : f.severity).focus();
      return;
    }
    var label = {severity: f.severity.value, actionable: f.actionable.value === "true",
                 team: f.team.value || null, duplicate_of: f.duplicate_of.value || null};
    var id = form.dataset.id, save = form.querySelector("button");
    delete form.dataset.dirty;
    if (DEMO) return run({type: "label", id: id, label: label, by: who()},
                         'form.label-form[data-id="' + CSS.escape(id) + '"] .live-status', save && save.id);
    save.disabled = true;
    post("/label/" + encodeURIComponent(id), label, {"X-Labeled-By": who()}).then(function (r) {
      save.disabled = false;
      if (r.status === 201) { show(status, "Saved. Updating the evaluation.", true); return refresh(save.id); }
      form.dataset.dirty = "1";
      show(status, explain(r.status, r.body), false);
    }).catch(function () { save.disabled = false; form.dataset.dirty = "1"; show(status, "Couldn't reach the server.", false); });
  });

  remember();
  if (DEMO) {
    if (actions().length) {
      setBusy(true);
      demoStatus("Restoring your demo: replaying what you did on the demo engine.", true);
      loadEngine().then(function (py) {
        session = startSession(py, actions());
        swap(py.runPython("session.render()"));
        setBusy(false);
        demoStatus("Restored " + actions().length + " earlier action(s). Start over to reset.", true);
      }).catch(function (err) {
        setBusy(false);
        demoStatus("Couldn't restore your demo (" + err.message + "). Showing the starting state.", false);
      });
    }
    return;
  }
  setInterval(function () {
    document.querySelectorAll("[data-left]").forEach(function (el) {
      var left = Math.max(0, parseInt(el.dataset.left, 10) - 1);
      el.dataset.left = left;
      el.textContent = left ? Math.floor(left / 60) + "m " + ("0" + left % 60).slice(-2) + "s left" : "deadline passed";
    });
  }, 1000);
  setInterval(function () {
    var a = document.activeElement;
    var busy = document.querySelector("form.label-form[data-dirty]") ||
      (a && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName));
    if (!busy) refresh().catch(function () {});
  }, 15000);
})();
"""


def render_app(results, alerts, results_name="results.json", label=None, footer=None,
               refresh_s=None, live=None, demo=None, nav=None):
    """Three-column observability dashboard layout."""
    meta = results["meta"]
    policy = triage.Policy(**meta["policy"])
    records = {r["id"]: r for r in results["alerts"]}
    known = {a["id"] for a in alerts}
    alerts = list(alerts) + [{"id": i, "env": "prod"} for i in records if i not in known]
    alerts = [a for a in alerts if a["id"] in records]
    report = evaluate.compute_report(results, alerts)
    decisions = report["decisions"]
    judgments = {i: triage.Judgment(**r["judgment"]) for i, r in records.items() if r.get("judgment")}
    errors = {i: r["error"] for i, r in records.items() if r.get("error")}
    alerts_by_id = {a["id"]: a for a in alerts}

    pending_count = len((live or {}).get("pending") or [])
    alert_count = len(alerts)
    s = results["summary"]

    # Count decisions by kind
    counts = {}
    for d in decisions.values():
        counts[kind(d, live)] = counts.get(kind(d, live), 0) + 1

    # ---- Ticker ----
    ticker_items = []
    for idx, a in enumerate(alerts):
        d = decisions.get(a["id"])
        if not d:
            continue
        k = kind(d, live)
        color = KIND_COLORS.get(k, "var(--graphite)")
        j = judgments.get(a["id"])
        pp = f"p={j.p_page:.2f}" if j else ""
        call = records.get(a["id"], {}).get("call")
        ms = f"{call['ms']} ms" if call else ""
        label_text = KIND_LABEL.get(k, k).lower()
        ticker_items.append(
            f'<span class="ticker-item"><span class="ticker-dot" style="background:{color}"></span>'
            f'jev #{idx + 1} {label_text}: {esc(display_title(a)[:40])} {esc(pp)} {esc(ms)}</span>')
    ticker_html = "".join(ticker_items)

    # ---- Left column ----
    # Timing panel
    total_ms = sum(r.get("call", {}).get("ms", 0) for r in records.values() if r.get("call"))
    total_s = total_ms / 1000 if total_ms else 0
    donut = donut_svg(counts, alert_count)
    breakdown_items = "".join(
        f'<div class="breakdown-item"><span class="breakdown-dot" style="background:{KIND_COLORS.get(k, "var(--graphite)")}"></span>'
        f'<strong>{counts.get(k, 0)}</strong> {KIND_LABEL.get(k, k)}</div>'
        for k in ["page", "review", "ticket", "quiet", "linked"] if counts.get(k, 0) > 0)

    timing_panel = (
        f'<div class="lp"><div class="lp-title">Triage timing</div>'
        f'<div class="lp-row"><div><div class="lp-big">{total_s:.2f} s</div>'
        f'<div class="lp-sub">{alert_count} decisions</div></div>'
        f'<div class="donut-wrap">{donut}<div class="donut-center">'
        f'<span class="donut-num">{alert_count}</span>'
        f'<span class="donut-label">alerts</span></div></div></div>'
        f'<div class="breakdown">{breakdown_items}</div></div>')

    # Latency panel
    latencies = sorted(r.get("call", {}).get("ms", 0) for r in records.values() if r.get("call"))
    if latencies:
        p50 = latencies[len(latencies) // 2]
        p1 = latencies[0]
        p95 = latencies[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[0]
        max_lat = max(latencies) or 1
        lat_bars = "".join(
            f'<div class="lat-row"><span class="lat-label">{lbl}</span>'
            f'<span class="lat-bar"><span class="lat-fill" style="width:{val / max_lat * 100:.0f}%;background:var(--accent)"></span></span>'
            f'<span class="lat-val">{val} ms</span></div>'
            for lbl, val in [("p50", p50), ("p1", p1), ("p95", p95)])
        lat_note = ' <span style="font-weight:400;color:var(--graphite)">(scripted)</span>' if scripted(results) else ''
        latency_panel = f'<div class="lp"><div class="lp-title">Latency per decision{lat_note}</div>{lat_bars}</div>'
    else:
        latency_panel = ""

    # Cost panel
    cost_html = ""
    if s.get("cost_usd"):
        cost_html = (f'<div class="lp"><div class="lp-title">Total cost</div>'
                     f'<div class="lp-big">${s["cost_usd"]:.4f}</div></div>')

    # Demo clock + controls panel
    demo_panel = ""
    if demo:
        minutes = demo.get("advance_min", 15)
        timeout_off = " disabled" if demo.get("timeout_used") else ""
        demo_panel = (
            f'<div class="lp"><div class="lp-title">Demo clock</div>'
            f'<div class="lp-clock" id="lp-clock-val">{esc(clock(demo["clock"]))}</div>'
            f'<div class="lp-speed">15&times; speed</div>'
            f'<div class="lp-actions" style="margin-top:8px">'
            f'<button type="button" class="lp-btn" id="demo-advance" data-demo="advance">Advance {minutes} min</button>'
            f'<button type="button" class="lp-btn" id="demo-timeout" data-demo="timeout"{timeout_off}>Jev timeout</button>'
            f'<button type="button" class="lp-btn danger" id="demo-reset">Start over</button></div>'
            f'<p class="live-status" id="demo-status" role="status" style="margin-top:6px;font-size:11px"></p></div>')

    # Demo banner
    banner_html = ""
    if demo:
        banner_html = f'<div class="lp" style="border-color:var(--review)">{demo_banner(demo)}</div>'

    # Reviews panel in left column
    pending_panel = ""
    if live:
        pending_panel = pending_section(live, alerts_by_id, demo)

    left_col = (f'<div id="left-content">{banner_html}{timing_panel}{latency_panel}{cost_html}'
                f'{demo_panel}{pending_panel}</div>')

    # ---- Center column: event feed ----
    lead, follow = thesis(decisions, live)
    cards = []
    for idx, a in enumerate(alerts):
        d = decisions.get(a["id"])
        if not d:
            continue
        j = judgments.get(a["id"])
        record = records.get(a["id"], {})
        cards.append(event_card(idx, a, d, j, record, live))

    notices_html = notices(results, decisions, errors, report)

    feed = (f'<div id="feed-content">'
            f'{notices_html}'
            f'{"".join(cards)}</div>')

    # ---- Right column: inspector ----
    # Decision log table
    log_rows = []
    for idx, a in enumerate(alerts):
        d = decisions.get(a["id"])
        if not d:
            continue
        j = judgments.get(a["id"])
        k = kind(d, live)
        color = KIND_COLORS.get(k, "var(--graphite)")
        call = records.get(a["id"], {}).get("call")
        ms = f"{call['ms']}" if call else "-"
        pp = f"{j.p_page:.2f}" if j else "-"
        label_text = KIND_LABEL.get(k, k).lower()
        log_rows.append(
            f'<tr data-id="{esc(a["id"])}">'
            f'<td>#{idx + 1}</td>'
            f'<td><span class="log-dot" style="background:{color}"></span></td>'
            f'<td>{esc(label_text)}</td>'
            f'<td>{esc(pp)}</td>'
            f'<td>{esc(ms)}</td></tr>')

    log_html = (f'<table class="dec-log" id="log-content"><thead><tr>'
                f'<th>#</th><th></th><th>Decision</th><th>P(page)</th><th>ms</th>'
                f'</tr></thead><tbody>{"".join(log_rows)}</tbody></table>')

    # Inspector hidden data
    detail_data = inspector_data(alerts, decisions, judgments, records, report, live)

    # Evaluation + facts + shadow for inspector tabs
    eval_section = evaluation(report, scripted(results))
    facts_section = run_facts(results)
    shadow_html = shadow_section(live.get("shadow"), alerts_by_id) if live and live.get("shadow") is not None else ""
    live_ctrl = live_controls(live) if live else ""

    # ---- Demo engine config ----
    demo_script = ""
    if demo:
        config = json.dumps(demo["engine"]).replace("</", "<\\/")
        demo_script = f"<script>window.JEV_DEMO = {{engine: {config}}};</script>"

    date = run_date(meta.get("generated_at"))
    demo_clock_val = esc(clock(demo["clock"])) if demo else ""

    site_links = ""
    if nav:
        links = "".join(f'<a href="{esc(href)}">{esc(text)}</a>' for text, href in nav)
        site_links = f'<nav class="site-nav" aria-label="jev-oncall site">{links}</nav>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>jev-oncall &mdash; {esc(date)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&amp;display=swap">
<style>{CSS}{LIVE_CSS}{APP_CSS}</style>
</head>
<body class="app" data-demo-clock="{demo_clock_val}">
<div class="ticker"><div class="ticker-inner" id="ticker-inner">{ticker_html}{ticker_html}</div></div>
<header class="app-header">
  <p class="mark">jev-oncall</p>
  <span style="font-size:13px;color:var(--graphite)">{esc(date)}</span>
  {site_links}
</header>
<div class="app-body">
  <div class="col-left">{left_col}</div>
  <div class="col-center">
    <div class="feed-header" id="feed-header">
      <h1>{esc(lead)}</h1>
      <div class="feed-sub">{esc(follow) if follow else ""}</div>
    </div>
    <div class="feed-scroll">{feed}</div>
  </div>
  <div class="col-right" id="col-right">
    <div class="insp-header">
      <h2>Inspector <button type="button" id="auto-play" class="auto-play-btn" title="Pause">&#9208;</button></h2>
      <div class="insp-sub">Auto-cycling</div>
    </div>
    <nav class="insp-tabs">
      <button class="insp-tab active" data-tab="analysis">Analysis</button>
      <button class="insp-tab" data-tab="eval">Evaluation</button>
      <button class="insp-tab" data-tab="compare">Compare</button>
      <button class="insp-tab" data-tab="log">Log</button>
    </nav>
    <div class="insp-body">
      <div class="insp-pane active" id="insp-analysis">
        <div style="color:var(--graphite);font-size:13px;padding:20px 0;text-align:center">
          Select an alert from the feed to see its analysis.</div>
      </div>
      <div class="insp-pane" id="insp-eval">{eval_section}{facts_section}{live_ctrl}</div>
      <div class="insp-pane" id="insp-compare">{shadow_html}</div>
      <div class="insp-pane" id="insp-log">{log_html}</div>
    </div>
  </div>
</div>
{detail_data}
{demo_script}
<div id="live-announce" class="sr-only" role="status" aria-live="polite"></div>
<script>{APP_LIVE_JS}</script>
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
