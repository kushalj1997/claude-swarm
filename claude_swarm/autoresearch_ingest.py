"""Source-only autoresearch ingest compatibility helpers.

The helpers in this module translate a deep-ai autoresearch acceptance package
into provider-neutral request/result rows. They deliberately do not execute a
provider batch, upload files, or mark runtime/publication readiness.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


TERMINAL_STATUSES = frozenset({"succeeded", "errored", "canceled", "expired", "failed"})


@dataclass(frozen=True)
class AutoresearchChunkingPolicy:
    """Static policy for future large-package handling.

    ``files_api_execution`` stays false because this package only records the
    source contract. A later approved worker may use the policy to decide
    whether to upload artifacts, but this module never performs that action.
    """

    max_inline_bytes: int = 64 * 1024
    execution: str = "source_policy_only"
    files_api_execution: bool = False
    caveat: str = "files_api_not_executed"

    def metadata_for(self, package: Mapping[str, Any]) -> dict[str, Any]:
        package_bytes = len(json.dumps(package, sort_keys=True, default=str).encode("utf-8"))
        return {
            "max_inline_bytes": self.max_inline_bytes,
            "estimated_package_bytes": package_bytes,
            "requires_external_artifact": package_bytes > self.max_inline_bytes,
            "execution": self.execution,
            "files_api_execution": self.files_api_execution,
            "caveat": self.caveat,
        }


@dataclass(frozen=True)
class AutoresearchIngestRequest:
    """Provider-neutral row prepared from an acceptance package."""

    custom_id: str
    prompt: str
    metadata: dict[str, Any]
    provider: str = "provider-neutral"
    max_tokens: int = 8192


@dataclass(frozen=True)
class AutoresearchIngestResult:
    """Provider-neutral row returned by a future approved ingest run."""

    custom_id: str
    status: str
    output_text: str = ""
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None


def build_autoresearch_ingest_requests(
    acceptance_package: Mapping[str, Any],
    *,
    chunking_policy: AutoresearchChunkingPolicy | None = None,
) -> list[AutoresearchIngestRequest]:
    """Build source-only provider-neutral request rows.

    Existing provider-batch ``custom_id`` values are preserved. Missing ids are
    synthesized from RCG evidence identifiers and row position so result rows
    can merge back deterministically.
    """

    policy = chunking_policy or AutoresearchChunkingPolicy()
    package = dict(acceptance_package)
    rows = _provider_rows(package)
    return [
        AutoresearchIngestRequest(
            custom_id=_custom_id_for_row(package, row, index),
            prompt=_prompt_for_row(package, row),
            metadata=_metadata_for_row(package, row, policy),
        )
        for index, row in enumerate(rows)
    ]


def merge_autoresearch_ingest_results(
    acceptance_package: Mapping[str, Any],
    results: Iterable[AutoresearchIngestResult | Mapping[str, Any]],
) -> dict[str, Any]:
    """Merge provider-neutral result rows back into an acceptance package copy."""

    merged = copy.deepcopy(dict(acceptance_package))
    provider_batch = dict(_mapping_value(merged, "provider_batch", {}) or {})
    existing_rows = _provider_rows(merged)
    by_custom_id = {
        str(row["custom_id"]): dict(row)
        for row in existing_rows
        if row.get("custom_id")
    }
    ordered_ids = [
        str(row["custom_id"])
        for row in existing_rows
        if row.get("custom_id")
    ]

    for result in results:
        normalized = _result_row(result)
        custom_id = str(normalized["custom_id"])
        row = by_custom_id.get(custom_id, {"custom_id": custom_id})
        row.update(normalized)
        by_custom_id[custom_id] = row
        if custom_id not in ordered_ids:
            ordered_ids.append(custom_id)

    rows = [by_custom_id[custom_id] for custom_id in ordered_ids]
    provider_batch.update(_summarize_rows(rows))
    merged["provider_batch"] = provider_batch
    merged["provider_execution_verified"] = False
    merged["runtime_proof_required"] = True
    merged["protected_runtime_verified"] = False
    merged["publish_safe"] = False
    merged["claim_caveats"] = _unique(
        [
            *_as_list(_mapping_value(merged, "claim_caveats", [])),
            "not_live_provider_execution_proof",
        ]
    )
    merged["failure_reasons"] = _unique(
        [
            *_as_list(_mapping_value(merged, "failure_reasons", [])),
            "provider_runtime_not_verified",
        ]
    )
    return merged


def _provider_rows(package: Mapping[str, Any]) -> list[dict[str, Any]]:
    provider_batch = _as_mapping(_mapping_value(package, "provider_batch", {}))
    rows = [dict(row) for row in _as_list(provider_batch.get("results")) if isinstance(row, Mapping)]
    if rows:
        return rows

    custom_ids = [str(custom_id) for custom_id in _as_list(provider_batch.get("custom_ids")) if custom_id]
    if custom_ids:
        return [{"custom_id": custom_id, "status": None, "usage": {}} for custom_id in custom_ids]
    return [{"custom_id": _fallback_custom_id(package, 0), "status": None, "usage": {}}]


def _metadata_for_row(
    package: Mapping[str, Any],
    row: Mapping[str, Any],
    policy: AutoresearchChunkingPolicy,
) -> dict[str, Any]:
    evidence = _as_mapping(_mapping_value(package, "evidence_contract", {}))
    return {
        "acceptance_classification": _mapping_value(package, "classification", None),
        "acceptance_status": _mapping_value(package, "acceptance_status", None),
        "evidence_contract": dict(evidence),
        "source_label": _first_present(package, evidence, key="source_label"),
        "source_kind": _first_present(package, evidence, key="source_kind"),
        "source_host": _first_present(package, evidence, key="source_host"),
        "source_db": _first_present(package, evidence, key="source_db"),
        **_alias_metadata_for_package(package, evidence),
        "claim_caveats": _unique(
            [
                *_as_list(evidence.get("claim_caveats")),
                *_as_list(_mapping_value(package, "claim_caveats", [])),
                "source_only_ingest_request",
                "not_live_provider_execution_proof",
            ]
        ),
        "runtime_probe_required": bool(
            _mapping_value(package, "runtime_probe_required", None)
            or _mapping_value(package, "runtime_proof_required", None)
        ),
        "protected_runtime_verified": bool(_mapping_value(package, "protected_runtime_verified", False)),
        "publication_status": _mapping_value(package, "publication_status", None),
        "publish_safe": bool(_mapping_value(package, "publish_safe", False)),
        "artifact_provenance": {
            "manifest_path": evidence.get("artifact_manifest_path"),
            "sha256": evidence.get("artifact_sha256"),
            "generated_at": evidence.get("artifact_generated_at"),
        },
        "metric_sufficiency": _as_mapping(_mapping_value(package, "metric_sufficiency", {})),
        "plot_inventory": _as_mapping(_mapping_value(package, "plot_inventory", {})),
        "provider_batch_row": dict(row),
        "chunking_policy": policy.metadata_for(package),
    }


def _alias_metadata_for_package(package: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    alias_publish_safe = _first_present(package, evidence, key="alias_publish_safe")
    alias_runtime_probe_required = _first_present(package, evidence, key="alias_runtime_probe_required")
    alias_protected_runtime_verified = _first_present(
        package,
        evidence,
        key="alias_protected_runtime_verified",
    )
    alias_claim_caveats = _first_present(package, evidence, key="alias_claim_caveats")
    return {
        "canonical_model_id": _first_present(package, evidence, key="canonical_model_id"),
        "display_model_id": _first_present(package, evidence, key="display_model_id"),
        "display_model_alias": _first_present(package, evidence, key="display_model_alias"),
        "alias_publication_status": _first_present(
            package,
            evidence,
            key="alias_publication_status",
        ),
        "alias_publish_safe": False if alias_publish_safe is None else alias_publish_safe,
        "alias_runtime_probe_required": (
            True if alias_runtime_probe_required is None else alias_runtime_probe_required
        ),
        "alias_protected_runtime_verified": (
            False
            if alias_protected_runtime_verified is None
            else alias_protected_runtime_verified
        ),
        "alias_claim_caveats": _unique(_as_list(alias_claim_caveats)),
    }


def _prompt_for_row(package: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    plot_inventory = _as_mapping(_mapping_value(package, "plot_inventory", {}))
    metric_sufficiency = _as_mapping(_mapping_value(package, "metric_sufficiency", {}))
    no_data_ids = ", ".join(str(plot_id) for plot_id in _as_list(plot_inventory.get("no_data_plot_ids")))
    caveats = ", ".join(str(caveat) for caveat in _as_list(_mapping_value(package, "claim_caveats", [])))
    return "\n".join(
        [
            "Run a source-only autoresearch ingest review.",
            "Do not claim live provider execution, runtime proof, or publication readiness.",
            f"custom_id: {_mapping_value(row, 'custom_id', '')}",
            f"source_label: {_mapping_value(package, 'source_label', '')}",
            f"publication_status: {_mapping_value(package, 'publication_status', '')}",
            f"publish_safe: {_mapping_value(package, 'publish_safe', False)}",
            f"protected_runtime_verified: {_mapping_value(package, 'protected_runtime_verified', False)}",
            f"metric_quality_score: {metric_sufficiency.get('quality_score')}",
            f"populated_plots: {plot_inventory.get('populated_plot_count')}",
            f"no_data_plot_ids: {no_data_ids}",
            f"claim_caveats: {caveats}",
        ]
    )


def _custom_id_for_row(package: Mapping[str, Any], row: Mapping[str, Any], index: int) -> str:
    custom_id = _mapping_value(row, "custom_id", None)
    if custom_id:
        return str(custom_id)
    return _fallback_custom_id(package, index)


def _fallback_custom_id(package: Mapping[str, Any], index: int) -> str:
    evidence = _as_mapping(_mapping_value(package, "evidence_contract", {}))
    parts = [
        "autoresearch",
        _slug(evidence.get("experiment_id") or "unknown-experiment"),
        _slug(evidence.get("variant_id") or "unknown-variant"),
        _slug(evidence.get("run_id") or evidence.get("training_session_id") or "unknown-run"),
        f"row-{index + 1}",
    ]
    return ":".join(parts)


def _result_row(result: AutoresearchIngestResult | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(result, AutoresearchIngestResult):
        row = {
            "custom_id": result.custom_id,
            "status": result.status,
            "output_text": result.output_text,
            "error": result.error,
            "usage": dict(result.usage),
            "stop_reason": result.stop_reason,
        }
    else:
        row = {
            "custom_id": _mapping_value(result, "custom_id", None),
            "status": _mapping_value(result, "status", None),
            "output_text": _mapping_value(result, "output_text", None)
            or _content_text(_mapping_value(result, "content", [])),
            "error": _mapping_value(result, "error", None),
            "usage": _as_mapping(_mapping_value(result, "usage", {})),
            "stop_reason": _mapping_value(result, "stop_reason", None),
        }
    status = str(row.get("status") or "")
    row["terminal"] = status in TERMINAL_STATUSES
    return row


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    terminal_count = sum(1 for row in rows if row.get("terminal") or row.get("status") in TERMINAL_STATUSES)
    succeeded_count = sum(1 for row in rows if row.get("status") == "succeeded")
    failed_count = sum(1 for row in rows if row.get("status") and row.get("status") != "succeeded")
    custom_ids = [str(row["custom_id"]) for row in rows if row.get("custom_id")]
    return {
        "result_count": len(rows),
        "custom_ids": custom_ids,
        "terminal_status_count": terminal_count,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "results": rows,
        "passes_static_thresholds": bool(rows) and terminal_count == len(rows) and failed_count == 0,
    }


def _content_text(content: Any) -> str:
    parts = [
        str(block.get("text", ""))
        for block in _as_list(content)
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part).strip()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _mapping_value(mapping: Mapping[str, Any], key: str, default: Any = None) -> Any:
    return mapping.get(key, default)


def _first_present(*mappings: Mapping[str, Any], key: str) -> Any:
    for mapping in mappings:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _unique(values: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    unique_values: list[Any] = []
    for value in values:
        key = str(value)
        if value is None or key in seen:
            continue
        seen.add(key)
        unique_values.append(value)
    return unique_values


def _slug(value: Any) -> str:
    text = str(value).strip() or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-") or "unknown"


__all__ = [
    "AutoresearchChunkingPolicy",
    "AutoresearchIngestRequest",
    "AutoresearchIngestResult",
    "build_autoresearch_ingest_requests",
    "merge_autoresearch_ingest_results",
]
