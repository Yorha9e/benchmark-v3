# Closed-loop benchmark v2 design

This directory is a self-contained, pass-at-one benchmark for ten candidate models. Each candidate receives identical bytes and may edit only `src/order_fulfillment/` and `src/delivery_spool/` in its own workspace. Hidden evaluator code, reference implementations, mutation fixtures, and harness code are never copied into candidate workspaces.

## Experimental unit and ordering

The experimental unit is one slot/model pair completing both projects in one workspace. Wave order is reversed relative to the prior prototype: Wave 1 is `subtest_4, subtest_6, subtest_1, subtest_9, subtest_8`; Wave 2 is `subtest_10, subtest_5, subtest_3, subtest_2, subtest_7`. Each executor has a 10,000,000 inference-token soft SLA. Planner/main-agent usage is reported separately and excluded from candidate ranking and SLA calculations.

## Acceptance model

There are ten milestones, five per project, and exactly two binary criteria per milestone. A criterion passes only when every raw unittest case named by that criterion passes. A milestone is strict-successful only when its two criteria pass and InstructionGate passes. AcceptanceCoverage is passed criteria divided by 20.

The mechanical lexicographic axes are:

1. `InstructionGate` descending;
2. `ClosedLoopProjectCount` descending (projects whose five milestones are all strict-successful);
3. `MilestoneStrictCount` descending;
4. `AcceptanceCoverage` descending;
5. `InferenceTokensPerMilestoneStrictSuccess` ascending.

Rows with identical values on all five axes retain the same rank. There is no weighted or subjective quality score. Missing/invalid token evidence is `indeterminate`, not infinity or zero, and blocks finalist publication when all prior axes leave that row in the top set.

## Integrity and isolation

`freeze_assets.py` hashes the benchmark-controlled inputs. `prepare_runs.py` refuses overwrites and records complete before trees. Evaluation records complete before/after trees and atomically publishes result files. Child Python environments remove `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, and `PYTHONUSERBASE`, then set `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, and `PYTHONHASHSEED=0`; only an explicit evaluator/workspace path is added back.

Instruction audit checks frozen and non-source assets, the two allowed source package boundaries, parseability and public signatures, standard-library-only imports, and AST-visible forbidden network/process/dynamic-code behavior. Process evidence that cannot be parsed is `unobservable`; it is never reported as PASS. Infrastructure-indeterminate decision evidence blocks finalists rather than becoming a candidate failure.

Reference implementations exist only under `validation/reference/`. Validation requires 20/20 twice with normalized identical output and targeted mutant rejection. Final asset freezing and candidate workspace creation are intentionally deferred until after main-agent review and insertion of `results/frozen-phi3-plan.md`.
