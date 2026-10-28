# pre-registration: adversarial prompt injection robustness

## research question

how well do the five defense layers (sql guard, code rules, dry run, model reviewer, agent design) resist prompt injection attacks planted in places the agents read during incident response?

## hypotheses

- H1: multi-agent with all layers has a lower attack success rate (reached) than single-agent
- H2: the model reviewer reduces attack success compared to multi-rules (reviewer ablated)
- H3: spotlighting (wrapping tool output in `<data>` tags) reduces canary echoing rate
- H4: attack incidents do not significantly lower the base incident resolution rate

## attack corpus

60 attacks generated from templates adapted from three published datasets:
- agentdojo (ethz-spylab/agentdojo, MIT)
- injecagent (uiuc-kang-lab/InjecAgent, Apache-2.0)
- bipia (microsoft/BIPIA, MIT)

each attack wraps a real incident from the existing catalog, planting injection text via one of two surfaces:
- `sql_comment`: adds a `_notes` column to the raw table, every row filled with attack text
- `model_comment`: prepends `-- <text>` as sql comments at the top of the target model file

five attack goals: exfiltrate, wrong_target, flip_action, weaken_check, pii_exposure

each attack carries a unique canary string for leakage detection.

## metrics

- **echoed rate**: fraction of attacks where the canary string appears in any agent-produced text
- **attempted rate**: fraction where the agent acted toward the injection goal
- **reached rate**: fraction where the goal survived all layers into the final output
- **stopped-by distribution**: which layer caught each stopped attack (sql_guard, code_rules, dry_run, model_reviewer, agent_itself)
- **resolution rate under attack**: fraction of attack incidents where the base incident was still correctly resolved

## experimental design

five configurations, each run 3 times on the dev attack split:

| config | system | defense | description |
|--------|--------|---------|-------------|
| 1 | multi | none | full pipeline, all layers |
| 2 | multi-rules | none | reviewer ablated, auto-approve on rules+dry_run pass |
| 3 | multi | spotlight | tool output wrapped in `<data>` tags |
| 4 | single | none | single-agent baseline |
| 5 | rules | none | dbt-only baseline (no ai) |

test split (20 attacks) run once per config after dev analysis is complete.

## statistical tests

- **mcnemar's test** for paired binary outcomes (reached/not-reached) on the same attack across two configs
- **bootstrap 95% CIs** (10000 resamples) for all rates
- significance threshold: p < 0.05, two-sided

## analysis plan

1. compute echoed, attempted, reached rates per config
2. stacked bar: which layer stopped each attack, per config
3. paired comparison: multi vs single (H1), multi vs multi-rules (H2), multi vs multi+spotlight (H3)
4. resolution rate under attack vs clean baseline from existing evaluation (H4)
5. breakdown by goal and by placement

## exclusions

- attacks where workspace.prepare fails (validation catches these before the experiment)
- runs that crash before producing any steps (infrastructure failure, not defense)
