"""Tests for jevcity.scenarios.loader."""

from __future__ import annotations

import pytest

from jevcity.scenarios.loader import load_scenario


def test_loads_base_scenario():
    scenario = load_scenario("scenarios/base.yaml")
    assert scenario.name == "base"
    assert scenario.jev.provider == "mock"
    assert scenario.policies == []
    assert scenario.ticks == 365


def test_loads_rent_cap_gracia_scenario_with_extends():
    scenario = load_scenario("scenarios/rent_cap_gracia.yaml")
    assert scenario.name == "rent_cap_gracia"
    # inherited from base.yaml
    assert scenario.jev.provider == "mock"
    assert scenario.ticks == 365
    assert scenario.seed == 42
    # own field
    assert len(scenario.policies) == 1
    policy = scenario.policies[0]
    assert policy.district == "gracia"
    assert policy.start_tick == 30
    assert policy.cap_pct_of_initial == 1.0
    assert policy.max_increase_pct == 0.0


def test_data_path_resolves_relative_to_cwd_or_repo_root():
    scenario = load_scenario("scenarios/base.yaml")
    from pathlib import Path

    assert Path(scenario.data_path).exists()


def test_extends_deep_merges_nested_dict_child_wins(tmp_path):
    base = tmp_path / "base.yaml"
    base.write_text(
        """
name: parent
ticks: 100
jev:
  provider: mock
  agents_per_request: 1
  confidence_threshold: 0.35
market:
  moving_cost: 1000.0
""",
        encoding="utf-8",
    )
    child = tmp_path / "child.yaml"
    child.write_text(
        """
extends: base.yaml
name: child
jev:
  agents_per_request: 4
""",
        encoding="utf-8",
    )

    scenario = load_scenario(child)
    assert scenario.name == "child"  # child wins on scalar
    assert scenario.ticks == 100  # inherited from parent
    assert scenario.jev.agents_per_request == 4  # child wins inside nested dict
    assert scenario.jev.confidence_threshold == 0.35  # inherited sibling key survives merge
    assert scenario.market.moving_cost == 1000.0  # inherited nested model entirely


def test_extends_replaces_lists_wholesale_not_merge(tmp_path):
    base = tmp_path / "base.yaml"
    base.write_text(
        """
name: parent
policies:
  - type: rent_cap
    district: gracia
    cap_pct_of_initial: 1.0
""",
        encoding="utf-8",
    )
    child = tmp_path / "child.yaml"
    child.write_text(
        """
extends: base.yaml
name: child
policies:
  - type: rent_cap
    district: eixample
    cap_pct_of_initial: 0.5
""",
        encoding="utf-8",
    )

    scenario = load_scenario(child)
    assert len(scenario.policies) == 1
    assert scenario.policies[0].district == "eixample"


def test_recursive_extends_chain(tmp_path):
    grandparent = tmp_path / "grandparent.yaml"
    grandparent.write_text("name: gp\nticks: 50\nseed: 1\n", encoding="utf-8")
    parent = tmp_path / "parent.yaml"
    parent.write_text("extends: grandparent.yaml\nname: p\nseed: 2\n", encoding="utf-8")
    child = tmp_path / "child.yaml"
    child.write_text("extends: parent.yaml\nname: c\n", encoding="utf-8")

    scenario = load_scenario(child)
    assert scenario.name == "c"
    assert scenario.ticks == 50  # from grandparent
    assert scenario.seed == 2  # from parent, overriding grandparent


def test_unknown_top_level_key_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: bad\nnonexistent_field: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="nonexistent_field"):
        load_scenario(path)


def test_unknown_nested_jev_key_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
name: bad
jev:
  provider: mock
  bogus_option: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="jev.bogus_option"):
        load_scenario(path)


def test_unknown_nested_market_key_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
name: bad
market:
  moving_cost: 1000.0
  typo_field: 1
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="market.typo_field"):
        load_scenario(path)


def test_unknown_nested_events_key_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
name: bad
events:
  job_loss_daily_prob: 0.001
  made_up: 1
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="events.made_up"):
        load_scenario(path)


def test_unknown_key_detected_after_extends_merge(tmp_path):
    base = tmp_path / "base.yaml"
    base.write_text("name: base\nticks: 10\n", encoding="utf-8")
    child = tmp_path / "child.yaml"
    child.write_text("extends: base.yaml\nname: child\nweird_key: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="weird_key"):
        load_scenario(child)


@pytest.mark.parametrize(
    "path",
    [
        "scenarios/base.yaml",
        "scenarios/rent_cap_gracia.yaml",
        "scenarios/hut_ban_2028.yaml",
        "scenarios/new_metro_line.yaml",
        "scenarios/low_emission_zone.yaml",
        "scenarios/combo_policies.yaml",
    ],
)
def test_every_scenario_yaml_loads_and_validates(path):
    scenario = load_scenario(path)
    assert scenario.name
    assert scenario.ticks > 0
    assert scenario.n_agents > 0
    # `districts: None` (unset, inherited from base.yaml) means "every district in the data
    # file" - this expansion's scenarios shouldn't narrow it.
    assert scenario.districts is None


def test_hut_ban_2028_policy_fields():
    scenario = load_scenario("scenarios/hut_ban_2028.yaml")
    assert len(scenario.policies) == 1
    policy = scenario.policies[0]
    assert policy.type == "tourist_flat_ban"
    assert policy.districts == "all"
    assert policy.start_tick == 30
    assert policy.end_tick == 365
    assert policy.reduction == 1.0


def test_new_metro_line_policy_fields():
    scenario = load_scenario("scenarios/new_metro_line.yaml")
    assert len(scenario.policies) == 1
    policy = scenario.policies[0]
    assert policy.type == "new_transit_line"
    assert set(policy.districts) == {"sant_andreu", "nou_barris", "sants_montjuic"}
    assert policy.start_tick == 60
    assert policy.transit_boost == 0.2


def test_low_emission_zone_policy_fields():
    scenario = load_scenario("scenarios/low_emission_zone.yaml")
    assert len(scenario.policies) == 1
    policy = scenario.policies[0]
    assert policy.type == "low_emission_zone"
    assert set(policy.districts) == {"ciutat_vella", "eixample"}
    assert policy.start_tick == 30
    assert policy.car_cost_monthly == 60.0


def test_combo_policies_has_both_rent_cap_and_hut_ban():
    scenario = load_scenario("scenarios/combo_policies.yaml")
    types_present = {p.type for p in scenario.policies}
    assert types_present == {"rent_cap", "tourist_flat_ban"}
    rent_cap = next(p for p in scenario.policies if p.type == "rent_cap")
    assert rent_cap.district == "gracia"


def test_extends_key_itself_is_not_an_unknown_field(tmp_path):
    base = tmp_path / "base.yaml"
    base.write_text("name: base\n", encoding="utf-8")
    child = tmp_path / "child.yaml"
    child.write_text("extends: base.yaml\nname: child\n", encoding="utf-8")
    # Should not raise about "extends" being unknown.
    scenario = load_scenario(child)
    assert scenario.name == "child"
