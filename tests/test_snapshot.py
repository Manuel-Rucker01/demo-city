"""Tests for jevcity.engine.snapshot.build_district_snapshots."""

from __future__ import annotations

from jevcity.engine.snapshot import build_district_snapshots
from jevcity.types import Agent, CommuteMode, Occupation, ShoppingPlace, Tenure
from jevcity.world.market import init_world


def make_agent(
    id,
    home,
    *,
    tenure=Tenure.RENTER,
    rent_monthly=1000.0,
    wage_monthly=2000.0,
    employed=True,
    active=True,
    commute_mode=None,
    shopping_place=ShoppingPlace.LOCAL,
) -> Agent:
    return Agent(
        id=id,
        age=35,
        household_size=2,
        occupation=Occupation.MID_SKILL,
        wage_monthly=wage_monthly,
        employed=employed,
        job_district=home if employed else None,
        home=home,
        rent_monthly=rent_monthly,
        lease_start_tick=-30,
        savings=3000.0,
        spending_level=0.5,
        satisfaction=0.6,
        tenure=tenure,
        active=active,
        commute_mode=commute_mode,
        shopping_place=shopping_place,
    )


def test_avg_paid_rent_and_burden_computed_over_renters_only(profiles):
    did = profiles[0].id
    agents = [
        make_agent(0, did, tenure=Tenure.RENTER, rent_monthly=1000.0, wage_monthly=2500.0),
        make_agent(1, did, tenure=Tenure.RENTER, rent_monthly=1200.0, wage_monthly=2500.0),
        # A cheap owner "rent" (mortgage/fees) that would drag the mean way down and the
        # burden way up if it were mixed in with renters.
        make_agent(2, did, tenure=Tenure.OWNER, rent_monthly=200.0, wage_monthly=2500.0),
    ]
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}

    snapshots = build_district_snapshots(world, agents_dict)
    snap = next(s for s in snapshots if s.id == did)

    assert snap.residents == 3
    assert snap.avg_paid_rent == 1100.0  # mean of the two renters only, not the owner
    expected_burden = ((1000.0 / 2500.0) + (1200.0 / 2500.0)) / 2
    assert abs(snap.avg_rent_burden - expected_burden) < 1e-9


def test_district_with_only_owners_has_zero_avg_paid_rent(profiles):
    did = profiles[0].id
    agents = [make_agent(0, did, tenure=Tenure.OWNER, rent_monthly=200.0)]
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}

    snapshots = build_district_snapshots(world, agents_dict)
    snap = next(s for s in snapshots if s.id == did)
    assert snap.residents == 1
    assert snap.avg_paid_rent == 0.0
    assert snap.avg_rent_burden == 0.0


def test_no_district_state_left_untouched(profiles):
    """DistrictState smoke test: every district still gets a snapshot even with no residents."""
    world = init_world(profiles, [])
    snapshots = build_district_snapshots(world, {})
    assert {s.id for s in snapshots} == {p.id for p in profiles}
    for s in snapshots:
        assert s.residents == 0
        assert s.avg_paid_rent == 0.0
        assert s.avg_rent_burden == 0.0


def test_inactive_agents_excluded_from_every_resident_figure(profiles):
    did = profiles[0].id
    agents = [
        make_agent(0, did, active=True, wage_monthly=2500.0, rent_monthly=1000.0),
        # This agent already left the city; if it leaked in it would drag residents/rent/burden.
        make_agent(1, did, active=False, wage_monthly=100.0, rent_monthly=5000.0),
    ]
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}

    snapshots = build_district_snapshots(world, agents_dict)
    snap = next(s for s in snapshots if s.id == did)

    assert snap.residents == 1
    assert snap.avg_paid_rent == 1000.0


def test_mode_share_only_among_employed_commuting_residents(profiles):
    did = profiles[0].id
    agents = [
        make_agent(0, did, employed=True, commute_mode=CommuteMode.METRO),
        make_agent(1, did, employed=True, commute_mode=CommuteMode.CAR),
        # Employed but no commute_mode set (never asked / not commuting) -> excluded.
        make_agent(2, did, employed=True, commute_mode=None),
        # Unemployed with a stale commute_mode -> excluded (not currently employed).
        make_agent(3, did, employed=False, commute_mode=CommuteMode.WALK),
    ]
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}

    snapshots = build_district_snapshots(world, agents_dict)
    snap = next(s for s in snapshots if s.id == did)

    assert snap.residents == 4  # unemployment/residents still count everyone active
    assert snap.mode_share == {"metro": 0.5, "car": 0.5}


def test_online_share_over_all_active_residents(profiles):
    did = profiles[0].id
    agents = [
        make_agent(0, did, shopping_place=ShoppingPlace.ONLINE),
        make_agent(1, did, shopping_place=ShoppingPlace.LOCAL),
        make_agent(2, did, shopping_place=ShoppingPlace.LOCAL),
        make_agent(3, did, shopping_place=ShoppingPlace.LOCAL),
    ]
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}

    snapshots = build_district_snapshots(world, agents_dict)
    snap = next(s for s in snapshots if s.id == did)
    assert snap.online_share == 0.25


def test_tourist_units_and_shop_fields_passed_through_from_district_state(profiles):
    did = profiles[0].id
    world = init_world(profiles, [])
    world.states[did].tourist_units = 42
    world.states[did].shops_open = 7
    world.states[did].shop_revenue_monthly = 12345.0

    snapshots = build_district_snapshots(world, {})
    snap = next(s for s in snapshots if s.id == did)
    assert snap.tourist_units == 42
    assert snap.shops_open == 7
    assert snap.shop_revenue_monthly == 12345.0


def test_arrivals_and_departures_counted_per_district(profiles):
    did0, did1 = profiles[0].id, profiles[1].id
    agents = [
        make_agent(0, did0),
        make_agent(1, did1),
        make_agent(2, did0, active=False),  # already inactive but still looked up by id
    ]
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}

    snapshots = build_district_snapshots(
        world, agents_dict, arrivals=[0, 1], departures=[2]
    )
    snap0 = next(s for s in snapshots if s.id == did0)
    snap1 = next(s for s in snapshots if s.id == did1)

    assert snap0.arrivals == 1
    assert snap0.departures == 1
    assert snap1.arrivals == 1
    assert snap1.departures == 0


def test_unknown_arrival_or_departure_id_is_ignored(profiles):
    did = profiles[0].id
    world = init_world(profiles, [])
    # Should not raise even though agent id 999 doesn't exist.
    snapshots = build_district_snapshots(world, {}, arrivals=[999], departures=[999])
    snap = next(s for s in snapshots if s.id == did)
    assert snap.arrivals == 0
    assert snap.departures == 0
