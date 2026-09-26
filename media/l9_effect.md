# L9: paired effect (l9_central-or1 vs base_zones-or1)

- Line opens on day 60; 25 of 1000 residents were told about it (14 because their trip to work gets faster, 11 because a new station is within walking distance of home).
- Minutes saved by rail for those commuters: median 8, range 3-11.
- Of the 10 informed commuters not already on rail, 9 chose metro afterwards.
- Same agents, P(metro) in their commute answers: 50% without the line vs 90% with it (2 agents asked in both runs).

| id | home | work | usual | event | answer with line | same agent without line |
|---|---|---|---|---|---|---|
| 344 | Les Corts | low-skill worker in Les Corts | car | A new L9 station opened within walking distance of your home. | car (metro 0.46) | - |
| 381 | Les Corts | low-skill worker in Sarrià-Sant Gervasi | bike | The new L9 opened: your trip to work by metro now takes ~28 min instead of ~32. | bike (metro 0.21) | - |
| 395 | Sarrià-Sant Gervasi | retired | - | A new L9 station opened within walking distance of your home. | - | - |
| 402 | Sarrià-Sant Gervasi | high-skill worker in Horta-Guinardó | metro | The new L9 opened: your trip to work by metro now takes ~28 min instead of ~39. | metro (metro 1.00) | - |
| 403 | Sarrià-Sant Gervasi | retired | - | A new L9 station opened within walking distance of your home. | - | - |
| 407 | Sarrià-Sant Gervasi | low-skill worker in Horta-Guinardó | bus | The new L9 opened: your trip to work by metro now takes ~54 min instead of ~64. | metro (metro 0.88) | - |
| 410 | Sarrià-Sant Gervasi | high-skill worker in Les Corts | bus | The new L9 opened: your trip to work by metro now takes ~49 min instead of ~59. | metro (metro 0.96) | - |
| 424 | Sarrià-Sant Gervasi | retired | - | A new L9 station opened within walking distance of your home. | - | - |
| 446 | Sarrià-Sant Gervasi | low-skill worker in Les Corts | walk | The new L9 opened: your trip to work by metro now takes ~33 min instead of ~36. | metro (metro 0.56) | - |
| 471 | Sarrià-Sant Gervasi | high-skill worker in Les Corts | bus | The new L9 opened: your trip to work by metro now takes ~24 min instead of ~32. | metro (metro 0.84) | - |
| 477 | Sarrià-Sant Gervasi | low-skill worker in Sarrià-Sant Gervasi | walk | A new L9 station opened within walking distance of your home. | walk (metro 0.12) | - |
| 487 | Gràcia | mid-skill worker in Sants-Montjuïc | metro | A new L9 station opened within walking distance of your home. | metro (metro 0.98) | - |
| 497 | Gràcia | retired | - | A new L9 station opened within walking distance of your home. | - | - |
| 506 | Gràcia | high-skill worker in Sarrià-Sant Gervasi | bus | The new L9 opened: your trip to work by metro now takes ~29 min instead of ~35. | metro (metro 0.94) | - |
| 518 | Gràcia | low-skill worker in Ciutat Vella | car | A new L9 station opened within walking distance of your home. | car (metro 0.35) | - |
| 529 | Gràcia | mid-skill worker in Sarrià-Sant Gervasi | metro | The new L9 opened: your trip to work by metro now takes ~31 min instead of ~36. | metro (metro 1.00) | metro (metro 1.00) |
| 531 | Gràcia | high-skill worker in Gràcia | car | A new L9 station opened within walking distance of your home. | car (metro 0.43) | - |
| 534 | Gràcia | high-skill worker in Sant Martí | bus | The new L9 opened: your trip to work by metro now takes ~25 min instead of ~34. | metro (metro 0.91) | - |
| 549 | Gràcia | retired | - | A new L9 station opened within walking distance of your home. | - | - |
| 612 | Horta-Guinardó | high-skill worker in Sarrià-Sant Gervasi | metro | The new L9 opened: your trip to work by metro now takes ~30 min instead of ~38. | metro (metro 1.00) | - |
| 654 | Horta-Guinardó | mid-skill worker in Horta-Guinardó | walk | A new L9 station opened within walking distance of your home. | walk (metro 0.19) | - |
| 665 | Nou Barris | low-skill worker in Sarrià-Sant Gervasi | bus | The new L9 opened: your trip to work by metro now takes ~36 min instead of ~46. | metro (metro 0.82) | - |
| 824 | Sant Andreu | low-skill worker in Gràcia | car | The new L9 opened: your trip to work by metro now takes ~22 min instead of ~31. | metro (metro 0.99) | car (metro 0.00) |
| 851 | Sant Andreu | low-skill worker in Sarrià-Sant Gervasi | metro | The new L9 opened: your trip to work by metro now takes ~42 min instead of ~48. | metro (metro 1.00) | - |
| 944 | Sant Martí | low-skill worker in Gràcia | bus | The new L9 opened: your trip to work by metro now takes ~29 min instead of ~38. | metro (metro 0.88) | - |

District metro share among commuters, end of run (noisy at this size):

| district | without | with |
|---|---|---|
| ciutat_vella | 38.6% | 34.0% |
| eixample | 50.5% | 49.5% |
| gracia | 26.9% | 34.6% |
| horta_guinardo | 22.7% | 29.3% |
| les_corts | 26.5% | 30.3% |
| nou_barris | 42.2% | 35.4% |
| sant_andreu | 41.3% | 40.6% |
| sant_marti | 30.6% | 35.6% |
| sants_montjuic | 43.0% | 43.0% |
| sarria_sant_gervasi | 22.4% | 30.9% |

Moves: 81 without vs 81 with the line. Cost: $0.73 + $0.72. Models: ['typesafe/jev-1.13-20260917'].
