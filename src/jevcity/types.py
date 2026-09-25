"""Shared contract between every jevcity module.

Everything that crosses a module boundary or gets written to disk is defined here.
Modules own their internals; they only exchange these types.

Units: money in EUR, rents and incomes per month, 1 tick = 1 day, 30 ticks = 1 month.
Sim scale: each agent represents `District.population * share / n_agents` real people;
housing units and jobs in `DistrictState` are in *agent units* (1 unit = 1 agent household slot).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# --- Jev facts (docs.typesafe.ai, reviewed 2026-09-24) -------------------------

# Provider URLs, limits and prices live in config/providers.yaml, not here.
JEV_MODEL_ID = "jev-1.13.0"  # TypeSafe pinned id (not jev-latest) so runs are reproducible
JEV_MAX_CHOICE_OPTIONS = 255
JEV_MAX_SCORE_LEVELS = 10

TICKS_PER_MONTH = 30
SIM_START_DATE = "2026-01-01"

DistrictId = str  # slug, e.g. "gracia", "eixample", "sant_marti", "nou_barris", "ciutat_vella"

# All 10 Barcelona districts (official codes 1..10), in code order.
BCN_DISTRICTS: tuple[DistrictId, ...] = (
    "ciutat_vella", "eixample", "sants_montjuic", "les_corts", "sarria_sant_gervasi",
    "gracia", "horta_guinardo", "nou_barris", "sant_andreu", "sant_marti",
)
LEAVE_CITY = "leave_city"  # extra `destination` option: the household leaves Barcelona


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
    # Optional realism fields (None = unknown -> modules fall back to defaults). When set, they
    # must also have a `sources` entry.
    income_per_household_annual: float | None = None  # EUR, gross or disposable (see refs)
    owner_share: float | None = None  # share of households owning their home, 0..1
    avg_household_size: float | None = None  # persons per household
    area_km2: float | None = None
    tourist_flats: int | None = None  # licensed tourist-use dwellings (HUT), real count
    car_ownership: float | None = None  # share of households with at least one car
    commute_mode_share: dict[str, float] | None = None  # CommuteMode value -> share, sums to 1
    households_with_children_share: float | None = None  # share of households with minors


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
    # Tourism: dwellings (agent units) used as tourist flats; not available to residents.
    tourist_units: int = 0
    # Local commerce (real-count scale, not agent units): open shops and last month's revenue.
    shops_open: int = 0
    shop_revenue_monthly: float = 0.0
    shop_revenue_baseline: float = 0.0  # first full month, for open/close dynamics
    # Transport policies in effect.
    transit_boost: float = 0.0  # added to profile.transit_score (clipped to 1)
    low_emission_zone: bool = False
    car_cost_extra_monthly: float = 0.0  # extra monthly cost for car commuters (LEZ charge)

    @property
    def vacant_units(self) -> int:
        """Dwellings available to residents: tourist flats are part of housing_units but are
        neither occupied by agents nor available to them."""
        return max(self.housing_units - self.occupied_units - self.tourist_units, 0)

    @property
    def vacancy_rate(self) -> float:
        """Share of the residential stock (excluding tourist flats) that is empty."""
        residential = self.housing_units - self.tourist_units
        return self.vacant_units / residential if residential > 0 else 0.0

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


class Tenure(StrEnum):
    RENTER = "renter"
    OWNER = "owner"


class CommuteMode(StrEnum):
    METRO = "metro"  # metro, tram, FGC, Rodalies
    BUS = "bus"
    CAR = "car"  # car or motorbike
    BIKE = "bike"
    WALK = "walk"


class ShoppingPlace(StrEnum):
    LOCAL = "local"  # shops in their own district
    WORK_DISTRICT = "work_district"  # near their job
    CENTRE = "centre"  # Ciutat Vella / Eixample shopping streets
    ONLINE = "online"


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
    tenure: Tenure = Tenure.RENTER  # owners: rent_monthly holds their housing cost
    #                                 (mortgage/fees), fixed; no lease renewals
    children: int = 0  # minors in the household
    has_car: bool = False
    commute_mode: CommuteMode | None = None  # None if not commuting (unemployed, retired)
    shopping_place: ShoppingPlace = ShoppingPlace.LOCAL
    active: bool = True  # False once the household has left Barcelona
    commute_since_tick: int | None = None  # when the current commute mode started (habit);
    #                                        negative = before the simulation started
    arrived_tick: int | None = None  # set for households that moved into the city mid-run

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
    SCHOOL_YEAR = "school_year"  # September, households with children
    TRANSIT_CHANGE = "transit_change"  # new line / LEZ affecting home or job district; payload: kind
    SHOP_CLOSED = "shop_closed"  # local shops closing in home district; payload: closed_pct
    TOURISM_PRESSURE = "tourism_pressure"  # tourist flats rising nearby; payload: tourist_share
    ARRIVED = "arrived"  # new household settled in the city this tick


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


QUESTION_NAMES = (
    "action", "destination", "spending", "satisfaction", "commute_mode", "shopping_place",
)
# action/destination/spending/satisfaction are always asked; commute_mode and shopping_place
# are CONDITIONAL (only when an event makes them relevant) to save tokens -- parse tolerates
# their absence. Up to 6 questions per agent -> K <= 5 under the 32-question provider limit.
# Token budget (enforced by tests in prompts): average <= 1,400 real tokens per K=1 request,
# max <= 1,900 (real tokens ~= len(canonical json) / 2.3).
TOKEN_BUDGET_AVG = 1400
TOKEN_BUDGET_MAX = 1900


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
    cost_usd: float | None = None  # cost reported by the provider (OpenRouter usage.cost,
    #                                Vercel providerMetadata.gateway.cost); None = not reported


class JevResponse(BaseModel):
    """Canonical response, always in TypeSafe shape (docs.typesafe.ai/api#response-body):
    answer types are "choice" | "score" | "noul". Provider codecs translate into this
    (e.g. Vercel native {"type":"boolean","probability":p} -> {"type":"noul","noul":p})."""

    model: str  # model string exactly as the provider reported it (log it: jev-latest moves)
    answers: dict[str, dict[str, Any]]  # question key -> answer object
    usage: JevUsage
    provider: str = "typesafe"
    meta: dict[str, Any] = Field(default_factory=dict)  # provider extras: id, routing, generationId


class AgentDecision(BaseModel):
    agent_id: int
    tick: int
    action: Action
    destination: DistrictId | None  # only set when action == MOVE; may be LEAVE_CITY
    spending: float  # 0..1
    satisfaction: float  # 0..1
    confidence: float  # confidence of the action answer
    gated: bool = False  # True if low confidence forced action to STAY
    action_probs: dict[str, float] = Field(default_factory=dict)
    commute_mode: CommuteMode | None = None  # None = not asked / keep current
    shopping_place: ShoppingPlace | None = None


@dataclass
class TickDelta:
    """What apply_decisions / daily_update changed this tick (consumed by engine for logging)."""

    moves: list[MoveRecord]
    changes: list[AgentChange]
    failed_moves: int = 0  # MOVE decisions that found no vacancy / could not afford
    job_matches: int = 0
    arrivals: list[Agent] = field(default_factory=list)  # households that moved into BCN
    departures: list[int] = field(default_factory=list)  # agent ids that left BCN


# --- Jev adapter ---------------------------------------------------------------------------

ProviderName = Literal["mock", "typesafe", "openrouter", "vercel", "local"]
WireFormat = Literal["systemone", "vercel_evaluate", "openrouter_decisions"]
BatchingMode = Literal["quality", "throughput"]
CostSource = Literal["none", "computed", "reported", "estimated", "mixed"]


class ProviderSettings(BaseModel):
    """One entry of config/providers.yaml. Limits are configuration, never code constants."""

    base_url: str
    path: str  # appended to base_url, e.g. "/v1/systemone"
    wire: WireFormat  # request/response format spoken at that path
    api_key_env: str | None  # None for mock
    default_model: str
    rpm_limit: int | None = None  # None = no client-side limit (still adaptive on 429)
    tps_limit: int | None = None  # input tokens per second
    max_context_tokens: int = 32_000  # state + all questions
    max_state_plus_question_tokens: int | None = None
    max_questions_per_request: int | None = None  # None = unknown/unlimited
    price_per_mtok_usd: float | None = None  # used only when the provider does not report cost
    docs: list[str] = Field(default_factory=list)
    todo: list[str] = Field(default_factory=list)  # undocumented behaviour, verify before real use


class JevConfig(BaseModel):
    provider: ProviderName = "mock"  # env JEV_PROVIDER overrides this
    model: str | None = None  # None -> provider default_model
    wire: WireFormat | None = None  # None -> provider default wire
    providers_file: str = "config/providers.yaml"
    overrides: dict[str, Any] = Field(default_factory=dict)  # ProviderSettings field overrides
    mock_as: ProviderName = "typesafe"  # mock estimates cost/limits as if it were this provider
    batching: BatchingMode = "quality"  # quality: K=agents_per_request; throughput: auto K
    agents_per_request: int = 1  # K in quality mode
    max_agents_per_request: int = 64  # cap for auto K in throughput mode
    rate_safety: float = 0.9  # use this fraction of configured limits
    max_concurrency: int = 32
    max_retries: int = 6
    timeout_s: float = 10.0
    decision_policy: Literal["sample", "argmax", "gate"] = "sample"  # how a Choice becomes an action
    confidence_threshold: float = 0.35  # only for decision_policy "gate": below -> STAY
    replay_from: str | None = None  # runs/<id>/jev_calls.ndjson.gz -> answers come from the log
    max_cost_usd: float = 5.0  # hard stop for paid providers


class Usage(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_source: CostSource = "none"
    estimated: bool = False  # True when tokens are estimated (mock) rather than reported
    retries: int = 0
    rate_limited: int = 0  # 429 responses received
    errors: int = 0
    cache_hits: int = 0  # replayed answers
    models_seen: dict[str, int] = Field(default_factory=dict)  # resolved model -> responses

    def add(self, other: Usage) -> Usage:
        sources = {self.cost_source, other.cost_source} - {"none"}
        models = dict(self.models_seen)
        for m, n in other.models_seen.items():
            models[m] = models.get(m, 0) + n
        return Usage(
            requests=self.requests + other.requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            cost_source=sources.pop() if len(sources) == 1 else ("mixed" if sources else "none"),
            estimated=self.estimated or other.estimated,
            retries=self.retries + other.retries,
            rate_limited=self.rate_limited + other.rate_limited,
            errors=self.errors + other.errors,
            cache_hits=self.cache_hits + other.cache_hits,
            models_seen=models,
        )


class CallRecord(BaseModel):
    """One line of runs/<id>/jev_calls.ndjson.gz. Enough to replay without calling any provider."""

    tick: int
    request_id: str
    cache_key: str  # sha256 of canonical JSON {state, questions} (provider/model independent)
    provider: ProviderName
    replayed: bool = False
    requested_model: str
    resolved_model: str  # == response.model
    request: dict[str, Any]  # canonical TypeSafe-shaped body {state, model, questions}
    wire_body: dict[str, Any] | None = None  # exact body sent when it differs from `request`
    response: JevResponse  # canonical
    latency_ms: float
    attempts: int


class CallSink(Protocol):
    def write_call(self, rec: CallRecord) -> None: ...


class JevBackend(Protocol):
    provider: ProviderName
    settings: ProviderSettings

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


class TouristFlatPolicy(BaseModel):
    """Phase out tourist flats (e.g. Barcelona's plan to revoke ~10,000 HUT licences by 2028):
    between start_tick and end_tick tourist_units shrink linearly to (1 - reduction) of their
    initial level; `return_to_rental_share` of the removed units join the residential stock."""

    type: Literal["tourist_flat_ban"] = "tourist_flat_ban"
    districts: list[DistrictId] | Literal["all"] = "all"
    start_tick: int = 0
    end_tick: int = 365
    reduction: float = 1.0
    return_to_rental_share: float = 1.0


class TransitLinePolicy(BaseModel):
    """New metro/tram line: raises transit in the listed districts from start_tick."""

    type: Literal["new_transit_line"] = "new_transit_line"
    districts: list[DistrictId]
    start_tick: int = 0
    transit_boost: float = 0.2


class LowEmissionZonePolicy(BaseModel):
    """Low-emission zone: car commuters living or working in the listed districts pay extra."""

    type: Literal["low_emission_zone"] = "low_emission_zone"
    districts: list[DistrictId]
    start_tick: int = 0
    car_cost_monthly: float = 60.0


Policy = Annotated[
    RentCapPolicy | TouristFlatPolicy | TransitLinePolicy | LowEmissionZonePolicy,
    Field(discriminator="type"),
]


class MarketParams(BaseModel):
    rent_adjust_interval: int = TICKS_PER_MONTH
    rent_elasticity: float = 0.6  # monthly rent change per unit of excess demand ratio
    max_monthly_rent_change: float = 0.008  # ~10%/yr ceiling
    target_vacancy: float = 0.05
    renewal_increase_cap: float = 0.10  # max renewal increase absent policy
    job_match_daily_prob: float = 0.3  # per job_search decision, scaled by vacancies
    spend_to_jobs: float = 0.00002  # DEPRECATED: superseded by shop dynamics (jobs_per_shop)
    moving_cost: float = 1500.0
    tourism_rent_pressure: float = 0.5  # extra excess-demand per unit of tourist share of stock
    shop_close_threshold: float = 0.85  # monthly revenue / baseline below this -> shops close
    shop_open_threshold: float = 1.10  # above this -> shops open
    shop_monthly_change_max: float = 0.005  # max share of shops opening/closing per month (~6%/yr)
    jobs_per_shop: float = 2.5  # real jobs per shop (converted to agent units)
    car_cost_monthly: float = 250.0  # baseline running cost of a commuting car
    public_transport_monthly: float = 40.0  # T-usual-like monthly pass


class EventParams(BaseModel):
    job_loss_daily_prob: float = 0.00015
    job_offer_daily_prob: float = 0.01
    life_event_daily_prob: float = 0.0008
    rent_burden_threshold: float = 0.40
    lease_length_ticks: int = 360
    tourism_pressure_daily_prob: float = 0.002  # scaled by district tourist share
    school_year_tick: int = 244  # ~1 September
    transit_awareness_days: int = 60  # affected commuters notice a new line/LEZ spread over
    #                                   this many days after it starts (not all on day one)


class MigrationParams(BaseModel):
    """Households arriving in / leaving Barcelona. Arrivals are new agents (new ids)."""

    arrivals_per_month_per_1000: float = 3.0  # new households per 1,000 agents per month
    leave_city_moving_cost: float = 3000.0


class PromptParams(BaseModel):
    max_districts_in_state: int = 5  # home + job + most relevant affordable alternatives


class Scenario(BaseModel):
    name: str
    description: str = ""
    seed: int = 42
    ticks: int = 365
    n_agents: int = 1000
    data_path: str = "data/processed/districts.json"
    districts: list[DistrictId] | None = None  # None = every district in the data file
    jev: JevConfig = Field(default_factory=JevConfig)
    market: MarketParams = Field(default_factory=MarketParams)
    events: EventParams = Field(default_factory=EventParams)
    policies: list[Policy] = Field(default_factory=list)
    migration: MigrationParams = Field(default_factory=MigrationParams)
    prompt: PromptParams = Field(default_factory=PromptParams)


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
    avg_rent_burden: float  # MEDIAN rent/income among renters (name kept for compatibility)
    rent_cap_active: bool
    tourist_units: int = 0
    shops_open: int = 0
    shop_revenue_monthly: float = 0.0
    mode_share: dict[str, float] = Field(default_factory=dict)  # among commuting residents
    online_share: float = 0.0  # residents whose main shopping is online
    arrivals: int = 0  # today
    departures: int = 0  # today


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
    usage_tick: Usage  # usage_tick.models_seen = exact model versions that answered this tick
    usage_total: Usage
    arrivals: list[AgentSnapshot] = Field(default_factory=list)  # new households (new dots)
    departures: list[int] = Field(default_factory=list)  # agent ids that left the city


class RunMeta(BaseModel):
    run_id: str
    created_at: str
    scenario: Scenario
    profiles: list[DistrictProfile]
    start_date: str = SIM_START_DATE
    jev_provider: ProviderName = "mock"
    jev_model_requested: str = JEV_MODEL_ID
    jev_wire: WireFormat = "systemone"


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
    tenure: Tenure = Tenure.RENTER
    children: int = 0
    has_car: bool = False
    commute_mode: CommuteMode | None = None


class RunSummary(BaseModel):
    run_id: str
    ticks: int
    usage: Usage
    total_moves: int
    final_districts: list[DistrictSnapshot]
    wall_time_s: float
