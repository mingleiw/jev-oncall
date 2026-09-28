#!/usr/bin/env python3
"""RCA leaderboard: render saved RCA traces as one self-contained HTML page.

    python3 rca_experiment.py --report rca_traces.jsonl --html rca_report.html

Laid out like an open model benchmark: one headline score per system
(% Resolved, with a 95% Wilson interval and the raw count), systems ranked in
a sortable table, a tab per scenario, a chart of each model alone vs with
Jev, cost and time per trial, the trial-level evidence behind every number,
and the commands to run it on another model.

Standard library only; every string from a trace is escaped.
"""
from __future__ import annotations

import html
import math
from datetime import datetime, timezone

import rca_experiment as rca

ORGS = {"anthropic": "Anthropic", "openai": "OpenAI", "test": "Test stand-in"}

# key, header, better ("high" / "low"), definition, denominator ("all" trials or "applicable")
RATES = [
    ("resolved", "% Resolved", "high",
     "Named the right cause and explained its mechanism from a check that shows it. "
     "Unfinished trials count as not resolved.", "all"),
    ("correct_hypothesis", "Right cause", "high", "Final hypothesis is the true one.", "all"),
    ("changed_direction", "Changed course", "high",
     "The model's own stated belief, in both setups: of trials where the deploy led right "
     "before the version check, the share where it no longer led right after.", "applicable"),
    ("blamed_decoy", "Blamed deploy", "low", "Final answer blamed the deploy.", "all"),
    ("cited_trap", "Cited a trap", "low", "Cited a failed or empty query as evidence.", "applicable"),
]
MEANS = [
    ("checks_to_cause", "To cause", "Mean checks until the model's own top hypothesis was the true cause. "
     "Lower is faster."),
    ("checks_used", "Checks", "Mean checks run per trial."),
    ("tokens", "Tokens", "Mean reasoning-model tokens per trial, input plus output. Jev's are not included."),
    ("seconds", "Time", "Mean wall time per trial."),
]


def _e(text):
    return html.escape(str(text), quote=True)


