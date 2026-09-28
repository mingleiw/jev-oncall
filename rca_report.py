#!/usr/bin/env python3
"""RCA leaderboard: render saved RCA traces as one self-contained HTML page.

    python3 rca_experiment.py --report rca_traces.jsonl --html rca_report.html

Every model and setup is ranked, overall and per scenario, on the same
scores the text report prints (counts, not percentages), with cost and time
per trial, a side-by-side of each model alone vs with Jev, and every trial's
path: the checks it ran and how the measured P(deploy) moved.

Standard library only; every string from a trace is escaped.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

import rca_experiment as rca

COLUMNS = [
    # key, kind, header, better ("high" / "low" / None), help
    ("correct_hypothesis", "count", "Right cause", "high", "Final hypothesis is the true one"),
    ("found_mechanism", "count", "Mechanism", "high", "Explained how it failed, from a check that shows it"),
    ("changed_direction", "count", "Changed course", "high",
     "Deploy led before the version check and not after (trials where it led)"),
    ("p_decoy_drop", "mean", "P(deploy) drop", "high", "Mean fall in P(deploy) across the version check"),
    ("blamed_decoy", "count", "Blamed deploy", "low", "Final answer blamed the deploy"),
    ("cited_trap", "count", "Cited a trap", "low", "Cited a failed or empty query as evidence"),
    ("checks_used", "mean", "Checks", "low", "Mean checks run per trial"),
    ("tokens", "mean", "Tokens", "low", "Mean model tokens per trial, input plus output"),
    ("seconds", "mean", "Seconds", "low", "Mean wall time per trial"),
]


def _frac(scores, key):
    vals = [s[key] for s in scores if s.get(key) is not None]
    return (sum(bool(v) for v in vals) / len(vals)) if vals else 0.0


def _mean(scores, key):
    vals = [s[key] for s in scores if s.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def scored(traces):
    """Each trace with its score against its own scenario's truth."""
    truths, out = {}, []
    for t in traces:
        name = t.get("scenario", rca.DEFAULT_SCENARIO)
        if name not in truths:
            truths[name] = rca.load_ground_truth(name)
        s = rca.score_trial(t, truths[name])
        usage = t.get("usage") or {}
        s["tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        s["seconds"] = t.get("seconds")
        out.append((t, s, truths[name]))
    return out


def rank_key(scores):
    """Right cause first, then mechanism, then changing course; fewer deploy
    blames, then fewer checks, break ties."""
    return (-_frac(scores, "correct_hypothesis"), -_frac(scores, "found_mechanism"),
            -_frac(scores, "changed_direction"), _frac(scores, "blamed_decoy"),
            _mean(scores, "checks_used") or 0)


def leaderboard(rows):
    """[(model, setup, [scores])] ranked best first."""
    groups = {}
    for t, s, _ in rows:
        groups.setdefault((t.get("model", "?"), t["setup"]), []).append(s)
    return sorted(((m, st, sc) for (m, st), sc in groups.items()),
                  key=lambda r: (rank_key(r[2]), r[0], r[1]))


def _cell(scores, key, kind):
    if key in ("tokens", "seconds"):
        v = _mean(scores, key)
        if v is None:
            return "–"
        return f"{v / 1000:.1f}k" if key == "tokens" and v >= 1000 else (
            f"{v:.0f}" if key == "tokens" else f"{v:.0f}s")
    if kind == "count":
        vals = [s[key] for s in scores if s.get(key) is not None]
        return f"{sum(bool(v) for v in vals)}/{len(vals)}" if vals else "–"
    v = _mean(scores, key)
    if v is None:
        return "–"
    return f"{v:+.2f}" if key == "p_decoy_drop" else f"{v:.1f}"


def _tone(scores, key, better):
    """good / bad / "" for a count cell, so the table reads at a glance."""
    if better is None or key in ("tokens", "seconds", "checks_used", "p_decoy_drop"):
        return ""
    vals = [s[key] for s in scores if s.get(key) is not None]
    if not vals:
        return ""
    f = sum(bool(v) for v in vals) / len(vals)
    good = f >= 0.8 if better == "high" else f == 0
    bad = f <= 0.2 if better == "high" else f >= 0.5
    return "good" if good else "bad" if bad else ""


def _n(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def _b(count, word):
    return f"<b>{count}</b> {word}" + ("" if count == 1 else "s")


def _e(text):
    return html.escape(str(text), quote=True)


def _setup_chip(setup):
    return (f'<span class="chip jev">with Jev</span>' if setup == "jev"
            else '<span class="chip alone">alone</span>')


def table(rows, caption):
    board = leaderboard(rows)
    head = "".join(f'<th scope="col" title="{_e(h)}">{_e(label)}</th>'
                   for _, _, label, _, h in COLUMNS)
    body = []
    for i, (model, setup, scores) in enumerate(board, 1):
        cells = "".join(f'<td class="num {_tone(scores, k, b)}">{_e(_cell(scores, k, kind))}</td>'
                        for k, kind, _, b, _ in COLUMNS)
        body.append(f'<tr class="{"row-jev" if setup == "jev" else ""}"><td class="rank">{i}</td>'
                    f'<th scope="row"><span class="model">{_e(model)}</span> {_setup_chip(setup)}'
                    f'<span class="n">{_n(len(scores), "trial")}</span></th>{cells}</tr>')
    return (f'<div class="scroll"><table><caption>{_e(caption)}</caption>'
            f'<thead><tr><th scope="col" class="rank">#</th><th scope="col">Model</th>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def lift(rows):
    """Per model: alone vs with Jev on the scores that matter most."""
    by = {}
    for t, s, _ in rows:
        by.setdefault(t.get("model", "?"), {}).setdefault(t["setup"], []).append(s)
    cards = []
    for model in sorted(by):
        alone, jev = by[model].get("alone", []), by[model].get("jev", [])
        if not alone or not jev:
            continue
        lines = []
        for key, label, better in (("correct_hypothesis", "Right cause", "high"),
                                   ("changed_direction", "Changed course", "high"),
                                   ("blamed_decoy", "Blamed deploy", "low")):
            a, j = _frac(alone, key), _frac(jev, key)
            delta = (j - a) if better == "high" else (a - j)
            mark = "up" if delta > 0.001 else "down" if delta < -0.001 else "flat"
            word = {"up": "better with Jev", "down": "worse with Jev", "flat": "no change"}[mark]
            lines.append(f'<li><span class="lab">{label}</span>'
                         f'<span class="val">{_e(_cell(alone, key, "count"))}</span>'
                         f'<span class="arrow" aria-hidden="true">→</span>'
                         f'<span class="val">{_e(_cell(jev, key, "count"))}</span>'
                         f'<span class="delta {mark}">{word}</span></li>')
        cards.append(f'<article class="lift"><h3>{_e(model)}</h3>'
                     f'<p class="sub">alone → with Jev</p><ul>{"".join(lines)}</ul></article>')
    return "".join(cards) or '<p class="empty">Needs both setups for a model.</p>'


def sparkline(trace, truth):
    """Measured P(deploy) at each state, with the version check marked."""
    beliefs = rca.measured_beliefs(trace)
    decoy = truth["decoy"]
    pts = [(i, b[decoy]) for i, b in enumerate(beliefs) if b]
    if len(pts) < 2:
        return '<span class="spark-empty">not enough beliefs</span>'
    w, h, pad = 150, 40, 4
    n = max(len(beliefs) - 1, 1)
    x = lambda i: pad + (w - 2 * pad) * i / n
    y = lambda p: pad + (h - 2 * pad) * (1 - p)
    path = " ".join(f"{'M' if k == 0 else 'L'}{x(i):.1f},{y(p):.1f}" for k, (i, p) in enumerate(pts))
    marker = ""
    if truth["version_check"] in trace["observed"]:
        k = trace["observed"].index(truth["version_check"]) + 1
        marker = (f'<line class="vc" x1="{x(k):.1f}" x2="{x(k):.1f}" y1="{pad}" y2="{h - pad}"/>')
    dots = "".join(f'<circle cx="{x(i):.1f}" cy="{y(p):.1f}" r="2"/>' for i, p in pts)
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="P(deploy) from {pts[0][1]:.2f} to {pts[-1][1]:.2f}">'
            f'<line class="base" x1="{pad}" x2="{w - pad}" y1="{y(0.5):.1f}" y2="{y(0.5):.1f}"/>'
            f'{marker}<path d="{path}"/>{dots}</svg>')


def trials(rows):
    items = []
    for t, s, truth in sorted(rows, key=lambda r: (r[0].get("scenario", ""), r[0].get("model", ""),
                                                   r[0]["setup"], r[0].get("trial", 0))):
        final = t.get("final")
        if final:
            verdict = ("right" if s["correct_hypothesis"] else "wrong")
            answer = f'<span class="ans {verdict}">{_e(final["hypothesis"])}</span>'
        else:
            answer = f'<span class="ans none">{_e(t.get("status", "no answer"))}</span>'
        chips = []
        for c in t["observed"]:
            cls = ("vc" if c == truth["version_check"] else "trap" if c in truth["traps"]
                   else "noise" if c in truth["noise"] else "")
            chips.append(f'<code class="{cls}">{_e(c)}</code>')
        items.append(
            f'<tr><td>{_e(t.get("scenario", ""))}</td><td>{_e(t.get("model", ""))} {_setup_chip(t["setup"])}</td>'
            f'<td class="num">{_e(t.get("trial", ""))}</td><td>{sparkline(t, truth)}</td>'
            f'<td class="path">{"".join(chips) or "–"}</td><td>{answer}</td></tr>')
    return ('<div class="scroll"><table class="trials"><thead><tr><th scope="col">Scenario</th>'
            '<th scope="col">Model</th><th scope="col">Trial</th><th scope="col">P(deploy) by step</th>'
            '<th scope="col">Checks in order</th><th scope="col">Answer</th></tr></thead>'
            f'<tbody>{"".join(items)}</tbody></table></div>')


CSS = """
:root{--bg:#f4f6f8;--surface:#ffffff;--ink:#17202b;--muted:#5a6676;--line:#dbe1e8;--soft:#eef2f5;
--jev:#0b7a73;--jev-soft:#dff1ef;--alone:#5a6676;--good:#1d7a4a;--good-soft:#e1f2e8;--bad:#b0302a;
--bad-soft:#f8e3e1;--warn:#8a5a00;--warn-soft:#fbefd4;--vc:#7a4cc2}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0f1318;
--surface:#161b22;--ink:#e3e8ee;--muted:#95a1b0;--line:#27303b;--soft:#1c232c;--jev:#46c2b7;
--jev-soft:#123330;--alone:#95a1b0;--good:#58c28a;--good-soft:#14301f;--bad:#f07a72;--bad-soft:#3a1a18;
--warn:#e3b35a;--warn-soft:#33270f;--vc:#b18cf0}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0f1318;--surface:#161b22;--ink:#e3e8ee;--muted:#95a1b0;
--line:#27303b;--soft:#1c232c;--jev:#46c2b7;--jev-soft:#123330;--alone:#95a1b0;--good:#58c28a;
--good-soft:#14301f;--bad:#f07a72;--bad-soft:#3a1a18;--warn:#e3b35a;--warn-soft:#33270f;--vc:#b18cf0}
body{background:var(--bg);color:var(--ink);font:15px/1.55 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
margin:0;padding-inline:16px;padding-block:28px 56px}
main{max-width:1120px;margin:0 auto;display:grid;gap:36px}
main>*,section>*{min-width:0}
h1{font-size:30px;line-height:1.15;margin:0;letter-spacing:-.01em;text-wrap:balance}
h2{font-size:19px;margin:0 0 4px;text-wrap:balance}
h3{font-size:15px;margin:0;font-family:"IBM Plex Mono",ui-monospace,monospace;font-weight:500}
p{margin:0;max-width:68ch}
.eyebrow{font:500 12px/1 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
header{display:grid;gap:10px}
.lede{color:var(--muted)}
.notice{border:1px solid var(--warn);background:var(--warn-soft);color:var(--ink);border-radius:8px;padding:12px 14px;
display:flex;gap:10px;align-items:flex-start}
.notice b{color:var(--warn);font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;letter-spacing:.06em;
text-transform:uppercase;white-space:nowrap;padding-top:2px}
.meta{display:flex;flex-wrap:wrap;gap:8px 20px;font-size:13px;color:var(--muted)}
.meta span b{color:var(--ink);font-weight:500;font-variant-numeric:tabular-nums}
section{display:grid;gap:12px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
caption{text-align:left;padding:12px 14px 4px;font-weight:600;font-size:14px}
th,td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle;white-space:nowrap}
thead th{font:500 12px/1.3 "IBM Plex Mono",ui-monospace,monospace;color:var(--muted);letter-spacing:.02em;
background:var(--soft);cursor:help}
tbody tr:last-child>*{border-bottom:0}
tbody th{font-weight:400}
td.num{text-align:right}
td.rank,th.rank{width:1%;text-align:center;font-family:"IBM Plex Mono",ui-monospace,monospace;color:var(--muted)}
tbody tr:first-child td.rank{color:var(--ink);font-weight:600}
.model{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:13px}
.n{display:block;font-size:12px;color:var(--muted)}
td.good{color:var(--good);background:var(--good-soft)}
td.bad{color:var(--bad);background:var(--bad-soft)}
.chip{display:inline-block;font:500 11px/1 "IBM Plex Mono",ui-monospace,monospace;padding:4px 6px;border-radius:4px;
margin-left:6px;vertical-align:1px}
.chip.jev{color:var(--jev);background:var(--jev-soft)}
.chip.alone{color:var(--alone);background:var(--soft)}
.lifts{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.lift{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px;display:grid;gap:8px}
.lift .sub{font-size:12px;color:var(--muted)}
.lift ul{list-style:none;margin:0;padding:0;display:grid;gap:6px}
.lift li{display:grid;grid-template-columns:1fr auto auto auto;gap:8px;align-items:center;font-size:14px;
font-variant-numeric:tabular-nums}
.lift li .delta{grid-column:1/-1;font-size:12px;margin-top:-4px}
.lift .val{font-family:"IBM Plex Mono",ui-monospace,monospace}
.lift .arrow{color:var(--muted)}
.delta.up{color:var(--good)}.delta.down{color:var(--bad)}.delta.flat{color:var(--muted)}
.scenarios{display:grid;gap:20px}
.trials td{font-size:13px}
.path{white-space:normal;min-width:260px}
.path code{display:inline-block;font:12px/1 "IBM Plex Mono",ui-monospace,monospace;padding:4px 5px;border-radius:4px;
background:var(--soft);margin:2px 3px 2px 0}
.path code.vc{color:var(--vc);outline:1px solid var(--vc)}
.path code.trap{color:var(--bad);background:var(--bad-soft)}
.path code.noise{color:var(--muted);text-decoration:line-through}
.ans{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;padding:3px 6px;border-radius:4px}
.ans.right{color:var(--good);background:var(--good-soft)}
.ans.wrong{color:var(--bad);background:var(--bad-soft)}
.ans.none{color:var(--muted);background:var(--soft)}
.spark{display:block;overflow:visible}
.spark path{fill:none;stroke:var(--ink);stroke-width:1.5}
.spark circle{fill:var(--ink)}
.spark .base{stroke:var(--line);stroke-dasharray:2 3}
.spark .vc{stroke:var(--vc);stroke-width:1.5}
.legend{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:12px;color:var(--muted);align-items:center}
.legend code{font:12px/1 "IBM Plex Mono",ui-monospace,monospace;padding:4px 5px;border-radius:4px;background:var(--soft)}
.legend .vc{color:var(--vc);outline:1px solid var(--vc)}
.legend .trap{color:var(--bad);background:var(--bad-soft)}
.legend .noise{text-decoration:line-through}
details{background:var(--surface);border:1px solid var(--line);border-radius:10px}
details>summary{cursor:pointer;padding:12px 14px;font-weight:600}
details>summary:focus-visible,th:focus-visible{outline:2px solid var(--jev);outline-offset:2px}
details .scroll{border:0;border-top:1px solid var(--line);border-radius:0 0 10px 10px}
.method{font-size:13px;color:var(--muted);display:grid;gap:6px}
.empty{color:var(--muted)}
@media (max-width:560px){h1{font-size:24px}th,td{padding:8px}}
"""


def render(traces, note=None, standalone=True):
    """The report as HTML. standalone=False omits the <html>/<head>/<body> shell."""
    rows = scored(traces)
    scenarios = sorted({t.get("scenario", rca.DEFAULT_SCENARIO) for t, _, _ in rows})
    models = sorted({t.get("model", "?") for t, _, _ in rows})
    modes = sorted({"forced" if t.get("forced") else "free" for t, _, _ in rows})
    notice = (f'<div class="notice" role="note"><b>Note</b><p>{_e(note)}</p></div>' if note else "")
    per_scenario = "".join(
        table([r for r in rows if r[0].get("scenario", rca.DEFAULT_SCENARIO) == n], f"Scenario: {n}")
        for n in scenarios)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = f"""<title>RCA Leaderboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style>
<main>
<header>
<span class="eyebrow">jev-oncall · RCA experiment</span>
<h1>Does the agent change course when the deploy is cleared?</h1>
<p class="lede">Each model investigates frozen incidents alone and with Jev re-scoring every hypothesis
after each check. A deploy looks guilty; a version check shows old and new versions failing alike.
Ranked by right cause, then mechanism, then changing course.</p>
{notice}
<div class="meta"><span>{_b(len(rows), "trial")}</span><span>{_b(len(models), "model")}</span>
<span>{_b(len(scenarios), "scenario")}</span><span>mode <b>{_e(", ".join(modes))}</b></span>
<span>generated <b>{generated}</b></span></div>
</header>
<section aria-labelledby="overall"><h2 id="overall">Ranking, all scenarios</h2>
<p class="lede">Counts are trials that met the test out of trials where it applies. Hover a column for its definition.</p>
{table(rows, "All scenarios combined")}
</section>
<section aria-labelledby="lift"><h2 id="lift">Alone vs with Jev</h2>
<div class="lifts">{lift(rows)}</div>
</section>
<section aria-labelledby="per"><h2 id="per">By scenario</h2>
<p class="lede">A result that holds on one scenario and not the other is itself a finding.</p>
<div class="scenarios">{per_scenario}</div>
</section>
<section aria-labelledby="paths"><h2 id="paths">Every trial</h2>
<div class="legend"><span>P(deploy) as measured: the model's own alone, Jev's with Jev.</span>
<code class="vc">version check</code><code class="trap">trap</code><code class="noise">noise</code></div>
<details><summary>{_n(len(rows), "trial")}: checks in order and how P(deploy) moved</summary>{trials(rows)}</details>
</section>
<section class="method" aria-labelledby="how"><h2 id="how">How to read this</h2>
<p>Right cause and mechanism are scored separately: naming the right hypothesis without the check
that shows how it failed doesn't count as finding the mechanism. Changed course only counts trials
where the deploy led right before the version check. Tokens are the reasoning model's; Jev's cost
is not included. Five trials per cell shows direction, not significance.</p>
</section>
</main>"""
    if not standalone:
        return body
    head, main = body.split("<main>", 1)
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'{head}</head><body><main>{main}</body></html>\n')


def write(traces, path, note=None, standalone=True):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(traces, note, standalone))
