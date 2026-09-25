# Transit access sanity report
Build time: 1.4s. Zones: 73. Stations: 239.

## Mode shares (multinomial logit on base times, utility = -0.1 x minutes) vs EMEF 2024
| mode | model | EMEF 2024 |
|---|---|---|
| metro | 23.7% | 17.3% |
| bus | 10.3% | 11.6% |
| car | 19.4% | 16.7% |
| bike | 43.5% | 3.2% |
| walk | 3.1% | 51.3% |

OD-pair weight = population_i x population_j (see build_transit_access.py comment). This has no distance-decay/friction term, so it still overweights long cross-city trips relative to real travel demand (EMEF is dominated by short, often sub-1km trips); walk and low-speed-mode shares are correspondingly understated here and metro/bike/car overstated. This is a sanity report, not a gate (docs/TRANSIT_ACCESS.md).

## Top 20 zone pairs by L9 metro time gain (base - l9_central)
| gain (min) | from | to | base | l9_central |
|---|---|---|---|---|
| 23.2 | Sant Gervasi - la Bonanova | Can Baró | 43.1 | 19.9 |
| 23.2 | Sarrià | Can Baró | 48.5 | 25.3 |
| 22.6 | Can Baró | Sant Gervasi - la Bonanova | 42.7 | 20.1 |
| 22.5 | Pedralbes | Can Baró | 49.9 | 27.4 |
| 22.2 | Can Baró | Pedralbes | 50.0 | 27.8 |
| 21.7 | les Tres Torres | Can Baró | 44.1 | 22.4 |
| 21.5 | Can Baró | Sarrià | 44.6 | 23.1 |
| 20.7 | Pedralbes | la Salut | 45.3 | 24.6 |
| 20.6 | Pedralbes | Sant Gervasi - la Bonanova | 40.9 | 20.3 |
| 19.7 | la Salut | Pedralbes | 45.4 | 25.7 |
| 19.6 | Sarrià | la Salut | 42.2 | 22.6 |
| 18.9 | Can Baró | Vallvidrera, el Tibidabo i les Planes | 63.6 | 44.7 |
| 18.7 | el Putxet i el Farró | Can Baró | 37.5 | 18.8 |
| 18.6 | Pedralbes | Vallvidrera, el Tibidabo i les Planes | 56.4 | 37.8 |
| 18.0 | Can Baró | el Putxet i el Farró | 38.1 | 20.1 |
| 17.9 | les Tres Torres | la Salut | 37.6 | 19.7 |
| 17.6 | Sant Gervasi - la Bonanova | la Salut | 34.8 | 17.2 |
| 16.8 | la Salut | Sant Gervasi - la Bonanova | 34.8 | 18.0 |
| 16.5 | la Salut | Sarrià | 37.5 | 21.0 |
| 16.0 | Sarrià | el Guinardó | 46.7 | 30.7 |

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
