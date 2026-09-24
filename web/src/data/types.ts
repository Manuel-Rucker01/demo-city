/**
 * TypeScript mirror of src/jevcity/types.py.
 *
 * Field names are snake_case to match the pydantic JSON exactly — do not camelCase these.
 * Only the subset of fields the web app reads is included; keep in sync with CONTRACTS.md.
 *
 * All fields added for the 10-district / tourism / commute / migration expansion are marked
 * optional so older exported runs (5 districts, no tourism/commute/migration data) still
 * typecheck and render without those features.
 */

export type DistrictId = string;

/** All 10 official Barcelona districts, in code order — mirrors BCN_DISTRICTS in types.py. */
export const DISTRICT_IDS = [
  "ciutat_vella",
  "eixample",
  "sants_montjuic",
  "les_corts",
  "sarria_sant_gervasi",
  "gracia",
  "horta_guinardo",
  "nou_barris",
  "sant_andreu",
  "sant_marti",
] as const;

export type Occupation = "student" | "low_skill" | "mid_skill" | "high_skill" | "retired";

export type Source = "opendata" | "derived" | "plausible";

export type Tenure = "renter" | "owner";

export type CommuteMode = "metro" | "bus" | "car" | "bike" | "walk";

export type ShoppingPlace = "local" | "work_district" | "centre" | "online";

export interface DistrictProfile {
  id: DistrictId;
  name: string;
  population: number;
  age_distribution: Record<string, number>;
  income_per_capita_annual: number;
  avg_rent_monthly: number;
  vacancy_rate: number;
  unemployment_rate: number;
  jobs_per_resident: number;
  shops: number;
  transit_score: number;
  centroid: [number, number]; // [lon, lat]
  sources: Record<string, Source>;
  refs?: Record<string, string>;
  tourist_flats?: number | null;
  commute_mode_share?: Record<string, number> | null;
}

export interface Usage {
  requests: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  estimated: boolean;
  retries: number;
  errors: number;
  cache_hits: number;
  /** Newer fields — optional so older exported runs without them still typecheck. */
  cost_source?: string;
  rate_limited?: number;
  models_seen?: Record<string, number>;
}

export interface DistrictSnapshot {
  id: DistrictId;
  avg_rent: number;
  avg_paid_rent: number;
  residents: number;
  vacancy_rate: number;
  unemployment_rate: number;
  jobs: number;
  filled_jobs: number;
  shop_revenue: number;
  avg_satisfaction: number;
  avg_rent_burden: number;
  rent_cap_active: boolean;
  /** Tourism / commerce / mobility fields — optional for backward compatibility. */
  tourist_units?: number;
  shops_open?: number;
  shop_revenue_monthly?: number;
  mode_share?: Partial<Record<CommuteMode, number>>;
  online_share?: number;
  arrivals?: number;
  departures?: number;
}

export interface MoveRecord {
  agent_id: number;
  src: DistrictId;
  dst: DistrictId;
}

export interface AgentChange {
  agent_id: number;
  employed?: boolean | null;
  satisfaction?: number | null;
}

export interface AgentSnapshot {
  id: number;
  age: number;
  occupation: Occupation;
  home: DistrictId;
  employed: boolean;
  job_district: DistrictId | null;
  wage_monthly: number;
  rent_monthly: number;
  satisfaction: number;
  tenure?: Tenure;
  children?: number;
  has_car?: boolean;
  commute_mode?: CommuteMode | null;
}

export interface TickRecord {
  tick: number;
  date: string;
  districts: DistrictSnapshot[];
  events_by_kind: Record<string, number>;
  actions_by_kind: Record<string, number>;
  gated_decisions: number;
  moves: MoveRecord[];
  changes: AgentChange[];
  usage_tick: Usage;
  usage_total: Usage;
  /** New households arriving this tick (new agent ids, not present in agents.json or any earlier tick). */
  arrivals?: AgentSnapshot[];
  /** Agent ids that left Barcelona this tick — no longer rendered / counted after this. */
  departures?: number[];
}

export interface RentCapPolicy {
  type: "rent_cap";
  district: DistrictId;
  start_tick: number;
  cap_monthly?: number | null;
  cap_pct_of_initial?: number | null;
  max_increase_pct?: number | null;
}

export interface TouristFlatPolicy {
  type: "tourist_flat_ban";
  districts: DistrictId[] | "all";
  start_tick: number;
  end_tick: number;
  reduction?: number;
  return_to_rental_share?: number;
}

export interface TransitLinePolicy {
  type: "new_transit_line";
  districts: DistrictId[];
  start_tick: number;
  transit_boost?: number;
}

export interface LowEmissionZonePolicy {
  type: "low_emission_zone";
  districts: DistrictId[];
  start_tick: number;
  car_cost_monthly?: number;
}

export type Policy = RentCapPolicy | TouristFlatPolicy | TransitLinePolicy | LowEmissionZonePolicy;

export interface Scenario {
  name: string;
  description: string;
  seed: number;
  ticks: number;
  n_agents: number;
  data_path: string;
  policies: Policy[];
  [key: string]: unknown;
}

export interface RunMeta {
  run_id: string;
  created_at: string;
  scenario: Scenario;
  profiles: DistrictProfile[];
  start_date: string;
  jev_model: string;
}

export interface RunSummary {
  run_id: string;
  ticks: number;
  usage: Usage;
  total_moves: number;
  final_districts: DistrictSnapshot[];
  wall_time_s: number;
}

export interface RunIndexEntry {
  run_id: string;
  scenario: string;
  description: string;
  ticks: number;
  n_agents: number;
  /** e.g. "mock" | "anthropic" | ... — which Jev provider produced this run's decisions. */
  provider?: string;
}

export type RunIndex = RunIndexEntry[];
