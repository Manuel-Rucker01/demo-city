"""Tests for jevcity.engine.snapshot.build_district_snapshots."""

from __future__ import annotations

from jevcity.engine.snapshot import build_district_snapshots
from jevcity.types import Agent, Occupation, Tenure
from jevcity.world.market import init_world


def make_agent(
    id,
    home,
    *,
    tenure=Tenure.RENTER,
    rent_monthly=1000.0,
    wage_monthly=2000.0,
    employed=True,
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
