"""Effect of a transit-network variant (e.g. the L9 central section): paired runs, same seed.

usage: uv run --group data python scripts/analyze_network_effect.py runs/base_zones-or1 runs/l9_central-or1 [--out media/l9]

Both runs share the population and are identical until the line opens, so each agent is compared with
itself: who was told about the line, how many minutes their trip saves, and what they answered to
"how will you commute now?" with the line vs. without it (the same agent's later commute answers in
the base run). District aggregates are printed too, but with ~1,000 agents they mostly show noise.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import statistics as st
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def load_calls(run: Path) -> list[dict]:
    with gzip.open(run / "jev_calls.ndjson.gz", "rt") as fh:
        return [json.loads(line) for line in fh]


def agent_of(call: dict) -> int:
    return int(next(iter(call["request"]["questions"])).split(":")[0])


def commute_answers(calls: list[dict], aid: int, from_tick: int) -> list[tuple[int, str, dict]]:
    key = f"{aid}:commute_mode"
    out = []
    for c in calls:
        if c["tick"] >= from_tick and key in c["request"]["questions"]:
            a = c["response"]["answers"][key]
            out.append((c["tick"], a["choice"], a["probabilities"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", type=Path)
    ap.add_argument("variant", type=Path)
    ap.add_argument("--out", type=Path, default=Path("media/l9"))
    a = ap.parse_args()

    cb, cv = load_calls(a.base), load_calls(a.variant)
    meta = json.loads((a.variant / "meta.json").read_text())
    policy = next(p for p in meta["scenario"]["policies"] if p["type"] == "transit_network")
    label, start = policy["label"], policy["start_tick"]

    informed: dict[int, dict] = {}
    for c in cv:
        for text in c["request"]["state"].get("today") or []:
            if label in text and agent_of(c) not in informed:
                p = c["request"]["state"]["person"]
                informed[agent_of(c)] = {"tick": c["tick"], "text": text, "home": p["home"], "work": p["work"],
                                         "usual": (p.get("commute") or "").split(",")[0] or None,
                                         "trip": p.get("trip_to_work")}
    rows = []
    for aid, info in sorted(informed.items()):
        with_line = commute_answers(cv, aid, info["tick"])
        without = commute_answers(cb, aid, info["tick"])
        rows.append({**info, "id": aid,
                     "with": with_line[-1] if with_line else None,
                     "without": without[-1] if without else None,
                     "p_metro_with": st.mean(x[2].get("metro", 0) for x in with_line) if with_line else None,
                     "p_metro_without": st.mean(x[2].get("metro", 0) for x in without) if without else None})

    trip = [r for r in rows if "trip to work" in r["text"]]
    access = [r for r in rows if "walking distance" in r["text"]]
    n_agents = meta["scenario"]["n_agents"]
    told = (
        f"- Line opens on day {start}; {len(rows)} of {n_agents} residents were told about it "
        f"({len(trip)} because their trip to work gets faster, {len(access)} because a new station is "
        f"within walking distance of home)."
    )
    lines = [f"# {label}: paired effect ({a.variant.name} vs {a.base.name})", "", told]
    gains = []
    for r in trip:
        nums = [int(x) for x in re.findall(r"~(\d+)", r["text"])]
        if len(nums) >= 2:
            gains.append(nums[1] - nums[0])
    if gains:
        lines.append(f"- Minutes saved by rail for those commuters: median {st.median(gains):.0f}, "
                     f"range {min(gains)}-{max(gains)}.")
    switch = [r for r in trip if r["usual"] not in (None, "metro") and r["with"] and r["with"][1] == "metro"]
    lines.append(f"- Of the {len([r for r in trip if r['usual'] not in (None, 'metro')])} informed commuters not "
                 f"already on rail, {len(switch)} chose metro afterwards.")
    paired = [r for r in rows if r["p_metro_with"] is not None and r["p_metro_without"] is not None]
    if paired:
        lines.append(f"- Same agents, P(metro) in their commute answers: {100 * st.mean(r['p_metro_without'] for r in paired):.0f}% "
                     f"without the line vs {100 * st.mean(r['p_metro_with'] for r in paired):.0f}% with it "
                     f"({len(paired)} agents asked in both runs).")
    lines += ["", "| id | home | work | usual | event | answer with line | same agent without line |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        fmt = lambda x: f"{x[1]} (metro {x[2].get('metro', 0):.2f})" if x else "-"
        lines.append(f"| {r['id']} | {r['home']} | {r['work'].replace('employed as ', '')} | {r['usual'] or '-'} | "
                     f"{r['text']} | {fmt(r['with'])} | {fmt(r['without'])} |")

    def load_ticks(run: Path) -> list[dict]:
        return [json.loads(line) for line in open(run / "ticks.ndjson")]

    tb, tv = load_ticks(a.base), load_ticks(a.variant)
    lines += ["", "District metro share among commuters, end of run (noisy at this size):", "",
              "| district | without | with |", "|---|---|---|"]
    for d in tb[-1]["districts"]:
        dv = {x["id"]: x for x in tv[-1]["districts"]}[d["id"]]
        lines.append(f"| {d['id']} | {100 * d['mode_share'].get('metro', 0):.1f}% | {100 * dv['mode_share'].get('metro', 0):.1f}% |")
    moves = lambda T: sum(t["moves"] if isinstance(t["moves"], int) else len(t["moves"] or []) for t in T)
    models = sorted(set(tb[-1]["usage_total"]["models_seen"]) | set(tv[-1]["usage_total"]["models_seen"]))
    footer = (
        f"Moves: {moves(tb)} without vs {moves(tv)} with the line. "
        f"Cost: ${tb[-1]['usage_total']['cost_usd']:.2f} + ${tv[-1]['usage_total']['cost_usd']:.2f}. "
        f"Models: {models}."
    )
    lines += ["", footer]

    a.out.parent.mkdir(parents=True, exist_ok=True)
    Path(f"{a.out}_effect.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))

    if paired:
        fig, ax = plt.subplots(figsize=(7, 0.45 * len(paired) + 1.5))
        for i, r in enumerate(paired):
            ax.plot([r["p_metro_without"] * 100, r["p_metro_with"] * 100], [i, i], color="grey", lw=1)
            ax.scatter(r["p_metro_without"] * 100, i, color="#9aa0a6", zorder=3)
            ax.scatter(r["p_metro_with"] * 100, i, color="#d62728", zorder=3)
        ax.set_yticks(range(len(paired)), [f"{r['home'][:18]} -> {r['work'].split(' in ')[-1][:18]} ({r['usual']})" for r in paired], fontsize=8)
        ax.set_xlabel("P(commute by metro), %: grey = same person without the line, red = with it")
        ax.set_title(f"{label}: the same residents with and without the line (Jev)")
        ax.set_xlim(0, 100); ax.grid(axis="x", alpha=0.3)
        fig.tight_layout(); fig.savefig(f"{a.out}_paired.png", dpi=140)


if __name__ == "__main__":
    main()
