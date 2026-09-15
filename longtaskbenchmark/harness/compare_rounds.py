from __future__ import annotations

import argparse
import csv
import io
import itertools
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from build_report import audit_legacy_hashes
from common import (
    CLOSED_LOOP_ROOT,
    PROJECT_ROOT,
    SchemaError,
    atomic_write_bytes,
    atomic_write_json,
    load_json,
    load_jsonl,
    load_manifest,
    ordered_slots,
    utc_now,
)


ROUND_ORDER = ("prototype-v1", "behavioral-quality-v4", "closed-loop-v2")


def _competition_ranks(
    ordered_records: list[dict[str, Any]], axis: Callable[[dict[str, Any]], tuple[Any, ...]]
) -> dict[str, int]:
    ranks: dict[str, int] = {}
    previous: tuple[Any, ...] | None = None
    current_rank = 0
    for position, record in enumerate(ordered_records, start=1):
        value = axis(record)
        if value != previous:
            current_rank = position
            previous = value
        ranks[record["slot"]] = current_rank
    return ranks


def _validate_slots(ranks: Mapping[str, int | None], slots: list[str], round_name: str) -> None:
    if set(ranks) != set(slots):
        raise SchemaError(f"{round_name} slots do not exactly match v2 manifest slots")


def load_prototype_v1(project_root: Path, slots: list[str]) -> dict[str, Any]:
    results = project_root / "prototype" / "results"
    with (results / "leaderboard.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        records = list(csv.DictReader(handle))
    if any(not isinstance(record.get("slot"), str) for record in records):
        raise SchemaError("prototype-v1 leaderboard contains an invalid slot")

    def axes(record: dict[str, Any]) -> tuple[Any, ...]:
        token_text = record.get("tokens_per_strict_success", "")
        token = float(token_text) if token_text not in {"", None} else float("inf")
        return (
            int(record.get("gate") == "True"),
            int(record["strict_success_count"]),
            float(record["acceptance_coverage"]),
            -token,
        )

    ordered = sorted(records, key=axes, reverse=True)
    ranks = _competition_ranks(ordered, axes)
    _validate_slots(ranks, slots, "prototype-v1")
    decision = load_json(results / "final-decision.json")
    finalist = decision.get("finalists")
    if finalist is None and isinstance(decision.get("executor_winner"), dict):
        winner_slot = decision["executor_winner"].get("slot")
        finalist = [winner_slot] if isinstance(winner_slot, str) else None
    if not isinstance(finalist, list) or any(slot not in ranks for slot in finalist):
        raise SchemaError("prototype-v1 final-decision finalist set is invalid")
    return {
        "round": "prototype-v1",
        "native_axes": [
            "InstructionGate(desc)",
            "StrictSuccessCount(desc)",
            "AcceptanceCoverage(desc)",
            "TokensPerStrictSuccess(asc)",
        ],
        "rank_semantics": "exact native-axis competition ranks; published top set comes from the frozen v1 decision",
        "ranks": ranks,
        "top_set": sorted(finalist),
        "decision_status": decision.get("executor_decision_status", decision.get("decision_status")),
    }


def load_behavioral_v4(project_root: Path, slots: list[str]) -> dict[str, Any]:
    root = project_root / "prototype" / "results" / "behavioral-quality-v4"
    records = load_jsonl(root / "raw-results.jsonl")

    def axes(record: dict[str, Any]) -> tuple[Any, ...]:
        return (
            int(record["instruction"]["instruction_gate"] is True),
            int(record["official"]["strict_success_count"]),
            float(record["official"]["acceptance_coverage"]),
            int(record["contract_extension"]["passed"]),
            int(record["resource_state"]["passed"]),
        )

    ordered = sorted(records, key=axes, reverse=True)
    ranks = _competition_ranks(ordered, axes)
    _validate_slots(ranks, slots, "behavioral-quality-v4")
    minimum = min(ranks.values())
    top_set = sorted(slot for slot, rank in ranks.items() if rank == minimum)
    return {
        "round": "behavioral-quality-v4",
        "native_axes": [
            "InstructionGate(desc)",
            "StrictSuccessCount(desc)",
            "AcceptanceCoverage(desc)",
            "ContractExtensionPasses(desc, capability aggregation)",
            "ResourceStatePasses(desc)",
        ],
        "rank_semantics": "exact native-axis competition ranks",
        "ranks": ranks,
        "top_set": top_set,
        "decision_status": "published" if top_set else "not_published",
    }


def load_closed_loop_v2(root: Path, slots: list[str]) -> dict[str, Any]:
    results = root / "results"
    decision = load_json(results / "final-decision.json")
    with (results / "leaderboard.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        records = list(csv.DictReader(handle))
    ranks: dict[str, int | None] = {}
    rank_statuses: dict[str, str] = {}
    for record in records:
        slot = record.get("Slot")
        rank = record.get("Rank")
        rank_status = record.get("RankStatus")
        if not isinstance(slot, str) or rank_status not in {"ranked", "indeterminate_within_primary_axes"}:
            raise SchemaError("closed-loop-v2 leaderboard has invalid Slot/RankStatus")
        if rank_status == "ranked":
            if not isinstance(rank, str) or not rank.isdigit():
                raise SchemaError("closed-loop-v2 ranked row has invalid Rank")
            parsed_rank: int | None = int(rank)
        else:
            if rank not in {"", None}:
                raise SchemaError("closed-loop-v2 indeterminate row must have an empty Rank")
            parsed_rank = None
        if slot in ranks:
            raise SchemaError(f"closed-loop-v2 duplicate slot: {slot}")
        ranks[slot] = parsed_rank
        rank_statuses[slot] = rank_status
    _validate_slots(ranks, slots, "closed-loop-v2")
    finalists = decision.get("finalists")
    if not isinstance(finalists, list) or any(slot not in ranks for slot in finalists):
        raise SchemaError("closed-loop-v2 finalist set is invalid")
    return {
        "round": "closed-loop-v2",
        "native_axes": [
            "InstructionGate(desc)",
            "ClosedLoopProjectCount(desc)",
            "MilestoneStrictCount(desc)",
            "AcceptanceCoverage(desc)",
            "InferenceTokensPerMilestoneStrictSuccess(asc)",
        ],
        "rank_semantics": "v2 leaderboard rank; indeterminate final axes are not force-ordered",
        "ranks": ranks,
        "rank_statuses": rank_statuses,
        "top_set": sorted(finalists),
        "decision_status": decision.get("decision_status"),
    }


def pair_relation(ranks: Mapping[str, int | None], left: str, right: str) -> str:
    left_rank = ranks[left]
    right_rank = ranks[right]
    if left_rank is None or right_rank is None:
        return "indeterminate"
    if left_rank < right_rank:
        return "left_ahead"
    if left_rank > right_rank:
        return "right_ahead"
    return "tie"


def top_set_movement(previous: Mapping[str, Any], current: Mapping[str, Any], slots: list[str]) -> list[dict[str, Any]]:
    previous_top = set(previous["top_set"])
    current_top = set(current["top_set"])
    movements: list[dict[str, Any]] = []
    for slot in slots:
        if slot in previous_top and slot in current_top:
            top_movement = "remained_in_top_set"
        elif slot in current_top:
            top_movement = "entered_top_set"
        elif slot in previous_top:
            top_movement = "left_top_set"
        else:
            top_movement = "outside_top_set"
        previous_rank = previous["ranks"][slot]
        current_rank = current["ranks"][slot]
        if previous_rank is None or current_rank is None:
            rank_movement = "indeterminate"
        elif current_rank < previous_rank:
            rank_movement = "improved"
        elif current_rank > previous_rank:
            rank_movement = "worsened"
        else:
            rank_movement = "unchanged_or_tied"
        movements.append(
            {
                "slot": slot,
                "from_round": previous["round"],
                "to_round": current["round"],
                "from_rank": previous_rank,
                "to_rank": current_rank,
                "rank_movement": rank_movement,
                "top_set_movement": top_movement,
            }
        )
    return movements


def pairwise_concordance(rounds: list[Mapping[str, Any]], slots: list[str]) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    counts = {"concordant": 0, "discordant": 0, "tie_changed": 0, "both_tied": 0, "indeterminate": 0}
    for left_round, right_round in itertools.combinations(rounds, 2):
        for left_slot, right_slot in itertools.combinations(slots, 2):
            left_relation = pair_relation(left_round["ranks"], left_slot, right_slot)
            right_relation = pair_relation(right_round["ranks"], left_slot, right_slot)
            if "indeterminate" in {left_relation, right_relation}:
                status = "indeterminate"
            elif left_relation == right_relation == "tie":
                status = "both_tied"
            elif "tie" in {left_relation, right_relation}:
                status = "tie_changed"
            elif left_relation == right_relation:
                status = "concordant"
            else:
                status = "discordant"
            counts[status] += 1
            details.append(
                {
                    "round_pair": [left_round["round"], right_round["round"]],
                    "slot_pair": [left_slot, right_slot],
                    "left_round_relation": left_relation,
                    "right_round_relation": right_relation,
                    "status": status,
                }
            )
    return {"counts": counts, "comparisons": details}


def _report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Cross-round mechanical comparison",
        "",
        "This comparison does not merge, normalize, or average scores across prototype-v1, behavioral-quality-v4, and closed-loop-v2. Each round keeps its own native axes.",
        "",
        "## Native top sets",
        "",
        "| Round | Decision status | Top set | Native axes |",
        "|---|---|---|---|",
    ]
    for round_payload in payload["rounds"]:
        lines.append(
            f"| {round_payload['round']} | `{round_payload['decision_status']}` | "
            f"{', '.join(round_payload['top_set']) if round_payload['top_set'] else 'not published'} | "
            f"{' → '.join(round_payload['native_axes'])} |"
        )
    lines.extend(("", "## Tie-aware movement", ""))
    for movement in payload["movement"]:
        if movement["top_set_movement"] != "outside_top_set" or movement["rank_movement"] != "unchanged_or_tied":
            lines.append(
                f"- `{movement['slot']}` {movement['from_round']}→{movement['to_round']}: "
                f"rank {movement['from_rank']}→{movement['to_rank']} ({movement['rank_movement']}), "
                f"{movement['top_set_movement']}."
            )
    counts = payload["pairwise_concordance"]["counts"]
    lines.extend(
        (
            "",
            "## Pairwise concordance summary",
            "",
            f"- Concordant: {counts['concordant']}",
            f"- Discordant: {counts['discordant']}",
            f"- Tie changed: {counts['tie_changed']}",
            f"- Both tied: {counts['both_tied']}",
            f"- Indeterminate: {counts['indeterminate']}",
            "",
            "Pairwise labels compare only native competition-rank relations. They are not a combined score or statistical inference.",
            "",
        )
    )
    return "\n".join(lines)


def compare_rounds(root: Path = CLOSED_LOOP_ROOT, output_dir: Path | None = None) -> dict[str, Any]:
    manifest = load_manifest(root)
    slots = ordered_slots(manifest)
    project_root = root.parent
    rounds = [
        load_prototype_v1(project_root, slots),
        load_behavioral_v4(project_root, slots),
        load_closed_loop_v2(root, slots),
    ]
    movement: list[dict[str, Any]] = []
    for previous, current in zip(rounds, rounds[1:]):
        movement.extend(top_set_movement(previous, current, slots))
    legacy_audit = audit_legacy_hashes(project_root)
    payload = {
        "schema_version": manifest["schema_version"],
        "generated_at": utc_now(),
        "round_order": list(ROUND_ORDER),
        "score_merging": False,
        "rounds": rounds,
        "movement": movement,
        "pairwise_concordance": pairwise_concordance(rounds, slots),
        "legacy_prototype_v4_hash_audit": legacy_audit,
        "publication_blocked": not legacy_audit["passed"],
    }
    destination = output_dir or root / "results"
    atomic_write_json(destination / "compare-rounds.json", payload)
    atomic_write_bytes(destination / "compare-rounds.md", _report(payload).encode("utf-8"))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare v1, v4, and v2 without combining their scores.")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    payload = compare_rounds(output_dir=args.output_dir)
    print(
        json.dumps(
            {
                "score_merging": payload["score_merging"],
                "publication_blocked": payload["publication_blocked"],
                "top_sets": {item["round"]: item["top_set"] for item in payload["rounds"]},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
