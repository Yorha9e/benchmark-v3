"""Agent trace pipeline: capture, normalize, annotate, export.

This package is the harness's first-class fine-tuning data pipeline
(spec §1 principle 3, §2 data-flow):

* :mod:`collector` — live streaming ingestion into
  :class:`AgentTrajectory` + ``wire.jsonl``.
* :mod:`normalizer` — ChatML / OpenAI-tools / Anthropic format conversion.
* :mod:`annotator` — milestone/reward binding + failure-step attribution.
* :mod:`exporter` — SFT golden JSONL and RL/DPO preference-pair JSONL.

Type definitions are imported from ``bench_harness.core.types`` when
available (Mission M1) and otherwise fall back to spec-§3-compatible
dataclasses (see :mod:`_compat`).
"""

from ._compat import (
    CANONICAL_TYPES,
    AgentTrajectory,
    EvaluationReport,
    MilestoneResult,
    TelemetryMetrics,
    TokenAuditMetrics,
    ToolCallRecord,
    ToolResultRecord,
    TrajectoryTurn,
    trajectory_from_dict,
    trajectory_to_dict,
    types_source,
)
from .annotator import AnnotatedTrajectory, TraceAnnotator
from .collector import TraceCollector
from .exporter import DatasetExporter
from .normalizer import TrajectoryNormalizer

__all__ = [
    "CANONICAL_TYPES",
    "AgentTrajectory",
    "AnnotatedTrajectory",
    "DatasetExporter",
    "EvaluationReport",
    "MilestoneResult",
    "TelemetryMetrics",
    "TokenAuditMetrics",
    "ToolCallRecord",
    "ToolResultRecord",
    "TraceAnnotator",
    "TraceCollector",
    "TrajectoryNormalizer",
    "TrajectoryTurn",
    "trajectory_from_dict",
    "trajectory_to_dict",
    "types_source",
]

__version__ = "3.0.0"