def _n(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def wilson(k, n, z=1.96):
    """95% Wilson score interval for k successes in n trials."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def scored(traces):
    """Each trace with its score against its own scenario's truth."""
    truths, out = {}, []
    for t in traces:
        name = t.get("scenario", rca.DEFAULT_SCENARIO)
        if name not in truths:
            truths[name] = rca.load_ground_truth(name)
        s = rca.score_trial(t, truths[name])
        s["resolved"] = bool(s["correct_hypothesis"] and s["found_mechanism"])
        for key in ("correct_hypothesis", "blamed_decoy"):
            s[key] = bool(s[key])  # an unfinished trial got neither right
        usage = t.get("usage") or {}
        s["tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        s["seconds"] = t.get("seconds")
        out.append((t, s, truths[name]))
    return out


def rate(scores, key):
    """(k, n): trials that met the test, of trials where it applies."""
    vals = [s[key] for s in scores if s.get(key) is not None]
    return sum(bool(v) for v in vals), len(vals)


def _frac(scores, key):
    k, n = rate(scores, key)
    return k / n if n else 0.0


def _mean(scores, key):
    vals = [s[key] for s in scores if s.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def rank_key(scores):
    """% Resolved first; then changing course, fewer deploy blames, fewer checks."""
    return (-_frac(scores, "resolved"), -_frac(scores, "changed_direction"),
            _frac(scores, "blamed_decoy"), _mean(scores, "checks_used") or 0)


def leaderboard(rows):
    """[(model, setup, [scores])] ranked best first."""
    groups = {}
    for t, s, _ in rows:
        groups.setdefault((t.get("model", "?"), t["setup"]), []).append(s)
    return sorted(((m, st, sc) for (m, st), sc in groups.items()),
                  key=lambda r: (rank_key(r[2]), r[0], r[1]))


def split_model(spec):
    provider, _, name = spec.partition(":")
    return (ORGS.get(provider, provider or "?"), name or spec)


SETUP_TAGS = {"jev": "+ Jev", "jev-contra": "+ Jev contradictions"}
SETUP_LABELS = {"alone": "alone", "jev": "+ Jev (ranking)", "jev-contra": "+ Jev (contradictions only)"}


def setup_tag(setup):
    label = SETUP_TAGS.get(setup)
    return f'<span class="tag {_e(setup)}">{_e(label)}</span>' if label else ""


def system_name(model, setup):
    org, name = split_model(model)
    tag = setup_tag(setup)
    return (f'<span class="sys"><span class="name">{_e(name)}</span>{tag}</span>'
            f'<span class="org">{_e(org)}</span>')


def _rate_cell(scores, key, better, headline=False):
    k, n = rate(scores, key)
    if not n:
        return '<td class="num" data-v="-1">–</td>'
    p = k / n
    lo, hi = wilson(k, n)
    tone = ("good" if (p >= 0.8 if better == "high" else p == 0) else
            "bad" if (p <= 0.2 if better == "high" else p >= 0.5) else "")
    if headline:
        return (f'<td class="num headline" data-v="{p:.4f}"><div class="score">'
                f'<span class="bar" aria-hidden="true"><span style="width:{p * 100:.1f}%"></span></span>'
                f'<span class="pct">{p * 100:.1f}</span></div>'
                f'<span class="sub">{k}/{n} · CI {lo * 100:.0f}–{hi * 100:.0f}</span></td>')
    return (f'<td class="num {tone}" data-v="{p:.4f}"><span class="pct">{p * 100:.0f}%</span>'
            f'<span class="sub">{k}/{n}</span></td>')


def _mean_cell(scores, key):
    v = _mean(scores, key)
    if v is None:
        return '<td class="num" data-v="-1">–</td>'
    text = (f"{v / 1000:.1f}k" if key == "tokens" and v >= 1000 else f"{v:.0f}" if key == "tokens"
            else f"{v:.0f}s" if key == "seconds" else f"{v:.1f}")
    return f'<td class="num" data-v="{v:.4f}">{text}</td>'


def table(rows, label):
    board = leaderboard(rows)
    heads = [f'<th scope="col" class="num"><button type="button" data-col="{i + 3}" title="{_e(d)}">'
             f'{_e(h)}</button></th>' for i, (_, h, _, d, _) in enumerate(RATES)]
    heads += [f'<th scope="col" class="num"><button type="button" data-col="{i + 3 + len(RATES)}" '
              f'title="{_e(d)}">{_e(h)}</button></th>' for i, (_, h, d) in enumerate(MEANS)]
    body = []
    prev, rank = None, 0
    for i, (model, setup, scores) in enumerate(board, 1):
        key = rank_key(scores)
        rank = rank if key == prev else i  # ties share a rank
        prev = key
        cells = "".join(_rate_cell(scores, k, b, headline=(k == "resolved")) for k, _, b, _, _ in RATES)
        cells += "".join(_mean_cell(scores, k) for k, _, _ in MEANS)
        body.append(f'<tr class="{_e(setup)}"><td class="rank" data-v="{i}">{rank}</td>'
                    f'<th scope="row">{system_name(model, setup)}</th>'
                    f'<td class="num" data-v="{len(scores)}">{len(scores)}</td>{cells}</tr>')
    return (f'<div class="scroll"><table class="board" aria-label="{_e(label)}"><thead><tr>'
            f'<th scope="col" class="rank"><button type="button" data-col="0">Rank</button></th>'
            f'<th scope="col">System</th>'
            f'<th scope="col" class="num"><button type="button" data-col="2" title="Trials run">Trials</button></th>'
            f'{"".join(heads)}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def lift_chart(rows):
    """Bars per model, one per setup it ran: % Resolved alone and with Jev, drawn to one scale."""
    by = {}
    for t, s, _ in rows:
        by.setdefault(t.get("model", "?"), {}).setdefault(t["setup"], []).append(s)
    models = [m for m in sorted(by) if by[m].get("alone") and set(by[m]) & set(rca.JEV_SETUPS)]
    if not models:
        return '<p class="muted">Run a model alone and with Jev to compare them.</p>'
    shown = [s for s in rca.SETUPS if any(by[m].get(s) for m in models)]
    bar, gap, left, top, plot_h = 40, 8, 44, 18, 180
    group = len(shown) * (bar + gap) + 44
    width = left + group * len(models) + 12
    height = top + plot_h + 52
    y = lambda p: top + plot_h * (1 - p)
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="% Resolved per model, alone and with Jev">']
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(f'<line class="grid" x1="{left}" x2="{width - 8}" y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>'
                     f'<text class="axis" x="{left - 8}" y="{y(tick) + 4:.1f}" text-anchor="end">{tick * 100:.0f}%</text>')
    for i, m in enumerate(models):
        x0 = left + i * group + (group - len(shown) * bar - (len(shown) - 1) * gap) / 2
        for j, setup in enumerate(shown):
            if not by[m].get(setup):
                continue
            k, n = rate(by[m][setup], "resolved")
            p = k / n if n else 0
            x = x0 + j * (bar + gap)
            h = max(plot_h * p, 1.5)
            parts.append(f'<rect class="b-{setup}" x="{x:.1f}" y="{y(p) if p else top + plot_h - 1.5:.1f}" '
                         f'width="{bar}" height="{h:.1f}" rx="2"><title>{_e(m)} {setup}: {k}/{n}</title></rect>'
                         f'<text class="val" x="{x + bar / 2:.1f}" y="{(y(p) if p else top + plot_h) - 6:.1f}" '
                         f'text-anchor="middle">{p * 100:.0f}%</text>')
        _, name = split_model(m)
        parts.append(f'<text class="label" x="{left + i * group + group / 2:.1f}" y="{top + plot_h + 20}" '
                     f'text-anchor="middle">{_e(name)}</text>')
    parts.append("</svg>")
    legend = '<div class="legend">' + "".join(
        f'<span><i class="sw b-{s}"></i>{_e(SETUP_LABELS[s])}</span>' for s in shown) + "</div>"
    return f'<div class="chart-wrap">{"".join(parts)}</div>{legend}'


def _avg(scores, key):
    v = _mean(scores, key)
    return "–" if v is None else f"{v:.2f}"


def belief_table(rows):
    """P(deploy) across the version check: the model's own belief in both setups, and
    Jev's scores on the + Jev rows, so the two believers are never mixed."""
    by = {}
    for t, s, _ in rows:
        by.setdefault(t.get("model", "?"), {}).setdefault(t["setup"], []).append(s)
    body = []
    for model in sorted(by):
        _, name = split_model(model)
        lines = [("alone", "model", "", by[model].get("alone", []))]
        for s in rca.JEV_SETUPS:
            lines += [(s, "model", "", by[model].get(s, [])), (s, "Jev's scores", "jev_", by[model].get(s, []))]
        for setup, who, pre, scores in lines:
            if not scores:
                continue
            k, n = rate(scores, pre + "changed_direction")
            tag = setup_tag(setup)
            label = f'<span class="sys"><span class="name">{_e(name)}</span>{tag}</span>'
            body.append(f'<tr class="{"jevrow" if pre else ""}"><th scope="row">{label}</th>'
                        f'<td>{_e(who)}</td>'
                        f'<td class="num">{_avg(scores, pre + "p_decoy_start")}</td>'
                        f'<td class="num">{_avg(scores, pre + "p_decoy_before")}</td>'
                        f'<td class="num">{_avg(scores, pre + "p_decoy_after")}</td>'
                        f'<td class="num">{_avg(scores, pre + "p_decoy_drop")}</td>'
                        f'<td class="num">{_avg(scores, pre + "decoy_held")}</td>'
                        f'<td class="num">{f"{k}/{n}" if n else "–"}</td></tr>')
    return ('<div class="scroll"><table class="beliefs"><thead><tr><th scope="col">System</th>'
            '<th scope="col">Whose belief</th><th scope="col" class="num">At start</th>'
            '<th scope="col" class="num">Before version check</th><th scope="col" class="num">After</th>'
            '<th scope="col" class="num">Drop</th>'
            '<th scope="col" class="num" title="Checks, from the version check on, that the deploy still led">'
            'Deploy held</th><th scope="col" class="num" '
            'title="Deploy led before the version check and not after, of trials where it led">'
            'Changed course</th></tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def sparkline(trace, truth):
    """Measured P(deploy) at each state, with the version check marked."""
    decoy = truth["decoy"]
    model = [(i, b[decoy]) for i, b in enumerate(rca.measured_beliefs(trace)) if b]
    jev = ([(i, b[decoy]) for i, b in enumerate(rca.jev_beliefs(trace)) if b]
           if trace["setup"] in rca.JEV_SETUPS else [])
    if len(model) < 2 and len(jev) < 2:
        return '<span class="muted">–</span>'
    w, h, pad = 120, 32, 4
    n = max(len(trace["states"]) - 1, 1)
    x = lambda i: pad + (w - 2 * pad) * i / n
    y = lambda p: pad + (h - 2 * pad) * (1 - p)
    line = lambda pts, cls: ('<path class="%s" d="%s"/>' % (cls, " ".join(
        f"{'M' if k == 0 else 'L'}{x(i):.1f},{y(p):.1f}" for k, (i, p) in enumerate(pts)))
        if len(pts) >= 2 else "")
    marker = ""
    if truth["version_check"] in trace["observed"]:
        k = trace["observed"].index(truth["version_check"]) + 1
        marker = f'<line class="vc" x1="{x(k):.1f}" x2="{x(k):.1f}" y1="{pad}" y2="{h - pad}"/>'
    label = (f"model P(deploy) {model[0][1]:.2f} to {model[-1][1]:.2f}" if len(model) >= 2 else "")
    if len(jev) >= 2:
        label += f"; Jev {jev[0][1]:.2f} to {jev[-1][1]:.2f}"
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="{_e(label)}">{marker}{line(jev, "jevline")}{line(model, "model")}</svg>')


def trials_table(rows):
    items = []
    for t, s, truth in sorted(rows, key=lambda r: (r[0].get("scenario", ""), r[0].get("model", ""),
                                                   r[0]["setup"], r[0].get("trial", 0))):
        final = t.get("final")
        if final:
            cls = "right" if s["resolved"] else "part" if s["correct_hypothesis"] else "wrong"
            answer = f'<span class="ans {cls}">{_e(final["hypothesis"])}</span>'
        else:
            answer = f'<span class="ans none">{_e(t.get("status", "no answer"))}</span>'
        chips = []
        for c in t["observed"]:
            cls = ("vc" if c == truth["version_check"] else "trap" if c in truth["traps"]
                   else "noise" if c in truth["noise"] else "")
            chips.append(f'<code class="{cls}">{_e(c)}</code>')
        items.append(f'<tr><td>{_e(t.get("scenario", ""))}</td><td>{system_name(t.get("model", "?"), t["setup"])}</td>'
                     f'<td class="num">{_e(t.get("trial", ""))}</td><td>{sparkline(t, truth)}</td>'
                     f'<td class="path">{"".join(chips) or "–"}</td><td>{answer}</td></tr>')
    return ('<div class="scroll"><table class="trials"><thead><tr><th scope="col">Scenario</th>'
            '<th scope="col">System</th><th scope="col" class="num">Trial</th><th scope="col">P(deploy)</th>'
            '<th scope="col">Checks in order</th><th scope="col">Answer</th></tr></thead>'
            f'<tbody>{"".join(items)}</tbody></table></div>')


CSS = """
:root{--bg:#f6f7f9;--surface:#fff;--ink:#141a22;--muted:#5d6878;--line:#e0e4ea;--soft:#f0f2f5;
--accent:#0a7c73;--accent-soft:#e0f2ef;--base:#8a94a3;--good:#1a7a47;--good-soft:#e3f3ea;--bad:#b3322b;
--bad-soft:#f9e5e3;--warn:#8c5a00;--warn-soft:#fcf0d6;--vc:#7446c4}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0e1116;--surface:#151a21;
--ink:#e4e8ee;--muted:#98a3b3;--line:#262e39;--soft:#1b222b;--accent:#4cc4b8;--accent-soft:#12302d;--base:#6b7584;
--good:#5cc68e;--good-soft:#13301f;--bad:#f27d74;--bad-soft:#3b1b19;--warn:#e6b65c;--warn-soft:#342810;--vc:#b18df2}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0e1116;--surface:#151a21;--ink:#e4e8ee;--muted:#98a3b3;
--line:#262e39;--soft:#1b222b;--accent:#4cc4b8;--accent-soft:#12302d;--base:#6b7584;--good:#5cc68e;
--good-soft:#13301f;--bad:#f27d74;--bad-soft:#3b1b19;--warn:#e6b65c;--warn-soft:#342810;--vc:#b18df2}
body{background:var(--bg);color:var(--ink);font:15px/1.55 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
margin:0;padding-inline:16px;padding-block:32px 64px}
main{max-width:1160px;margin:0 auto;display:grid;gap:40px}
main>*,section>*{min-width:0}
.mono,code,.num,.eyebrow,.org,.tag{font-family:"IBM Plex Mono",ui-monospace,monospace}
header{display:grid;gap:12px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:500}
h1{font-size:34px;line-height:1.1;margin:0;letter-spacing:-.015em}
h2{font-size:20px;margin:0;text-wrap:balance}
p{margin:0;max-width:72ch}
.lede{font-size:16px;color:var(--muted)}
.muted{color:var(--muted)}
.notice{border:1px solid var(--warn);background:var(--warn-soft);border-radius:8px;padding:12px 14px;display:flex;gap:12px}
.notice b{color:var(--warn);font:500 12px/1.6 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase;white-space:nowrap}
.facts{display:flex;flex-wrap:wrap;gap:0;border:1px solid var(--line);border-radius:10px;background:var(--surface);overflow:hidden}
.facts div{padding:10px 16px;border-right:1px solid var(--line);display:grid;gap:2px}
.facts div:last-child{border-right:0}
.facts dt{font-size:12px;color:var(--muted)}
.facts dd{margin:0;font:500 15px/1.3 "IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
section{display:grid;gap:14px}
.sec-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:end;gap:12px}
[role=tablist]{display:flex;flex-wrap:wrap;gap:4px;background:var(--soft);padding:4px;border-radius:8px;width:fit-content}
[role=tab]{font:500 13px/1 "IBM Plex Mono",ui-monospace,monospace;border:0;background:transparent;color:var(--muted);
padding:8px 12px;border-radius:6px;cursor:pointer}
[role=tab][aria-selected=true]{background:var(--surface);color:var(--ink);box-shadow:0 1px 2px rgb(0 0 0/.08)}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle;white-space:nowrap}
tbody tr:last-child>*{border-bottom:0}
thead th{font:500 12px/1.3 "IBM Plex Mono",ui-monospace,monospace;color:var(--muted);background:var(--soft)}
thead th button{all:unset;cursor:pointer;display:inline-flex;gap:4px;align-items:center}
thead th button[aria-sort=descending]::after{content:"↓"}
thead th button[aria-sort=ascending]::after{content:"↑"}
.num{text-align:right}
td.rank,th.rank{width:1%;text-align:center}
td.rank{font:600 15px/1 "IBM Plex Mono",ui-monospace,monospace}
tbody th{font-weight:400}
.sys{display:flex;align-items:center;gap:8px}
.name{font-weight:600}
.tag{font-size:11px;font-weight:500;color:var(--accent);background:var(--accent-soft);padding:3px 6px;border-radius:4px}
.org{display:block;font-size:12px;color:var(--muted)}
.headline{min-width:170px}
.score{display:flex;align-items:center;gap:10px;justify-content:flex-end}
.bar{width:90px;height:8px;border-radius:4px;background:var(--soft);overflow:hidden;display:inline-block}
.bar span{display:block;height:100%;background:var(--ink)}
tr.jev .bar span{background:var(--accent)}
.headline .pct{font-size:17px;font-weight:600;min-width:3.2em}
.sub{display:block;font-size:11px;color:var(--muted)}
td.good .pct{color:var(--good)}td.bad .pct{color:var(--bad)}
.chart-wrap{border:1px solid var(--line);border-radius:10px;background:var(--surface);padding:14px 8px 4px;overflow-x:auto}
.chart{display:block;width:100%;max-width:720px;height:auto}
.chart .grid{stroke:var(--line)}
.chart .axis,.chart .label{fill:var(--muted);font:12px "IBM Plex Mono",ui-monospace,monospace}
.chart .label{fill:var(--ink)}
.chart .val{fill:var(--ink);font:500 12px "IBM Plex Mono",ui-monospace,monospace}
.b-alone{fill:var(--base);background:var(--base)}.b-jev{fill:var(--accent);background:var(--accent)}
.b-jev-contra{fill:var(--vc);background:var(--vc)}
.tag.jev-contra{color:var(--vc);background:var(--soft)}
tr.jev-contra .bar span{background:var(--vc)}
.legend{display:flex;gap:18px;font-size:13px;color:var(--muted)}
.legend span{display:inline-flex;gap:6px;align-items:center}
.sw{display:inline-block;width:12px;height:12px;border-radius:2px}
.trials td{font-size:13px}
.path{white-space:normal;min-width:260px}
.path code{display:inline-block;font-size:12px;line-height:1;padding:4px 5px;border-radius:4px;background:var(--soft);margin:2px 3px 2px 0}
code.vc{color:var(--vc);outline:1px solid var(--vc)}
code.trap{color:var(--bad);background:var(--bad-soft)}
code.noise{color:var(--muted);text-decoration:line-through}
.ans{font:12px/1 "IBM Plex Mono",ui-monospace,monospace;padding:4px 6px;border-radius:4px}
.ans.right{color:var(--good);background:var(--good-soft)}
.ans.part{color:var(--warn);background:var(--warn-soft)}
.ans.wrong{color:var(--bad);background:var(--bad-soft)}
.ans.none{color:var(--muted);background:var(--soft)}
.spark{display:block}
.spark path{fill:none;stroke:var(--ink);stroke-width:1.5}
.spark path.jevline{stroke:var(--accent);stroke-dasharray:3 2}
tr.jevrow>*{background:var(--accent-soft)}
.key-model,.key-jev{display:inline-block;width:18px;height:0;border-top:2px solid var(--ink);vertical-align:middle;margin-right:4px}
.key-jev{border-top:2px dashed var(--accent)}
.spark .vc{stroke:var(--vc);stroke-width:1.5}
.keys{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:12px;color:var(--muted);align-items:center}
.keys code{font-size:12px;padding:3px 5px;border-radius:4px;background:var(--soft)}
details{background:var(--surface);border:1px solid var(--line);border-radius:10px}
details>summary{cursor:pointer;padding:12px 14px;font-weight:600}
details .scroll{border:0;border-top:1px solid var(--line);border-radius:0 0 10px 10px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px;align-items:start}
dl.defs{margin:0;display:grid;gap:10px}
dl.defs dt{font-weight:600;font-size:14px}
dl.defs dd{margin:0;color:var(--muted);font-size:14px}
pre{margin:0;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px;overflow-x:auto;
font:13px/1.6 "IBM Plex Mono",ui-monospace,monospace}
@media (max-width:560px){h1{font-size:26px}th,td{padding:8px}.facts div{flex:1 1 40%}}
"""

JS = """
document.querySelectorAll('[role=tablist]').forEach(function (list) {
  var tabs = list.querySelectorAll('[role=tab]');
  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on);
        document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
      });
    });
  });
});
document.querySelectorAll('table.board thead button').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var table = btn.closest('table'), col = +btn.dataset.col, body = table.tBodies[0];
    var dir = btn.getAttribute('aria-sort') === 'descending' ? 'ascending' : 'descending';
    if (col === 0 && !btn.hasAttribute('aria-sort')) dir = 'ascending';
    table.querySelectorAll('thead button').forEach(function (b) { b.removeAttribute('aria-sort'); });
    btn.setAttribute('aria-sort', dir);
    var rows = Array.prototype.slice.call(body.rows);
    rows.sort(function (a, b) {
      var x = +a.cells[col].dataset.v, y = +b.cells[col].dataset.v;
      return dir === 'ascending' ? x - y : y - x;
    });
    rows.forEach(function (r) { body.appendChild(r); });
  });
});
"""


def render(traces, note=None, standalone=True):
    """The report as HTML. standalone=False omits the <html>/<head>/<body> shell."""
    rows = scored(traces)
    scenarios = sorted({t.get("scenario", rca.DEFAULT_SCENARIO) for t, _, _ in rows})
    models = sorted({t.get("model", "?") for t, _, _ in rows})
    modes = sorted({"forced version check" if t.get("forced") else "free choice" for t, _, _ in rows})
    per_system = sorted({len(sc) for _, _, sc in leaderboard(rows)})
    notice = f'<div class="notice" role="note"><b>Note</b><p>{_e(note)}</p></div>' if note else ""
    versions = sorted({str(t.get("harness_version")) for t, _, _ in rows})
    if len(versions) > 1:
        notice += ('<div class="notice" role="note"><b>Mixed</b><p>These traces come from harness versions '
                   f'{_e(", ".join(versions))}, which sent the model or Jev different prompts. Compare '
                   'results within one version; render each version on its own page.</p></div>')
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    panels = [("overall", "Overall", rows)] + [
        (f"s-{n}", n, [r for r in rows if r[0].get("scenario", rca.DEFAULT_SCENARIO) == n]) for n in scenarios]
    tabs = "".join(f'<button type="button" role="tab" id="tab-{pid}" aria-controls="panel-{pid}" '
                   f'aria-selected="{"true" if i == 0 else "false"}">{_e(label)}</button>'
                   for i, (pid, label, _) in enumerate(panels))
    boards = "".join(f'<div role="tabpanel" id="panel-{pid}" aria-labelledby="tab-{pid}"{"" if i == 0 else " hidden"}>'
                     f'{table(prs, label)}</div>' for i, (pid, label, prs) in enumerate(panels))
    defs = "".join(f"<dt>{_e(h)}</dt><dd>{_e(d)}</dd>" for _, h, _, d, _ in RATES)
    defs += "".join(f"<dt>{_e(h)}</dt><dd>{_e(d)}</dd>" for _, h, d in MEANS)
    body = f"""<title>RCA Leaderboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style>
<main>
<header>
<span class="eyebrow">jev-oncall · open RCA benchmark</span>
<h1>RCA Leaderboard</h1>
<p class="lede">Can an agent find the root cause of an outage when the first suspect is wrong? Each system
investigates frozen incidents where a recent deploy looks guilty and a version check clears it.
Systems run alone and with Jev re-scoring every hypothesis after each check.</p>
{notice}
<dl class="facts">
<div><dt>Systems</dt><dd>{len(leaderboard(rows))}</dd></div>
<div><dt>Models</dt><dd>{len(models)}</dd></div>
<div><dt>Scenarios</dt><dd>{len(scenarios)}</dd></div>
<div><dt>Trials per system</dt><dd>{_e("/".join(str(n) for n in per_system))}</dd></div>
<div><dt>Mode</dt><dd>{_e(", ".join(modes))}</dd></div>
<div><dt>Updated</dt><dd>{generated}</dd></div>
</dl>
</header>
<section aria-labelledby="board-h">
<div class="sec-head"><h2 id="board-h">Leaderboard</h2><div role="tablist" aria-label="Scenario">{tabs}</div></div>
{boards}
<p class="muted">Ranked by % Resolved. The bar shows the score; below it, the raw count and a 95% confidence
interval. Click a column to sort; hover it for its definition.</p>
</section>
<section aria-labelledby="lift-h">
<h2 id="lift-h">Does Jev help? % Resolved, alone vs + Jev</h2>
{lift_chart(rows)}
</section>
<section aria-labelledby="belief-h">
<h2 id="belief-h">Belief in the deploy, across the version check</h2>
<p class="muted">Mean P(deploy). The model's own stated belief is measured in both setups; on + Jev rows,
Jev's scores are shown on their own line. A bigger drop from a higher start is not the same as changing course.</p>
{belief_table(rows)}
</section>
<section aria-labelledby="trials-h">
<h2 id="trials-h">Trials</h2>
<div class="keys"><span><i class="key-model"></i>model's P(deploy)</span><span><i class="key-jev"></i>Jev's P(deploy)</span><span>Vertical line: the version check.</span>
<code class="vc">version check</code><code class="trap">trap</code><code class="noise">noise</code></div>
<details><summary>{_n(len(rows), "trial")}: checks in order, P(deploy) and answer</summary>{trials_table(rows)}</details>
</section>
<section class="two" aria-label="Method and submissions">
<div><h2>Metrics</h2><dl class="defs">{defs}</dl></div>
<div style="display:grid;gap:12px"><h2>Run it on your model</h2>
<p class="muted">Any Anthropic model or OpenAI-compatible endpoint. Keys come from environment variables.</p>
<pre>git clone https://github.com/mingleiw/jev-oncall
cd jev-oncall
export ANTHROPIC_API_KEY=... OPENAI_API_KEY=... TYPESAFE_API_KEY=...
python3 rca_experiment.py --check --models openai:your-model
python3 rca_experiment.py --models openai:your-model \\
  --trials 5 --forced --html rca_report.html</pre>
<p class="muted">Frozen evidence, fixed check menu, answers kept in separate files the agent never sees.
Few trials per system: read the intervals before the ranks.</p></div>
</section>
</main>
<script>{JS}</script>"""
    if not standalone:
        return body
    head, main = body.split("<main>", 1)
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'{head}</head><body><main>{main}</body></html>\n')


def write(traces, path, note=None, standalone=True):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(traces, note, standalone))
