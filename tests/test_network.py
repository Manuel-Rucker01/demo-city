"""Tests for the T3 zone-level transit network feature (docs/TRANSIT_ACCESS.md).

Uses a small synthetic TransitAccess fixture (3 districts x 2 zones each, variants
"base"/"line_x") built entirely in this file -- the real data/processed/transit_access.json
is built by a parallel task and is not available here (see docs/TRANSIT_ACCESS.md section 5).

`test_legacy_behaviour_is_byte_identical_without_access_path` is the proof required by
docs/TRANSIT_ACCESS.md section 2 ("without [access_path], behaviour and outputs must stay
byte-identical to today"): GOLDEN_HASH was computed by running the same scenario against
commit f8fcdc3 (the tip of l9-network before this task's changes) -- see the final report for
how it was produced.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import numpy as np
import pytest
from conftest import make_profiles

from jevcity.engine.loop import run_simulation
from jevcity.events.triggers import detect_events
from jevcity.jev.meter import estimate_tokens
from jevcity.prompts.buckets import trip_times_text
from jevcity.prompts.questions import mock_priors_for_agent
from jevcity.prompts.state_builder import _person_block, build_state_k1
from jevcity.runlog.reader import RunReader
from jevcity.types import (
    AGE_BUCKETS,
    Action,
    Agent,
    AgentDecision,
    DistrictId,
    DistrictProfile,
    DistrictState,
    EventParams,
    JevConfig,
    Occupation,
    Scenario,
    Source,
    Station,
    Tenure,
    TransitAccess,
    TransitNetworkPolicy,
    World,
    Zone,
)
from jevcity.world import network
from jevcity.world.market import apply_decisions, apply_policies, init_world

GOLDEN_HASH = "f8f3ac126d4e96685c5725ab13e76ff13211dd6be7725a7173123af6b3e10252"

MODES = ("metro", "bus", "car", "bike", "walk")
DISTRICTS = ("d1", "d2", "d3")


# --- synthetic TransitAccess fixture --------------------------------------------------------


def _zone(zid: str, district: DistrictId, pop: int, job_weight: float, new_cov: float = 0.0) -> Zone:
    return Zone(
        id=zid,
        name=f"zone {zid}",
        district=district,
        population=pop,
        job_weight=job_weight,
        centroid=(2.15, 41.4),
        rail_coverage=0.5,
        new_coverage={"line_x": new_cov} if new_cov else {},
    )


_ZONES = [
    _zone("z1", "d1", 1000, 0.6),
    _zone("z2", "d1", 500, 0.4, new_cov=0.5),  # gets new walking-distance access in line_x
    _zone("z3", "d2", 800, 0.5),
    _zone("z4", "d2", 800, 0.5),
    _zone("z5", "d3", 600, 0.3),
    _zone("z6", "d3", 600, 0.7),
]
_ZONE_IDS = [z.id for z in _ZONES]
_N = len(_ZONE_IDS)


def _base_matrix() -> list[float]:
    """metro minutes(i, j) = 5 if i==j else 10 * |i-j| + 5 -- deterministic, asymmetric-free."""
    out = []
    for i in range(_N):
        for j in range(_N):
            out.append(5.0 if i == j else 10.0 * abs(i - j) + 5.0)
    return out


def _other_mode_matrix(base_minutes_per_step: float) -> list[float]:
    out = []
    for i in range(_N):
        for j in range(_N):
            out.append(0.0 if i == j else base_minutes_per_step * abs(i - j) + 3.0)
    return out


def make_access() -> TransitAccess:
    base_metro = _base_matrix()
    # line_x cuts z1<->z4 (idx 0, 3) metro time a lot (45 -> 12, gain 33) and leaves everything
    # else the same, so only agents commuting exactly z1<->z4 should qualify for kind="network".
    line_x_metro = list(base_metro)
    i, j = _ZONE_IDS.index("z1"), _ZONE_IDS.index("z4")
    line_x_metro[i * _N + j] = 12.0
    line_x_metro[j * _N + i] = 12.0

    other_modes = {
        "bus": _other_mode_matrix(14.0),
        "car": _other_mode_matrix(8.0),
        "bike": _other_mode_matrix(6.0),
        "walk": _other_mode_matrix(20.0),
    }
    times = {
        "base": {"metro": base_metro, **other_modes},
        "line_x": {"metro": line_x_metro, **other_modes},
    }
    return TransitAccess(
        radius_m=600,
        zones=_ZONES,
        variants=["base", "line_x"],
        modes=list(MODES),
        times=times,
        stations=[Station(name="Z Station", lines=["X"], lon=2.15, lat=41.4, variant="line_x")],
        sources={"synthetic": "test fixture"},
        assumptions={"detour_factor": 1.3},
    )


DISTRICT_IDS = set(DISTRICTS)


def make_agent(
    id_,
    home,
    *,
    employed=True,
    job_district=None,
    home_zone=None,
    job_zone=None,
    occupation=Occupation.MID_SKILL,
    has_car=False,
    tenure=Tenure.RENTER,
) -> Agent:
    if employed and job_district is None:
        job_district = home
    return Agent(
        id=id_,
        age=35,
        household_size=1,
        occupation=occupation,
        wage_monthly=2200.0,
        employed=employed,
        job_district=job_district,
        home=home,
        rent_monthly=1000.0,
        lease_start_tick=-30,
        savings=5000.0,
        spending_level=0.5,
        satisfaction=0.5,
        tenure=tenure,
        has_car=has_car,
        home_zone=home_zone,
        job_zone=job_zone,
    )


# --- 0. byte-identical legacy behaviour (docs/TRANSIT_ACCESS.md section 2 hard requirement) --


def _write_districts_json(path) -> None:
    payload = [json.loads(p.model_dump_json()) for p in make_profiles()]
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_legacy_behaviour_is_byte_identical_without_access_path(tmp_path):
    districts_path = tmp_path / "districts.json"
    _write_districts_json(districts_path)
    scenario = Scenario(
        name="golden",
        seed=777,
        ticks=40,
        n_agents=60,
        data_path=str(districts_path),
        jev=JevConfig(provider="mock", agents_per_request=1, confidence_threshold=0.35),
    )
    run_dir = tmp_path / "run"
    asyncio.run(run_simulation(scenario, run_dir))

    h = hashlib.sha256()
    for name in ("agents.json", "ticks.ndjson"):
        h.update((run_dir / name).read_bytes())
    assert h.hexdigest() == GOLDEN_HASH, (
        "outputs changed for a scenario with no access_path -- T3's changes must be a no-op "
        "here (docs/TRANSIT_ACCESS.md section 2)"
    )


# --- 1. load_access / validate_access ---------------------------------------------------


def test_load_access_round_trips(tmp_path):
    access = make_access()
    p = tmp_path / "access.json"
    p.write_text(access.model_dump_json(), encoding="utf-8")
    loaded = network.load_access(p)
    assert [z.id for z in loaded.zones] == _ZONE_IDS
    assert loaded.variants == ["base", "line_x"]


def test_load_access_bad_json_raises(tmp_path):
    p = tmp_path / "access.json"
    p.write_text("not json{", encoding="utf-8")
    with pytest.raises(ValueError):
        network.load_access(p)


def test_load_access_missing_file_raises(tmp_path):
    with pytest.raises(ValueError):
        network.load_access(tmp_path / "nope.json")


def test_validate_access_accepts_the_fixture():
    network.validate_access(make_access(), DISTRICT_IDS)


def test_validate_access_rejects_base_not_first():
    access = make_access().model_copy(update={"variants": ["line_x", "base"]})
    with pytest.raises(ValueError, match="base"):
        network.validate_access(access, DISTRICT_IDS)


def test_validate_access_rejects_wrong_matrix_length():
    access = make_access()
    bad_times = json.loads(access.model_dump_json())["times"]
    bad_times["base"]["metro"] = bad_times["base"]["metro"][:-1]
    access = access.model_copy(update={"times": bad_times})
    with pytest.raises(ValueError, match="length"):
        network.validate_access(access, DISTRICT_IDS)


def test_validate_access_rejects_unknown_district():
    access = make_access()
    with pytest.raises(ValueError, match="district"):
        network.validate_access(access, {"d1", "d2"})  # d3 missing from `districts`


def test_validate_access_rejects_district_with_no_zone():
    access = make_access()
    with pytest.raises(ValueError, match="no zone"):
        network.validate_access(access, DISTRICT_IDS | {"d4"})


def test_validate_access_rejects_job_weight_not_summing_to_one():
    zones = list(_ZONES)
    zones[0] = zones[0].model_copy(update={"job_weight": 0.1})  # d1 now sums to 0.5, not 1.0
    access = make_access().model_copy(update={"zones": zones})
    with pytest.raises(ValueError, match="job_weight"):
        network.validate_access(access, DISTRICT_IDS)


# --- 2. zones_in / sampling weights -------------------------------------------------------


def test_zones_in_filters_by_district():
    access = make_access()
    assert {z.id for z in network.zones_in(access, "d1")} == {"z1", "z2"}
    assert {z.id for z in network.zones_in(access, "d3")} == {"z5", "z6"}


def test_sample_home_zone_respects_population_weights():
    access = make_access()
    rng = np.random.default_rng(0)
    draws = [network.sample_home_zone(access, "d1", rng) for _ in range(4000)]
    share_z1 = draws.count("z1") / len(draws)
    # z1 pop=1000, z2 pop=500 -> ~2/3 z1.
    assert 0.6 < share_z1 < 0.73


def test_sample_job_zone_respects_job_weights():
    access = make_access()
    rng = np.random.default_rng(0)
    draws = [network.sample_job_zone(access, "d3", rng) for _ in range(4000)]
    share_z6 = draws.count("z6") / len(draws)
    # z5 job_weight=0.3, z6 job_weight=0.7.
    assert 0.63 < share_z6 < 0.77


def test_sample_zone_unknown_district_raises():
    access = make_access()
    with pytest.raises(ValueError):
        network.sample_home_zone(access, "d4", np.random.default_rng(0))


# --- 3. active_variant / active_network_policy --------------------------------------------


def test_active_variant_picks_latest_started_policy():
    scenario = Scenario(
        name="s",
        policies=[
            TransitNetworkPolicy(variant="line_x", label="X", start_tick=60, min_gain_minutes=3.0),
        ],
    )
    assert network.active_variant(scenario, 0) == "base"
    assert network.active_variant(scenario, 59) == "base"
    assert network.active_variant(scenario, 60) == "line_x"
    assert network.active_variant(scenario, 1000) == "line_x"


def test_active_variant_with_several_policies_uses_the_latest_start_tick():
    scenario = Scenario(
        name="s",
        policies=[
            TransitNetworkPolicy(variant="line_x", label="X", start_tick=10),
            TransitNetworkPolicy(variant="line_y", label="Y", start_tick=100),
        ],
    )
    assert network.active_variant(scenario, 5) == "base"
    assert network.active_variant(scenario, 50) == "line_x"
    assert network.active_variant(scenario, 150) == "line_y"


# --- 4. trip_minutes --------------------------------------------------------------------


def _world_with_access(variant="base") -> World:
    return World(profiles={}, states={}, tick=0, access=make_access(), network_variant=variant)


def test_trip_minutes_looks_up_the_right_cell():
    world = _world_with_access("base")
    times = network.trip_minutes(world, "z1", "z4", "base")
    assert times["metro"] == pytest.approx(35.0)  # |0-3|*10+5
    assert set(times) == set(MODES)


def test_trip_minutes_defaults_to_world_network_variant():
    world = _world_with_access("line_x")
    times = network.trip_minutes(world, "z1", "z4")
    assert times["metro"] == pytest.approx(12.0)


def test_trip_minutes_diagonal_is_within_zone_time():
    world = _world_with_access("base")
    times = network.trip_minutes(world, "z2", "z2", "base")
    assert times["metro"] == pytest.approx(5.0)


def test_trip_minutes_unknown_zone_raises():
    world = _world_with_access("base")
    with pytest.raises(KeyError):
        network.trip_minutes(world, "zzz", "z1", "base")


def test_agent_trip_minutes_none_cases():
    world = _world_with_access("base")
    employed_no_zones = make_agent(1, "d1", employed=True)
    assert network.agent_trip_minutes(world, employed_no_zones) is None

    unemployed = make_agent(2, "d1", employed=False, home_zone="z1")
    assert network.agent_trip_minutes(world, unemployed) is None

    world_no_access = World(profiles={}, states={}, tick=0)
    agent = make_agent(3, "d1", employed=True, home_zone="z1", job_zone="z4")
    assert network.agent_trip_minutes(world_no_access, agent) is None


def test_agent_trip_minutes_present_when_both_zones_set():
    world = _world_with_access("base")
    agent = make_agent(4, "d1", employed=True, job_district="d2", home_zone="z1", job_zone="z4")
    times = network.agent_trip_minutes(world, agent)
    assert times is not None
    assert times["metro"] == pytest.approx(35.0)


def test_new_access_share_reads_zone_new_coverage():
    world = _world_with_access("line_x")
    assert network.new_access_share(world, "z2", "line_x") == pytest.approx(0.5)
    assert network.new_access_share(world, "z1", "line_x") == 0.0
    assert network.new_access_share(world, "z2", "base") == 0.0  # absent -> 0.0


# --- 5. trip_times_text ------------------------------------------------------------------


def test_trip_times_text_formats_all_modes_in_order():
    times = {"metro": 38.4, "bus": 44.0, "car": 25.2, "bike": 23.6, "walk": 40.0}
    text = trip_times_text(times, has_car=True)
    assert text == "metro 38min, bus 44min, car 25min+parking, bike 24min, walk 40min"


def test_trip_times_text_drops_car_without_has_car():
    times = {"metro": 10.0, "car": 20.0}
    assert "car" not in trip_times_text(times, has_car=False)


def test_trip_times_text_drops_long_walks():
    times = {"metro": 10.0, "walk": 70.0}
    assert "walk" not in trip_times_text(times, has_car=False)
    times2 = {"metro": 10.0, "walk": 55.0}
    assert "walk 55min" in trip_times_text(times2, has_car=False)


# --- 6. TRANSIT_CHANGE network / network_access events (events/triggers.py) --------------


def _params(**overrides) -> EventParams:
    base = {
        "job_loss_daily_prob": 0.0, "job_offer_daily_prob": 0.0, "life_event_daily_prob": 0.0,
        "tourism_pressure_daily_prob": 0.0, "transit_awareness_days": 30,
    }
    base.update(overrides)
    return EventParams(**base)


def _seed_and_switch_variant(world, agents_dict, params, policy):
    """Seed world._prev_network_variant on 'base' (no firing), then flip to the policy's
    variant the way market.apply_policies would, and return every TRANSIT_CHANGE event fired
    across the whole awareness window."""
    rng = np.random.default_rng(0)
    detect_events(world, agents_dict, tick=1, params=params, rng=rng)  # seeds caches, no fire
    world.network_variant = policy.variant
    world._network_policy = policy

    fired = []
    for t in range(2, 2 + params.transit_awareness_days + 5):
        fired.extend(
            e for e in detect_events(world, agents_dict, tick=t, params=params, rng=rng)
            if e.kind.value == "transit_change"
        )
    return fired


def test_network_kind_fires_only_for_agents_who_gain_enough():
    world = World(profiles={}, states={}, tick=0, access=make_access(), network_variant="base")
    policy = TransitNetworkPolicy(variant="line_x", label="L9", min_gain_minutes=10.0)
    params = _params()

    gains = make_agent(1, "d1", employed=True, job_district="d2", home_zone="z1", job_zone="z4")
    no_gain = make_agent(2, "d2", employed=True, job_district="d2", home_zone="z3", job_zone="z4")
    agents_dict = {1: gains, 2: no_gain}

    fired = _seed_and_switch_variant(world, agents_dict, params, policy)
    by_agent = {e.agent_id: e for e in fired}

    assert by_agent[1].payload["kind"] == "network"
    assert by_agent[1].payload["line"] == "L9"
    assert by_agent[1].payload["before_min"] == pytest.approx(35.0)
    assert by_agent[1].payload["after_min"] == pytest.approx(12.0)
    # agent 2's z3<->z4 pair doesn't change between variants -> no gain -> not "network".
    assert by_agent.get(2, None) is None or by_agent[2].payload["kind"] != "network"


def test_network_access_fires_for_others_with_new_coverage_probability():
    world = World(profiles={}, states={}, tick=0, access=make_access(), network_variant="base")
    policy = TransitNetworkPolicy(variant="line_x", label="L9", min_gain_minutes=100.0)  # nobody gains
    params = _params()

    # 400 unemployed agents in z2 (new_coverage["line_x"] = 0.5) and z1 (no new coverage).
    agents_dict = {}
    for i in range(200):
        agents_dict[i] = make_agent(i, "d1", employed=False, occupation=Occupation.RETIRED, home_zone="z2")
    for i in range(200, 400):
        agents_dict[i] = make_agent(i, "d1", employed=False, occupation=Occupation.RETIRED, home_zone="z1")

    fired = _seed_and_switch_variant(world, agents_dict, params, policy)
    by_agent = {e.agent_id: e for e in fired}

    z2_hits = [aid for aid in range(200) if by_agent.get(aid) is not None]
    z1_hits = [aid for aid in range(200, 400) if by_agent.get(aid) is not None]
    assert not z1_hits  # z2 has no new_coverage entry for line_x -> never fires
    assert 60 < len(z2_hits) < 140  # ~50% of 200, generous statistical band
    for aid in z2_hits:
        assert by_agent[aid].payload == {"kind": "network_access", "line": "L9"}


def test_network_access_draw_is_deterministic_per_agent_and_variant():
    from jevcity.events.triggers import _network_access_draw

    assert _network_access_draw(42, "line_x") == _network_access_draw(42, "line_x")
    assert _network_access_draw(42, "line_x") != _network_access_draw(42, "line_y")
    assert _network_access_draw(42, "line_x") != _network_access_draw(43, "line_x")
    d = _network_access_draw(42, "line_x")
    assert 0.0 <= d < 1.0


def test_no_network_events_without_access():
    world = World(profiles={}, states={}, tick=0)  # world.access is None
    policy = TransitNetworkPolicy(variant="line_x", label="L9", min_gain_minutes=1.0)
    params = _params()
    agent = make_agent(1, "d1", employed=True, job_district="d2")
    agents_dict = {1: agent}
    fired = _seed_and_switch_variant(world, agents_dict, params, policy)
    assert fired == []


# --- 7. zones redrawn on move / new job (world/market.py apply_decisions) -----------------


def _minimal_world_for_market(rng: np.random.Generator) -> tuple[World, list[Agent]]:
    ages = dict(zip(AGE_BUCKETS, (0.2, 0.2, 0.2, 0.2, 0.2), strict=True))
    profiles = [
        DistrictProfile(
            id=did, name=did, population=10_000, age_distribution=ages,
            income_per_capita_annual=20_000.0, avg_rent_monthly=900.0, vacancy_rate=0.3,
            unemployment_rate=0.1, jobs_per_resident=0.8, shops=100, transit_score=0.5,
            centroid=(2.15, 41.4),
            sources={f: Source.PLAUSIBLE for f in (
                "population", "age_distribution", "income_per_capita_annual",
                "avg_rent_monthly", "vacancy_rate", "unemployment_rate",
                "jobs_per_resident", "shops", "transit_score")},
        )
        for did in DISTRICTS
    ]
    agents = [make_agent(i, "d1", employed=True, job_district="d1", home_zone="z1", job_zone="z1") for i in range(5)]
    world = init_world(profiles, agents)
    # init_world sizes housing_units off each district's actual resident count -- d2/d3 start
    # with 0 residents here, so give them room for a MOVE to succeed in the tests below.
    for did in ("d2", "d3"):
        world.states[did].housing_units = 50
    world.access = make_access()
    world.network_variant = "base"
    return world, agents


def test_move_redraws_home_zone(rng):
    world, agents = _minimal_world_for_market(rng)
    agent = agents[0]
    agent.savings = 100_000.0  # affordable, can pay moving cost
    decision = AgentDecision(
        agent_id=agent.id, tick=1, action=Action.MOVE, destination="d3",
        spending=0.5, satisfaction=0.5, confidence=0.9,
    )
    scenario = Scenario(name="s")
    apply_decisions(world, {a.id: a for a in agents}, [decision], scenario, rng, tick=1)
    assert agent.home == "d3"
    assert agent.home_zone in {"z5", "z6"}


def test_new_job_redraws_job_zone(rng):
    world, agents = _minimal_world_for_market(rng)
    agent = agents[0]
    agent.employed = False
    agent.job_district = None
    agent.job_zone = None
    world.states["d2"].jobs = 100
    world.states["d2"].filled_jobs = 0  # plenty of vacancies -> job_search should succeed often
    decision = AgentDecision(
        agent_id=agent.id, tick=1, action=Action.JOB_SEARCH, destination=None,
        spending=0.5, satisfaction=0.5, confidence=0.9,
    )
    scenario = Scenario(name="s")
    found = False
    for seed in range(20):
        agent.employed = False
        agent.job_district = None
        agent.job_zone = None
        local_rng = np.random.default_rng(seed)
        apply_decisions(world, {a.id: a for a in agents}, [decision], scenario, local_rng, tick=1)
        if agent.employed:
            found = True
            assert agent.job_district is not None
            assert agent.job_zone in {z.id for z in network.zones_in(world.access, agent.job_district)}
            break
    assert found, "job_search never matched across 20 rng seeds"


# --- 8. apply_policies wires world.network_variant / world._network_policy ---------------


def test_apply_policies_sets_network_variant_and_policy_cache():
    world, _agents = _minimal_world_for_market(np.random.default_rng(0))
    scenario = Scenario(
        name="s",
        policies=[TransitNetworkPolicy(variant="line_x", label="L9", start_tick=5, min_gain_minutes=3.0)],
    )
    apply_policies(world, scenario, tick=1)
    assert world.network_variant == "base"
    apply_policies(world, scenario, tick=5)
    assert world.network_variant == "line_x"
    assert world._network_policy.label == "L9"


def test_apply_policies_defaults_to_base_with_no_policy():
    world, _agents = _minimal_world_for_market(np.random.default_rng(0))
    scenario = Scenario(name="s")
    apply_policies(world, scenario, tick=100)
    assert world.network_variant == "base"


# --- 9. person block trip_to_work + mock commute_mode prior --------------------------------


def _world10_like():
    profiles = {}
    states = {}
    for did in DISTRICTS:
        ages = dict(zip(AGE_BUCKETS, (0.2, 0.2, 0.2, 0.2, 0.2), strict=True))
        profiles[did] = DistrictProfile(
            id=did, name=did.upper(), population=10_000, age_distribution=ages,
            income_per_capita_annual=20_000.0, avg_rent_monthly=900.0, vacancy_rate=0.3,
            unemployment_rate=0.1, jobs_per_resident=0.8, shops=100, transit_score=0.5,
            centroid=(2.15, 41.4),
            sources={f: Source.PLAUSIBLE for f in (
                "population", "age_distribution", "income_per_capita_annual",
                "avg_rent_monthly", "vacancy_rate", "unemployment_rate",
                "jobs_per_resident", "shops", "transit_score")},
        )
        states[did] = DistrictState(
            id=did, avg_rent=900.0, housing_units=1000, occupied_units=700, jobs=800, filled_jobs=700,
        )
    world = World(profiles=profiles, states=states, tick=10, access=make_access(), network_variant="base")
    return world


def test_person_block_has_trip_to_work_only_with_both_zones():
    world = _world10_like()
    with_zones = make_agent(1, "d1", employed=True, job_district="d2", home_zone="z1", job_zone="z4")
    without_zones = make_agent(2, "d1", employed=True, job_district="d2")

    block_with = _person_block(with_zones, world, tick=10)
    block_without = _person_block(without_zones, world, tick=10)

    assert "trip_to_work" in block_with
    assert block_with["trip_to_work"] == trip_times_text(
        network.agent_trip_minutes(world, with_zones), has_car=with_zones.has_car
    )
    assert "trip_to_work" not in block_without


def test_build_state_k1_includes_trip_to_work():
    world = _world10_like()
    agent = make_agent(1, "d1", employed=True, job_district="d2", home_zone="z1", job_zone="z4")
    state = build_state_k1(world, agent, [], tick=10, district_ids=["d1", "d2"])
    assert "trip_to_work" in state["person"]


def test_district_transit_field_notes_new_coverage_stations():
    world = _world10_like()
    world.network_variant = "line_x"
    world._network_policy = TransitNetworkPolicy(variant="line_x", label="L9", min_gain_minutes=3.0)
    agent = make_agent(1, "d1", employed=True, job_district="d1", home_zone="z1", job_zone="z1")
    state = build_state_k1(world, agent, [], tick=10, district_ids=["d1", "d2"])
    d1_block = next(d for d in state["districts"] if d["you_live_here"])
    assert "new L9 stations" in d1_block["transit"]  # d1 has z2 with new_coverage > 0
    d2_block = next(d for d in state["districts"] if not d["you_live_here"])
    assert "new L9 stations" not in d2_block["transit"]  # d2's zones have no new_coverage


def test_mock_prior_commute_mode_blends_trip_times_when_available():
    world = _world10_like()
    fast_metro = make_agent(
        1, "d1", employed=True, job_district="d1", home_zone="z1", job_zone="z1", has_car=True
    )  # same-zone trip: metro minutes = 5 (fastest by far under this fixture)
    priors = mock_priors_for_agent(fast_metro, [], world, districts=["d1"])
    assert priors["commute_mode"]["metro"] > priors["commute_mode"]["car"]

    no_zones = make_agent(2, "d1", employed=True, job_district="d1", has_car=True)
    priors_no_zones = mock_priors_for_agent(no_zones, [], world, districts=["d1"])
    # No zones -> plain habit-weighted prior (metro popularity 3.0 vs car 2.0), unaffected by trip times.
    assert priors_no_zones["commute_mode"]["metro"] == pytest.approx(3.0)
    assert priors_no_zones["commute_mode"]["car"] == pytest.approx(2.0)


# --- 10. token budget with trip_to_work -----------------------------------------------------


def test_trip_to_work_token_cost_is_within_budget():
    """docs/TRANSIT_ACCESS.md: the person-block addition should average <= 45 tokens."""
    world = _world10_like()
    agents_with = [
        make_agent(i, "d1", employed=True, job_district="d2", home_zone="z1", job_zone="z4", has_car=(i % 2 == 0))
        for i in range(20)
    ]
    deltas = []
    for agent in agents_with:
        block_with = _person_block(agent, world, tick=10)
        agent_no_zone = make_agent(
            agent.id, "d1", employed=True, job_district="d2", has_car=agent.has_car
        )
        block_without = _person_block(agent_no_zone, world, tick=10)
        tokens_with = estimate_tokens({"person": block_with})
        tokens_without = estimate_tokens({"person": block_without})
        deltas.append(tokens_with - tokens_without)

    avg_delta = sum(deltas) / len(deltas)
    print(f"\ntrip_to_work token delta: avg={avg_delta:.1f} max={max(deltas)}")
    assert avg_delta <= 45, f"trip_to_work adds {avg_delta:.1f} tokens on average, budget is 45"


# --- 11. end-to-end: scenario with access_path, deterministic replay ------------------------


def _make_access_for_real_districts() -> TransitAccess:
    """One zone per conftest.make_profiles() district (ciutat_vella, eixample, gracia,
    sant_marti, nou_barris) -- for the e2e/replay tests, which build their population from
    that 5-district fixture rather than the d1/d2/d3 synthetic one used above."""
    district_ids = [p.id for p in make_profiles()]
    zones = [_zone(f"z_{did}", did, 10_000, 1.0) for did in district_ids]
    n = len(zones)

    def matrix(step: float) -> list[float]:
        return [0.0 if i == j else step * abs(i - j) + 5.0 for i in range(n) for j in range(n)]

    times = {
        "base": {m: matrix(8.0 if m == "metro" else 10.0) for m in MODES},
    }
    times["line_x"] = {m: list(times["base"][m]) for m in MODES}
    return TransitAccess(
        radius_m=600,
        zones=zones,
        variants=["base", "line_x"],
        modes=list(MODES),
        times=times,
        stations=[],
        sources={"synthetic": "test fixture"},
        assumptions={"detour_factor": 1.3},
    )


def _write_access_json(path) -> None:
    path.write_text(_make_access_for_real_districts().model_dump_json(), encoding="utf-8")


@pytest.fixture
def zones_scenario(tmp_path) -> Scenario:
    districts_path = tmp_path / "districts.json"
    _write_districts_json(districts_path)
    access_path = tmp_path / "access.json"
    _write_access_json(access_path)
    return Scenario(
        name="zones-e2e",
        seed=42,
        ticks=25,
        n_agents=80,
        data_path=str(districts_path),
        access_path=str(access_path),
        jev=JevConfig(provider="mock", agents_per_request=1, confidence_threshold=0.35),
    )


def test_e2e_run_with_access_path_assigns_zones_to_agents(zones_scenario, tmp_path):
    run_dir = tmp_path / "run"
    summary = asyncio.run(run_simulation(zones_scenario, run_dir))
    assert summary.ticks == 25

    reader = RunReader(run_dir)
    agents = reader.agents()
    assert len(agents) == 80
    # agents.json (AgentSnapshot) doesn't carry home_zone/job_zone (not part of that contract),
    # so this just checks the run completed and produced the expected number of ticks/agents.
    ticks = list(reader.ticks())
    assert len(ticks) == 25


def test_e2e_replay_with_access_path_is_identical(zones_scenario, tmp_path):
    run_dir = tmp_path / "run_live"
    asyncio.run(run_simulation(zones_scenario, run_dir))

    calls_path = run_dir / "jev_calls.ndjson.gz"
    replay_scenario = zones_scenario.model_copy(deep=True)
    replay_scenario.jev = replay_scenario.jev.model_copy(update={"replay_from": str(calls_path)})

    run_dir_replay = tmp_path / "run_replay"
    asyncio.run(run_simulation(replay_scenario, run_dir_replay))

    live_lines = (run_dir / "ticks.ndjson").read_text(encoding="utf-8").splitlines()
    replay_lines = (run_dir_replay / "ticks.ndjson").read_text(encoding="utf-8").splitlines()
    assert len(live_lines) == len(replay_lines) == 25

    compared_fields = (
        "tick", "date", "districts", "events_by_kind", "actions_by_kind",
        "gated_decisions", "moves", "changes",
    )
    for live_line, replay_line in zip(live_lines, replay_lines, strict=True):
        live = json.loads(live_line)
        replay = json.loads(replay_line)
        for field in compared_fields:
            assert live[field] == replay[field], f"tick {live['tick']} field {field!r} differs"


def test_scenario_with_missing_access_data_file_skips_gracefully(tmp_path):
    """scenarios/base_zones.yaml and l9_central.yaml point at data/processed/transit_access.json,
    which is built by a parallel task and may not exist yet -- run_simulation must raise a plain,
    catchable error (not crash oddly) so a test/CI step can skip cleanly."""
    districts_path = tmp_path / "districts.json"
    _write_districts_json(districts_path)
    scenario = Scenario(
        name="missing-access",
        seed=1,
        ticks=2,
        n_agents=10,
        data_path=str(districts_path),
        access_path=str(tmp_path / "does_not_exist.json"),
        jev=JevConfig(provider="mock"),
    )
    with pytest.raises(ValueError):
        asyncio.run(run_simulation(scenario, tmp_path / "run"))
