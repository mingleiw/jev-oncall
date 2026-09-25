"""Shadow mode: record what jev-oncall would have done next to what your
current paging did, so a team can compare before trusting it.

Your current paging routes each alert by its configured severity. That is
exactly triage.static_route(), the fail-open path, so every decision is
logged beside that baseline from the first alert on. Labels added later
(what the alert really was) turn the log into history evaluate.py can score.

The log is append-only JSONL, one event per line:

    {"type": "start", ...}      the server started: model, policy, teams
    {"type": "decision", ...}   an alert, its decision record, and the baseline
    {"type": "review", ...}     a REVIEW was acked, escalated, or cancelled
    {"type": "label", ...}      what an alert really was

Standard library only.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from datetime import datetime, timezone

import triage

# How each action looks from the pager's point of view. The baseline only
# ever produces "page" or "quiet"; jev-oncall can also ask for a review,
# fold an alert into an incident, or drop it.
_BUCKETS = {"PAGE_NOW": "page", "PAGE": "page", "REVIEW": "review", "DEDUP": "dedup",
            "DROP": "drop", "TICKET": "quiet", "LOG": "quiet"}

# (baseline bucket, jev-oncall bucket) -> comparison. Anything not listed agrees.
COMPARISONS = {
    ("quiet", "page"): "page_added",
    ("quiet", "review"): "review_added",
    ("quiet", "drop"): "dropped",
    ("page", "review"): "page_to_review",
    ("page", "dedup"): "page_deduped",
    ("page", "quiet"): "page_held_back",
    ("page", "drop"): "page_held_back",
}
COMPARISON_LABELS = {
    "agree": "Same as your current routing",
    "page_added": "Would page, your routing didn't",
    "review_added": "Would ask for a review, your routing didn't page",
    "page_to_review": "Your routing paged, would ask for a review instead",
    "page_deduped": "Your routing paged, would fold into an incident that already pages",
    "page_held_back": "Your routing paged, would not page",
    "dropped": "Would drop, nobody sees it",
}

LABEL_KEYS = ("severity", "actionable", "team", "duplicate_of")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compare(action, baseline):
    """How a jev-oncall action differs from the baseline action."""
    key = (_BUCKETS.get(baseline, "quiet"), _BUCKETS.get(action, "quiet"))
    return COMPARISONS.get(key, "agree")


class UnknownAlert(ValueError):
    """A label for an alert this server never received."""


def validate_label(body, teams, known=None, alert_id=None):
    """Check a label posted to /label/<id>. Returns the clean label or raises
    ValueError with a message the caller can show. With `known` (the alert
    ids received), the alert and its cause must be among them."""
    if known is not None and alert_id not in known:
        raise UnknownAlert(f"no alert {alert_id!r} has been received, so there is nothing to label")
    if not isinstance(body, dict):
        raise ValueError("label must be a JSON object")
    unknown = sorted(set(body) - set(LABEL_KEYS))
    if unknown:
        raise ValueError(f"unknown key(s) {', '.join(unknown)}; expected {', '.join(LABEL_KEYS)}")
    if body.get("severity") not in triage.SEV_LEVELS:
        raise ValueError(f"severity must be one of {', '.join(triage.SEV_LEVELS)}")
    if not isinstance(body.get("actionable"), bool):
        raise ValueError("actionable must be true or false")
    team = body.get("team")
    if team is not None and team not in teams:
        raise ValueError(f"team must be one of {', '.join(teams)}, or left out")
    dup = body.get("duplicate_of")
    if dup is not None and (not isinstance(dup, str) or not dup):
        raise ValueError("duplicate_of must be an alert id, or null")
    if dup is not None and dup == alert_id:
        raise ValueError("an alert can't be caused by itself")
    if dup is not None and known is not None and dup not in known:
        raise ValueError(f"duplicate_of {dup!r} is not an alert this server received")
    return {"severity": body["severity"], "actionable": body["actionable"],
            "team": team, "duplicate_of": dup}


class ShadowLog:
    """Append-only JSONL log. Safe to share across the server's threads."""

    def __init__(self, path, clock=None):
        self.path = path
        self.clock = clock  # epoch seconds; None: the wall clock
        self._lock = threading.Lock()

    def _now(self):
        if self.clock is None:
            return _now()
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")

    def _append(self, event):
        line = json.dumps(event, sort_keys=True)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def start(self, config):
        self._append({"type": "start", "at": self._now(), "model": config.model,
                      "policy": asdict(config.policy), "teams": config.teams})

    def decision(self, alert, record):
        baseline = triage.static_route(alert, "shadow baseline").action
        self._append({"type": "decision", "at": self._now(), "id": alert["id"], "alert": alert,
                      "record": record, "baseline": baseline,
                      "comparison": compare(record["action"], baseline)})

    def review(self, alert_id, outcome, by=None):
        """outcome: "acked", "escalated", or "cancelled" (resolved upstream)."""
        self._append({"type": "review", "at": self._now(), "id": alert_id, "outcome": outcome,
                      "by": by})

    def label(self, alert_id, label, by=None):
        self._append({"type": "label", "at": self._now(), "id": alert_id, "label": label, "by": by})


