"""Shared contract between every jevcity module.

Everything that crosses a module boundary or gets written to disk is defined here.
Modules own their internals; they only exchange these types.

Units: money in EUR, rents and incomes per month, 1 tick = 1 day, 30 ticks = 1 month.
Sim scale: each agent represents `District.population * share / n_agents` real people;
housing units and jobs in `DistrictState` are in *agent units* (1 unit = 1 agent household slot).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# --- Jev facts (from docs.typesafe.ai/models, reviewed 2026-09-24) -------------------------

JEV_MODEL_ID = "jev-1.13.0"  # pinned, not the jev-latest alias, so runs are reproducible
JEV_API_URL = "https://api.typesafe.ai/v1/systemone"
JEV_PRICE_PER_MTOK_USD = 0.042  # input tokens only; output tokens are free
JEV_RATE_LIMIT_RPM = 1200
JEV_RATE_LIMIT_TPS = 250_000
JEV_MAX_CHOICE_OPTIONS = 255
JEV_MAX_SCORE_LEVELS = 10

TICKS_PER_MONTH = 30
SIM_START_DATE = "2026-01-01"

DistrictId = str  # slug, e.g. "gracia", "eixample", "sant_marti", "nou_barris", "ciutat_vella"


# --- World ---------------------------------------------------------------------------------


class Source(StrEnum):
    OPENDATA = "opendata"  # taken from Open Data BCN (dataset id recorded in DistrictProfile.refs)
    DERIVED = "derived"  # computed from opendata fields
    PLAUSIBLE = "plausible"  # hand-set plausible value, NOT real data


AGE_BUCKETS = ("0-17", "18-34", "35-49", "50-64", "65+")


class DistrictProfile(BaseModel):
    """Static district data, loaded from data/processed/districts.json."""

    id: DistrictId
    name: str  # display name with accents, e.g. "Gràcia"
    population: int
    age_distribution: dict[str, float]  # AGE_BUCKETS -> share, sums to 1
    income_per_capita_annual: float  # EUR
    avg_rent_monthly: float  # EUR, typical rental flat
    vacancy_rate: float  # share of housing units empty, 0..1
    unemployment_rate: float  # 0..1
    jobs_per_resident: float  # jobs located in the district / residents (job centrality)
    shops: int  # ground-floor commercial premises
    transit_score: float  # 0..1, subjective transit connectivity
    centroid: tuple[float, float]  # (lon, lat)
    sources: dict[str, Source]  # field name -> provenance, for every numeric field above
    refs: dict[str, str] = Field(default_factory=dict)  # field name -> dataset id / note


class DistrictState(BaseModel):
    """Mutable per-district market state. Units are agent units (see module docstring)."""

    model_config = ConfigDict(validate_assignment=False)

    id: DistrictId
    avg_rent: float  # current market rent for new leases, EUR/month
    housing_units: int
    occupied_units: int
    jobs: int  # job slots located here
    filled_jobs: int
    shop_revenue_daily: float = 0.0  # EUR spent by agents in this district today
    rent_cap: float | None = None  # active cap on new-lease market rent, EUR/month
    max_increase_pct: float | None = None  # active cap on renewal increase, e.g. 0.0 = freeze

    @property
    def vacant_units(self) -> int:
        return self.housing_units - self.occupied_units

    @property
    def vacancy_rate(self) -> float:
        return self.vacant_units / self.housing_units if self.housing_units else 0.0

    @property
    def job_vacancies(self) -> int:
        return max(self.jobs - self.filled_jobs, 0)


@dataclass
class World:
    profiles: dict[DistrictId, DistrictProfile]
    states: dict[DistrictId, DistrictState]
    tick: int = 0
    rent_history: dict[DistrictId, list[float]] | None = None  # avg_rent at each month boundary


# --- Agents --------------------------------------------------------------------------------


class Occupation(StrEnum):
    STUDENT = "student"
    LOW_SKILL = "low_skill"
    MID_SKILL = "mid_skill"
    HIGH_SKILL = "high_skill"
    RETIRED = "retired"


@dataclass(slots=True)
class Agent:
    """One simulated household head. Mutable; the engine owns all mutation."""

    id: int
    age: int
    household_size: int
    occupation: Occupation
    wage_monthly: float  # what they earn when employed (pension for retired)
    employed: bool
    job_district: DistrictId | None
    home: DistrictId
    rent_monthly: float  # their own lease, fixed until renewal
    lease_start_tick: int  # may be negative (lease signed before sim start)
    savings: float
    spending_level: float  # 0..1 discretionary spending propensity
    satisfaction: float  # 0..1
    days_unemployed: int = 0
    last_move_tick: int | None = None

    @property
    def income_monthly(self) -> float:
        if self.employed or self.occupation is Occupation.RETIRED:
            return self.wage_monthly
        return 0.6 * self.wage_monthly if self.days_unemployed < 360 else 450.0

    @property
    def rent_burden(self) -> float:
        inc = self.income_monthly
        return self.rent_monthly / inc if inc > 0 else 9.99


# --- Events --------------------------------------------------------------------------------


class EventKind(StrEnum):
    LEASE_RENEWAL = "lease_renewal"  # annual renewal; payload: new_rent, increase_pct
    JOB_LOSS = "job_loss"
    JOB_OFFER = "job_offer"  # payload: district, wage
    PAYDAY = "payday"  # monthly, staggered by agent id
    RENT_BURDEN = "rent_burden"  # burden crossed scenario threshold; payload: burden
    LIFE_EVENT = "life_event"  # payload: kind in {"new_child","partner","health","inheritance"}


class Event(BaseModel):
    agent_id: int
    kind: EventKind
    payload: dict[str, float | str] = Field(default_factory=dict)


# --- Decisions -----------------------------------------------------------------------------


class Action(StrEnum):
    STAY = "stay"
    MOVE = "move"
    JOB_SEARCH = "job_search"
    SPEND = "spend"
    SAVE = "save"


QUESTION_NAMES = ("action", "destination", "spending", "satisfaction")


def question_key(agent_id: int, name: str) -> str:
    """Key of one agent's question inside DecisionRequest.questions (never sent to the model)."""
    return f"{agent_id}:{name}"


