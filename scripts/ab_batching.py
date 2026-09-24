"""A/B test: does Jev answer the same when K agents share one state?

Builds realistic decision requests from the base scenario (mock-free: the world only evolves
through daily accounting and events, no decisions are applied), then evaluates the SAME agents
at the same tick twice through the configured provider: once with K=1 and once with K=K_BIG.
Reports agreement of the action choice, total-variation distance between action distributions,
destination agreement, and score differences. Writes raw pairs to runs/ab-batching/.

Usage (real provider, costs real tokens):
    JEV_PROVIDER=vercel uv run python scripts/ab_batching.py --n 200 --k 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path

import numpy as np

from jevcity import jev
from jevcity.events import triggers
from jevcity.population import generator
from jevcity.prompts import parse, state_builder
from jevcity.scenarios.loader import load_scenario
from jevcity.types import JevConfig, question_key
from jevcity.world import loader, market


def collect_requests(n: int, k_big: int, seed: int):
    scenario = load_scenario("scenarios/base.yaml")
    rng = np.random.default_rng(seed)
    profiles = loader.load_profiles(scenario.data_path)
    agents = generator.generate_population(profiles, scenario.n_agents, rng)
    by_id = {a.id: a for a in agents}
    world = market.init_world(profiles, agents)
    small, big, count = [], [], 0
    for tick in range(1, 400):
        world.tick = tick
        market.apply_policies(world, scenario, tick)
        events = triggers.detect_events(world, by_id, tick, scenario.events, rng)
        if events and tick >= 15:  # skip the first days so lease/payday mix is varied
            ids = sorted({e.agent_id for e in events})[: n - count]
            evs = [e for e in events if e.agent_id in set(ids)]
            small += state_builder.build_requests(world, by_id, evs, tick, 1)
            big += state_builder.build_requests(world, by_id, evs, tick, k_big)
            count += len(ids)
        market.daily_update(world, by_id, scenario, tick, rng)
        if count >= n:
            break
    return small, big


def tvd(p: dict, q: dict) -> float:
    keys = set(p) | set(q)
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in keys)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    small, big = collect_requests(args.n, args.k, args.seed)
    cfg = JevConfig(max_cost_usd=2.0)
    provider, _ = jev.resolve_provider(cfg)
    print(f"provider={provider} agents={sum(len(r.agent_ids) for r in small)} "
          f"requests K=1: {len(small)}  K={args.k}: {len(big)}")

    backend = jev.make_backend(cfg)
    try:
        resp_big = await backend.evaluate_many(big)
        print("K big done", backend.usage().model_dump(include={"requests", "input_tokens", "rate_limited"}))
        resp_small = await backend.evaluate_many(small)
        print("K=1 done", backend.usage().model_dump(include={"requests", "input_tokens", "rate_limited"}))
    finally:
        await backend.aclose()

    def answers(reqs, resps):
        out = {}
        for req, resp in zip(reqs, resps, strict=True):
            for aid in req.agent_ids:
                out[aid] = {name: resp.answers.get(question_key(aid, name)) for name in
                            ("action", "destination", "spending", "satisfaction")}
        return out

    a1, ak = answers(small, resp_small), answers(big, resp_big)
    common = sorted(set(a1) & set(ak))
    act_agree = [a1[i]["action"]["choice"] == ak[i]["action"]["choice"] for i in common]
    act_tvd = [tvd(a1[i]["action"]["probabilities"], ak[i]["action"]["probabilities"]) for i in common]
    dst_agree = [a1[i]["destination"]["choice"] == ak[i]["destination"]["choice"] for i in common]
    dst_tvd = [tvd(a1[i]["destination"]["probabilities"], ak[i]["destination"]["probabilities"])
               for i in common]
    sp = [abs(a1[i]["spending"]["score"] - ak[i]["spending"]["score"]) for i in common]
    sa = [abs(a1[i]["satisfaction"]["score"] - ak[i]["satisfaction"]["score"]) for i in common]

    def dist(ans):
        tot: dict[str, float] = {}
        for i in common:
            for k, v in ans[i]["action"]["probabilities"].items():
                tot[k] = tot.get(k, 0.0) + v / len(common)
        return {k: round(v, 3) for k, v in sorted(tot.items())}

    report = {
        "agents": len(common),
        "k_big": args.k,
        "action_argmax_agreement": sum(act_agree) / len(common),
        "action_tvd_mean": statistics.mean(act_tvd),
        "action_tvd_median": statistics.median(act_tvd),
        "destination_argmax_agreement": sum(dst_agree) / len(common),
        "destination_tvd_mean": statistics.mean(dst_tvd),
        "spending_score_absdiff_mean": statistics.mean(sp),
        "satisfaction_score_absdiff_mean": statistics.mean(sa),
        "population_action_dist_k1": dist(a1),
        "population_action_dist_kbig": dist(ak),
    }
    out = Path("runs/ab-batching")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2))
    (out / "pairs.json").write_text(json.dumps({str(i): {"k1": a1[i], "kbig": ak[i]} for i in common}))
    print(json.dumps(report, indent=2))
    # decision policy used by the sim samples from probabilities; population-level agreement
    # (dist) matters more than per-agent argmax agreement
    _ = parse  # keep import for readers: see prompts/parse.py for how answers become actions


if __name__ == "__main__":
    asyncio.run(main())
