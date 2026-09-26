"""LinkedIn chart: who the L9 central section helps, from the paired Jev runs.

usage: uv run --group data python scripts/chart_l9_effect.py runs/base_zones-or1 runs/l9_central-or1 [--out media/l9_effect.png]

Agents' barris are not in agents.json, so they are rebuilt exactly as the engine builds them (same seed,
same rng sequence: population, then zones, then the commute repair) and checked against agents.json.
An informed commuter's home barri is taken from that initial draw only if they had not moved before
being told (their home district in the call matches); otherwise they are left off the map.
Barri outlines come from the raw Open Data BCN file cached by scripts/fetch_transit_raw.py.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from jevcity.population import generator
from jevcity.scenarios.loader import load_scenario
from jevcity.world import loader as world_loader
from jevcity.world import market, network

RAW_BARRIS = Path("data/raw/transit/barcelonaciutat_barris.json")
COLORS = {"switched": "#d62728", "already": "#1f77b4", "stayed": "#7f7f7f"}


def rebuild_agents(scenario_path: Path):
    scenario = load_scenario(scenario_path)
    rng = np.random.default_rng(scenario.seed)
    profiles = world_loader.load_profiles(scenario.data_path)
    agents = generator.generate_population(profiles, scenario.n_agents, rng)
    world = market.init_world(profiles, agents)
    access = network.load_access(scenario.access_path)
    world.access = access
    for agent in agents:
        agent.home_zone = network.sample_home_zone(access, agent.home, rng)
        if agent.employed and agent.job_district is not None:
            agent.job_zone = network.sample_job_zone(access, agent.job_district, rng)
        network.repair_commute_mode(world, agent)
    return {a.id: a for a in agents}, access, scenario


def polygons(wkt: str) -> list[np.ndarray]:
    rings = re.findall(r"\(\(([^()]+)\)", wkt)
    return [np.array([[float(v) for v in pt.split()] for pt in ring.split(",")]) for ring in rings]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", type=Path)
    ap.add_argument("variant", type=Path)
    ap.add_argument("--out", type=Path, default=Path("media/l9_effect.png"))
    a = ap.parse_args()

    meta = json.loads((a.variant / "meta.json").read_text())
    agents, access, _ = rebuild_agents(Path("scenarios") / f"{meta['scenario']['name']}.yaml")
    snap = {s["id"]: s for s in json.loads((a.variant / "agents.json").read_text())}
    mismatch = [i for i, s in snap.items() if (s["home"], s["job_district"], s["commute_mode"])
                != (agents[i].home, agents[i].job_district, agents[i].commute_mode and agents[i].commute_mode.value)]
    if mismatch:
        raise SystemExit(f"rebuilt population differs from agents.json for {len(mismatch)} agents, e.g. {mismatch[:5]}")

    zones = {z.id: z for z in access.zones}
    names = {p.id: p.name for p in world_loader.load_profiles(meta["scenario"]["data_path"])}
    policy = next(p for p in meta["scenario"]["policies"] if p["type"] == "transit_network")
    label = policy["label"]

    informed = {}
    with gzip.open(a.variant / "jev_calls.ndjson.gz", "rt") as fh:
        calls = [json.loads(line) for line in fh]
    for c in calls:
        for text in c["request"]["state"].get("today") or []:
            aid = int(next(iter(c["request"]["questions"])).split(":")[0])
            if label in text and aid not in informed:
                answer = c["response"]["answers"].get(f"{aid}:commute_mode")
                informed[aid] = {"text": text, "home_name": c["request"]["state"]["person"]["home"],
                                 "usual": (c["request"]["state"]["person"].get("commute") or "").split(",")[0] or None,
                                 "choice": answer["choice"] if answer else None}

    fig, (ax, side) = plt.subplots(1, 2, figsize=(13, 7.2), gridspec_kw={"width_ratios": [2.3, 1]})
    for b in json.loads(RAW_BARRIS.read_text()):
        for ring in polygons(b["geometria_wgs84"]):
            ax.fill(ring[:, 0], ring[:, 1], color="#f2f2f2", ec="#d0d0d0", lw=0.5, zorder=1)
    base = [s for s in access.stations if s.variant == "base"]
    ax.scatter([s.lon for s in base], [s.lat for s in base], s=6, color="#b0b0b0", zorder=2, label="existing rail stations")
    new = [s for s in access.stations if s.variant == policy["variant"]]
    order = ["Zona Universitària", "Campus Nord", "Manuel Girona", "Prat de la Riba", "Sarrià", "Mandri", "El Putxet",
             "Lesseps", "Travessera de Dalt", "Sanllehy", "Guinardó-Hospital de Sant Pau", "Maragall", "Sagrera"]
    by_name = {s.name: s for s in access.stations}

    def find(name):
        for n, s in by_name.items():
            if name.lower().split()[0] in n.lower() and (name.split()[-1].lower() in n.lower()):
                return s
        return None

    path = [s for s in (find(n) for n in order) if s is not None]
    ax.plot([s.lon for s in path], [s.lat for s in path], color="#ff7f0e", lw=4, alpha=0.8, zorder=3, label=f"{label} central section")
    ax.scatter([s.lon for s in new], [s.lat for s in new], s=40, color="#ff7f0e", ec="white", zorder=4)

    counts = {"switched": 0, "already": 0, "stayed": 0}
    for aid, info in informed.items():
        ag = agents[aid]
        if "trip to work" not in info["text"] or names.get(ag.home) != info["home_name"] or not ag.job_zone:
            continue
        kind = "already" if info["usual"] == "metro" else ("switched" if info["choice"] == "metro" else "stayed")
        counts[kind] += 1
        h, j = zones[ag.home_zone].centroid, zones[ag.job_zone].centroid
        ax.annotate("", xy=j, xytext=h, arrowprops={"arrowstyle": "->", "color": COLORS[kind], "lw": 1.8,
                                                    "connectionstyle": "arc3,rad=0.15"}, zorder=5)
        ax.scatter(*h, s=28, color=COLORS[kind], zorder=6)
    for kind, text in (("switched", "switched to metro"), ("already", "already on metro (now faster)"), ("stayed", "kept their mode")):
        ax.plot([], [], color=COLORS[kind], lw=2, label=f"commute that gets faster: {text}")
    ax.set_xlim(2.07, 2.23); ax.set_ylim(41.35, 41.46); ax.set_aspect(1 / np.cos(np.radians(41.4)))
    ax.axis("off"); ax.legend(loc="lower right", fontsize=8, frameon=True)
    ax.set_title(f"Barcelona {label} central section (Zona Universitària - La Sagrera): whose commute changes", fontsize=11)

    trip = [v for v in informed.values() if "trip to work" in v["text"]]
    gains = [int(m[1]) - int(m[0]) for m in (re.findall(r"~(\d+)", v["text"]) for v in trip) if len(m) >= 2]
    not_rail = [v for v in trip if v["usual"] != "metro"]
    switched = [v for v in not_rail if v["choice"] == "metro"]
    n = meta["scenario"]["n_agents"]
    lines = [f"{len(informed)} of {n:,} simulated residents", "are affected by the line", "",
             f"{len(trip)} get a faster trip to work", f"(median {int(np.median(gains))} min saved)", "",
             f"{len(informed) - len(trip)} get a station within", "walking distance of home", "",
             f"{len(switched)} of {len(not_rail)} commuters not on rail", "switch to metro (" + ", ".join(
                 f"{k} from {({'walk': 'walking', 'bike': 'cycling'}).get(m, 'the ' + str(m))}" for m, k in sorted(Counter(v["usual"] for v in switched).items(), key=lambda x: -x[1])) + ")", "",
             "Moves and rents: no difference", "within one year"]
    side.axis("off")
    side.text(0.02, 0.98, "\n".join(lines), va="top", fontsize=12, family="DejaVu Sans")
    side.text(0.02, 0.02, "Agent-based simulation, 1,000 households, 1 year, two paired runs\n(same seed, with vs without the line); "
              "decisions by TypeSafe's Jev\n(jev-1.13, via OpenRouter). Real stations, real rail network (OSM),\n"
              "real barri populations; jobs by barri use a proxy. Agents take the news\nat face value, so switching is an upper bound. Illustrative, not a forecast.", fontsize=7.5, color="#555555", va="bottom")
    fig.tight_layout()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out, dpi=150)
    print(f"wrote {a.out}; on map: {counts}")


if __name__ == "__main__":
    main()