def load(path):
    """All events in the log, oldest first, and how many lines were unreadable.
    A half-written last line after a crash must not stop the report."""
    events, bad = [], 0
    if not os.path.exists(path):
        return events, bad
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(event, dict) and event.get("type"):
                events.append(event)
            else:
                bad += 1
    return events, bad


def _latest(events):
    """The newest decision, label and review outcome per alert id, and the
    newest start event. An alert judged twice counts once, as judged last."""
    decisions, labels, reviews, start = {}, {}, {}, None
    for e in events:
        kind, aid = e.get("type"), e.get("id")
        if kind == "start":
            start = e
        elif kind == "decision" and aid:
            decisions[aid] = e
        elif kind == "label" and aid:
            labels[aid] = e
        elif kind == "review" and aid:
            reviews[aid] = e
    return decisions, labels, reviews, start


def labels_by_id(events):
    """The newest label per alert id, in evaluate.py's "expected" shape."""
    return {aid: e["label"] for aid, e in _latest(events)[1].items()}


def summarize(events, bad_lines=0, recent=25):
    """What /shadow returns: how jev-oncall compares with your current routing."""
    decisions, labels, reviews, start = _latest(events)
    counts = dict.fromkeys(COMPARISON_LABELS, 0)
    for d in decisions.values():
        counts[d.get("comparison", "agree")] = counts.get(d.get("comparison", "agree"), 0) + 1
    outcomes = {}
    for r in reviews.values():
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    baseline_pages = sum(_BUCKETS.get(d["baseline"]) == "page" for d in decisions.values())
    jev_pages = sum(_BUCKETS.get(d["record"]["action"]) == "page" for d in decisions.values())
    # A review nobody acked paged too, later: count it, but apart from the
    # decisions made on arrival.
    escalated = sum(1 for aid, r in reviews.items()
                    if r["outcome"] == "escalated" and aid in decisions)
    differing = [d for d in decisions.values() if d.get("comparison", "agree") != "agree"]
    differing.sort(key=lambda d: d["at"], reverse=True)
    return {
        "alerts": len(decisions),
        "since": min((d["at"] for d in decisions.values()), default=None),
        "model": start.get("model") if start else None,
        "comparisons": [{"key": k, "label": COMPARISON_LABELS[k], "count": counts[k]}
                        for k in COMPARISON_LABELS],
        "pages": {"your_routing": baseline_pages, "jev_oncall": jev_pages,
                  "escalated_reviews": escalated},
        "reviews": outcomes,
        "labeled": sum(1 for aid in labels if aid in decisions),
        "unreadable_lines": bad_lines,
        "recent_differences": [{
            "id": d["id"],
            "title": d["alert"].get("title", ""),
            "your_routing": d["baseline"],
            "jev_oncall": d["record"]["action"],
            "comparison": COMPARISON_LABELS[d["comparison"]],
            "reasons": d["record"].get("reasons", []),
            "at": d["at"],
        } for d in differing[:recent]],
    }


def to_results(events):
    """The log as (results, alerts) in triage.py's results.json shape, with
    each label attached as the alert's "expected", ready for evaluate.py.
    Only labels posted to /label count: an "expected" block that arrived
    inside a webhook payload is dropped, so /shadow and the evaluation agree
    on what was labeled."""
    decisions, labels, _, start = _latest(events)
    alerts, records = [], []
    for aid, d in decisions.items():
        alert = {k: v for k, v in d["alert"].items() if k != "expected"}
        if aid in labels:
            alert["expected"] = labels[aid]["label"]
        alerts.append(alert)
        records.append(d["record"])
    policy = (start or {}).get("policy") or asdict(triage.Policy())
    models = sorted({r["judgment"]["model"] for r in records if r.get("judgment")})
    results = {
        "meta": {
            "version": 2,
            "model_requested": (start or {}).get("model", triage.MODEL),
            "models_answered": models,
            "policy": policy,
            "teams": (start or {}).get("teams", triage.TEAM_CRITERIA),
            "generated_at": _now(),
            "note": f"From a shadow log: {len(records)} alerts, {len(labels)} labeled.",
        },
        "alerts": records,
    }
    return results, alerts