class DecisionRequest(BaseModel):
    """One HTTP call to Jev: one state, many typed questions (1..K agents x QUESTION_NAMES).

    `state` and `questions` are sent verbatim as the API body fields of the same name.
    `mock_priors` is never sent: it tells the MOCK backend how to weight random answers,
    keyed like `questions`, value = option/level -> weight (choice keys, or "0".."n-1" for score).
    """

    request_id: str
    tick: int
    agent_ids: list[int]
    state: dict[str, Any] | list[Any] | str
    questions: dict[str, dict[str, Any]]
    mock_priors: dict[str, dict[str, float]] = Field(default_factory=dict)


class JevUsage(BaseModel):
    input_tokens: int
    output_tokens: int = 0


class JevResponse(BaseModel):
    """Raw API response body (docs.typesafe.ai/api#response-body)."""

    model: str
    answers: dict[str, dict[str, Any]]  # question key -> answer object (type, choice|score|noul, ...)
    usage: JevUsage


class AgentDecision(BaseModel):
    agent_id: int
    tick: int
    action: Action
    destination: DistrictId | None  # only set when action == MOVE
    spending: float  # 0..1
    satisfaction: float  # 0..1
    confidence: float  # confidence of the action answer
    gated: bool = False  # True if low confidence forced action to STAY
    action_probs: dict[str, float] = Field(default_factory=dict)


@dataclass
class TickDelta:
    """What apply_decisions / daily_update changed this tick (consumed by engine for logging)."""

    moves: list[MoveRecord]
    changes: list[AgentChange]
    failed_moves: int = 0  # MOVE decisions that found no vacancy / could not afford
    job_matches: int = 0


# --- Jev adapter ---------------------------------------------------------------------------

JevMode = Literal["mock", "real", "replay"]


class JevConfig(BaseModel):
    mode: JevMode = "mock"
    model: str = JEV_MODEL_ID
    api_key_env: str = "JEV_API_KEY"
    base_url: str = JEV_API_URL
    agents_per_request: int = 1  # K; >1 packs several agents into one state
    max_concurrency: int = 16
    rpm_limit: int = int(JEV_RATE_LIMIT_RPM * 0.9)  # safety margin; docs say limits move
    tps_limit: int = int(JEV_RATE_LIMIT_TPS * 0.9)
    max_retries: int = 5
    timeout_s: float = 10.0
    confidence_threshold: float = 0.35  # action answers below this fall back to STAY
    replay_from: str | None = None  # path to a runs/<id>/jev_calls.ndjson for replay mode
    max_cost_usd: float = 5.0  # hard stop for real mode


