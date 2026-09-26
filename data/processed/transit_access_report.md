# Transit access sanity report
Build time: 23.8s. Zones: 73. Stations: 239.

## Mode shares (multinomial logit on base times, utility = -0.1 x minutes) vs EMEF 2024
| mode | model | EMEF 2024 |
|---|---|---|
| metro | 29.8% | 17.3% |
| bus | 14.8% | 11.6% |
| car | 24.3% | 16.7% |
| bike | 27.7% | 3.2% |
| walk | 3.3% | 51.3% |

OD-pair weight = population_i x population_j (see build_transit_access.py comment). This has no distance-decay/friction term, so it still overweights long cross-city trips relative to real travel demand (EMEF is dominated by short, often sub-1km trips); walk and low-speed-mode shares are correspondingly understated here and metro/bike/car overstated. This is a sanity report, not a gate (docs/TRANSIT_ACCESS.md).

## Fastest mode by OD pair, before vs after this change (elevation-aware bike/walk)
OD-pair weight = population_i x job_weight_j x (district_j's share of citywide jobs, from districts.json jobs_per_resident x population). Share of all i!=j zone pairs (base variant; metro/bus/car identical before/after) where each mode has the lowest door-to-door time.

| mode | before (flat bike/walk) | after (elevation-aware) |
|---|---|---|
| metro | 5.4% | 4.9% |
| bus | 0.0% | 0.0% |
| car | 45.1% | 90.2% |
| bike | 49.6% | 4.9% |
| walk | 0.0% | 0.0% |

## Top 10 largest uphill bike climb penalties (zone centroid to zone centroid)
Climb penalty = net ascent / 6 m per minute (assumptions.bike_climb_m_per_min); descents get no bonus.

| climb penalty (min) | from (elev) | to (elev) | ascent (m) |
|---|---|---|---|
| 65.0 | la Marina del Prat Vermell (0m) | Vallvidrera, el Tibidabo i les Planes (390m) | 390 |
| 63.4 | la Vila Olímpica del Poblenou (10m) | Vallvidrera, el Tibidabo i les Planes (390m) | 380 |
| 63.2 | Diagonal Mar i el Front Marítim del Poblenou (11m) | Vallvidrera, el Tibidabo i les Planes (390m) | 379 |
| 62.8 | la Barceloneta (13m) | Vallvidrera, el Tibidabo i les Planes (390m) | 377 |
| 62.7 | Provençals del Poblenou (14m) | Vallvidrera, el Tibidabo i les Planes (390m) | 376 |
| 62.7 | el Poblenou (14m) | Vallvidrera, el Tibidabo i les Planes (390m) | 376 |
| 62.5 | el Besòs i el Maresme (15m) | Vallvidrera, el Tibidabo i les Planes (390m) | 375 |
| 62.0 | Sant Pere, Santa Caterina i la Ribera (18m) | Vallvidrera, el Tibidabo i les Planes (390m) | 372 |
| 61.8 | Sant Martí de Provençals (19m) | Vallvidrera, el Tibidabo i les Planes (390m) | 371 |
| 61.5 | la Bordeta (21m) | Vallvidrera, el Tibidabo i les Planes (390m) | 369 |

## Top 20 zone pairs by L9 metro time gain (base - l9_central)
| gain (min) | from | to | base | l9_central |
|---|---|---|---|---|
| 26.0 | Sarrià | Can Baró | 53.8 | 27.8 |
| 25.3 | Sant Gervasi - la Bonanova | Can Baró | 47.8 | 22.5 |
| 24.9 | Pedralbes | Can Baró | 54.9 | 30.0 |
| 24.4 | les Tres Torres | Can Baró | 49.6 | 25.2 |
| 23.9 | Can Baró | Sant Gervasi - la Bonanova | 44.2 | 20.3 |
| 23.7 | Can Baró | Pedralbes | 51.5 | 27.8 |
| 21.6 | Pedralbes | la Salut | 47.5 | 25.9 |
| 21.6 | Pedralbes | Sant Gervasi - la Bonanova | 42.4 | 20.8 |
| 21.5 | Can Baró | Sarrià | 45.3 | 23.8 |
| 21.1 | la Salut | Pedralbes | 47.0 | 25.9 |
| 21.0 | Sarrià | la Salut | 44.7 | 23.7 |
| 19.8 | el Putxet i el Farró | Can Baró | 41.2 | 21.4 |
| 19.1 | les Tres Torres | la Salut | 40.2 | 21.1 |
| 18.9 | Can Baró | Vallvidrera, el Tibidabo i les Planes | 77.7 | 58.8 |
| 18.9 | Can Baró | el Putxet i el Farró | 40.0 | 21.1 |
| 18.4 | Sarrià | el Guinardó | 49.5 | 31.1 |
| 18.3 | Pedralbes | Vallvidrera, el Tibidabo i les Planes | 70.9 | 52.6 |
| 18.1 | Sant Gervasi - la Bonanova | la Salut | 36.5 | 18.4 |
| 18.1 | Sant Gervasi - la Bonanova | el Guinardó | 43.9 | 25.8 |
| 17.9 | la Salut | Sant Gervasi - la Bonanova | 36.3 | 18.4 |

## Population-weighted new_coverage by district (l9_central)
| district | new coverage |
|---|---|
| gracia | 4.2% |
| horta_guinardo | 2.0% |
| les_corts | 2.0% |
| sarria_sant_gervasi | 1.8% |
| ciutat_vella | 0.0% |
| eixample | 0.0% |
| sants_montjuic | 0.0% |
| nou_barris | 0.0% |
| sant_andreu | 0.0% |
| sant_marti | 0.0% |

## Stations per line
| line | stations |
|---|---|
| L1 | 30 |
| L10N | 6 |
| L10S | 11 |
| L11 | 5 |
| L12 | 2 |
| L2 | 18 |
| L3 | 26 |
| L4 | 22 |
| L5 | 27 |
| L6 | 8 |
| L7 | 7 |
| L8 | 11 |
| L9 | 13 |
| L9N | 9 |
| L9S | 15 |
| Rodalies | 30 |
| S1S2 | 32 |
| T1 | 21 |
| T2 | 24 |
| T3 | 20 |
| T4 | 13 |
| T5 | 16 |
| T6 | 14 |

## Stations flagged approx: true (1)
- Prat de la Riba
