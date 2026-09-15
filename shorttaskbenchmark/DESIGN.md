# Short benchmark design

`short-python-stdlib-v1` is a deterministic, standard-library-only benchmark for short single-file Python tasks. It contains four tasks and four binary official criteria per task (16 official criteria total). The manifest is authoritative for task count, criterion count, A/B cells, waves, resource SLA, capabilities, ranking axes, and freeze exclusions.

## Estimand and conditions

The unit is one model/condition cell, evaluated once in a clean workspace (Pass@1). A cells receive `TASKS.md` and the public smoke tests and must plan their own implementation. B cells receive the same public material plus a separately prepared shared plan. The current shared-plan file is only a public-source draft pending an actual Phi-3/K3 planning run; no B formal run is claimed here.

Each of the ten candidate slots has an A and B cell. The four declared waves are `G1-A`, `G2-B`, `G1-B`, and `G2-A`, with five cells per wave. A candidate may edit only the four solution files and delivery acknowledgement; usage records are collected separately. There is no repair pass in the formal result. Only a proven infrastructure failure before a first valid response may be re-dispatched once into a fresh same-condition slot; interruption, timeout, or candidate failure remains the recorded result.

No network, database, third-party dependency, or existing benchmark is part of a cell. The harness executes each official criterion and capability probe in a separate child process with a fixed timeout, controlled working directory, standard-library-only static import audit, bounded captured output, and no inherited candidate `PYTHONPATH`.

## Scoring

The Instruction Gate checks acknowledgement, required files, public-file and cell metadata integrity, API signatures, standard-library imports, extra files, and reported external writes. A failed gate does not erase raw criterion results, but it sets strict official criterion count and strict task count to zero. A strict task is one task with all four official criteria passing and a passing gate.

`build_report.py` reads only raw evaluation records and manifest ranking axes. It emits separate strict and lenient rankings for A and B, with ties retained when every declared axis is equal. The strict axes are gate, strict task count, official criterion count, extension capability count, resource capability count, and token efficiency. The lenient axes are execution completion, raw criterion count, acceptance coverage, instruction compliance, capability count, and token efficiency. The report also emits B-minus-A deltas per matched model; A and B are never collapsed into a weighted score.

Usage is provider wire data from `usage.json` and JSONL event records. Input, output, cache, total, record count, and availability are preserved. Missing usage makes efficiency fields unavailable rather than silently treating a zero-token run as efficient. Planner usage is reported separately and is currently `not_run`.

## Failure classes and retention

A criterion is `pass`, ordinary `fail`, `timeout`, `runner_error`, or `output_limit`. Raw records, usage, audit details, and task-level aggregates are retained. The candidate workspace is never overwritten by evaluation or report generation. Atomic JSON writes reject an existing destination; preparation rejects an existing cell tree.

## Budgets and freeze rules

The manifest records calibrated soft limits of 500,000 inference tokens, 100,000 fresh input tokens, and 60,000 output tokens per formal cell; the shared planner has a 300,000-token soft limit. There are 28 formal cells across fourteen candidate models and at most three non-counted calibration cells. These are budgets, not claims of consumption. The calibration planner consumed 155,201 total wire tokens.

`freeze_assets.py` hashes static code, task, evaluator, harness, validation, and public-source plan assets with SHA-256. `runs/`, dynamic result files, bytecode caches, temporary files, and the hash inventory are excluded; the named public-source plan is the only explicitly included file under `results/`. Generation and verification are separate and generation refuses overwrite. Any post-freeze scoring-asset change requires a new benchmark version and old/new records must not be mixed.

Reference double-run and targeted-mutant checks validate evaluator sensitivity only. Three non-counted A-condition calibration cells were run with GLM-5.2, GPT-5.6-Luna, and DeepSeek-V4-Flash. Calibration found one over-strict hidden check (`rp_delete_identity`), which was corrected before freeze; the resulting outcomes were 16/16 raw with a gate failure, 16/16 full pass, and 13/16 partial pass, so the suite is not saturated or universally failing. Detailed records are in `results/calibration-summary.json`. Current status is `calibration=completed_non_counted` and `freeze=frozen`; no ten-model formal run or formal ranking has occurred.