class Usage(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    estimated: bool = False  # True when tokens are estimated (mock) rather than reported by API
    retries: int = 0
    errors: int = 0
    cache_hits: int = 0

    def add(self, other: Usage) -> Usage:
        return Usage(
            requests=self.requests + other.requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            estimated=self.estimated or other.estimated,
            retries=self.retries + other.retries,
            errors=self.errors + other.errors,
            cache_hits=self.cache_hits + other.cache_hits,
        )


class CallRecord(BaseModel):
    """One line of runs/<id>/jev_calls.ndjson. Enough to replay without calling Jev."""

    tick: int
    request_id: str
    cache_key: str  # sha256 of canonical JSON {model, state, questions}
    mode: JevMode
    request: dict[str, Any]  # exact body sent: {state, model, questions}
    response: JevResponse
    latency_ms: float
    attempts: int


class CallSink(Protocol):
    def write_call(self, rec: CallRecord) -> None: ...


class JevBackend(Protocol):
    mode: JevMode

    async def evaluate_many(self, reqs: Sequence[DecisionRequest]) -> list[JevResponse]:
        """Evaluate all requests concurrently within rate limits; result order == input order."""
        ...

    def usage(self) -> Usage:
        """Cumulative usage since creation."""
        ...

    async def aclose(self) -> None: ...


# --- Scenarios -----------------------------------------------------------------------------


class RentCapPolicy(BaseModel):
    type: Literal["rent_cap"] = "rent_cap"
    district: DistrictId
    start_tick: int = 0
    cap_monthly: float | None = None  # absolute cap on new-lease rent
    cap_pct_of_initial: float | None = None  # e.g. 1.0 = cap at initial avg rent
    max_increase_pct: float | None = None  # cap on renewal increases, e.g. 0.0 = freeze


Policy = RentCapPolicy  # becomes a discriminated union as policies are added


class MarketParams(BaseModel):
    rent_adjust_interval: int = TICKS_PER_MONTH
    rent_elasticity: float = 0.6  # monthly rent change per unit of excess demand ratio
    max_monthly_rent_change: float = 0.03
    target_vacancy: float = 0.05
    renewal_increase_cap: float = 0.10  # max renewal increase absent policy
    job_match_daily_prob: float = 0.08  # per job_search decision, scaled by vacancies
    spend_to_jobs: float = 0.00002  # new job slots per EUR of monthly shop revenue surplus
    moving_cost: float = 1500.0


class EventParams(BaseModel):
    job_loss_daily_prob: float = 0.0003
    job_offer_daily_prob: float = 0.001
    life_event_daily_prob: float = 0.0008
    rent_burden_threshold: float = 0.40
    lease_length_ticks: int = 360


class Scenario(BaseModel):
    name: str
    description: str = ""
    seed: int = 42
    ticks: int = 365
    n_agents: int = 1000
    data_path: str = "data/processed/districts.json"
    jev: JevConfig = Field(default_factory=JevConfig)
    market: MarketParams = Field(default_factory=MarketParams)
    events: EventParams = Field(default_factory=EventParams)
    policies: list[Policy] = Field(default_factory=list)


# --- Run log -------------------------------------------------------------------------------


class DistrictSnapshot(BaseModel):
    id: DistrictId
    avg_rent: float  # market rent for new leases
    avg_paid_rent: float  # mean rent actually paid by residents
    residents: int
    vacancy_rate: float
    unemployment_rate: float  # among working-age resident agents
    jobs: int
    filled_jobs: int
    shop_revenue: float  # today's
    avg_satisfaction: float
    avg_rent_burden: float
    rent_cap_active: bool


class MoveRecord(BaseModel):
    agent_id: int
    src: DistrictId
    dst: DistrictId


class AgentChange(BaseModel):
    """Per-tick change used by the web view to recolor dots without full agent dumps."""

    agent_id: int
    employed: bool | None = None
    satisfaction: float | None = None


class TickRecord(BaseModel):
    tick: int
    date: str  # ISO date
    districts: list[DistrictSnapshot]
    events_by_kind: dict[str, int]
    actions_by_kind: dict[str, int]
    gated_decisions: int
    moves: list[MoveRecord]
    changes: list[AgentChange]
    usage_tick: Usage
    usage_total: Usage


class RunMeta(BaseModel):
    run_id: str
    created_at: str
    scenario: Scenario
    profiles: list[DistrictProfile]
    start_date: str = SIM_START_DATE
    jev_model: str = JEV_MODEL_ID


class AgentSnapshot(BaseModel):
    """Row of runs/<id>/agents.json (initial population)."""

    id: int
    age: int
    occupation: Occupation
    home: DistrictId
    employed: bool
    job_district: DistrictId | None
    wage_monthly: float
    rent_monthly: float
    satisfaction: float


class RunSummary(BaseModel):
    run_id: str
    ticks: int
    usage: Usage
    total_moves: int
    final_districts: list[DistrictSnapshot]
    wall_time_s: float
