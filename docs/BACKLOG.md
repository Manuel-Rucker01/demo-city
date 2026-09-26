# Backlog

Ideas and findings to act on later. Newest first.

## 2026-09-25: from the local-model benchmark (`~/Workspace/projects/local-models/bench`)

- **Non-housing cost of living in the agent state.** The state only shows income, housing cost
  (`rent_burden`) and savings, with nothing about food, bills or transport. Add one line such as
  `"essentials": "food, bills and transport take ~X% of income"`, where X comes from INE's
  Encuesta de Presupuestos Familiares (Catalonia) by income bracket. This costs a few tokens per
  call. Check that spending and move decisions stay sensible, since Jev already cuts spending hard
  when housing takes more than 50% of income.
- **Reword ambiguous event texts** (`prompts/state_builder.py`).
  - `"Today is payday."` → `"Your monthly salary arrived today, as it does every month."`
  - `"You just moved into Barcelona."` → `"You arrived in Barcelona recently and have just settled into your current home."`
  - `"You've moved in with a partner."` → `"Your partner has come to live with you in your current home."`
  - Measured on Jev (206 real calls): P(move) for new arrivals fell from 26% to 5%, and on payday
    "stay" rose from 71% to 86%. Small models misread "moved" as "move" and "payday" as "spend".
  - This changes results compared with the published runs, so re-run base and metro before
    publishing anything new.
- **Move rate vs reality.** The Jev base runs move about 9.7% of residents a year within the city.
  Barcelona's padró gives about 6–7% (Open Data BCN `est-demo-taxa-migracio-interna`, 2021).
  Consider calibrating, and add this comparison to the README limitations.
- **Local provider.** A `local` provider pointing at a TypeSafe-compatible server on localhost
  needs only a `config/providers.yaml` entry, because the wire format is `systemone`. Use it for
  free dev and large test runs. Candidate models and results are in the benchmark report.
