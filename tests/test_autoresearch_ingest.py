from __future__ import annotations

from pathlib import Path

from claude_swarm.autoresearch_ingest import (
    AutoresearchChunkingPolicy,
    AutoresearchIngestResult,
    build_autoresearch_ingest_requests,
    merge_autoresearch_ingest_results,
)


def _acceptance_package() -> dict[str, object]:
    return {
        "classification": "source_acceptance_package_only",
        "acceptance_status": "blocked_pending_source_evidence",
        "evidence_contract": {
            "experiment_id": 51,
            "variant_id": "v-claude-3.0-D1",
            "run_id": "v-claude-3.0-accepted-2026",
            "training_session_id": 606990,
            "artifact_manifest_path": "artifacts/rcg/v-claude-3.0-D1/manifest.json",
            "artifact_sha256": "b" * 64,
            "artifact_generated_at": "2026-06-02T05:55:00+00:00",
            "source_label": "lambda:lambda_pg:artifact",
            "source_kind": "artifact",
            "source_host": "lambda",
            "source_db": "training_db",
            "claim_caveats": ["runtime_proof_required", "not_publication_ready"],
        },
        "metric_sufficiency": {
            "quality_score": 74.5,
            "required_metric_count": 5,
            "populated_metric_count": 5,
            "missing_metrics": [],
            "passes_static_thresholds": True,
        },
        "plot_inventory": {
            "plot_count": 2,
            "populated_plot_count": 1,
            "no_data_plot_ids": ["00_no_data"],
            "no_data_panel_count": 1,
            "has_00_no_data_artifact": True,
            "passes_static_thresholds": False,
        },
        "provider_batch": {
            "custom_ids": ["rcg:D1:summary", "rcg:D1:risk"],
            "results": [
                {"custom_id": "rcg:D1:summary", "status": None, "usage": {}},
                {"custom_id": "rcg:D1:risk", "status": None, "usage": {}},
            ],
        },
        "runtime_proof_required": True,
        "protected_runtime_verified": False,
        "publication_status": "diagnostics_only_unverified",
        "publish_safe": False,
        "claim_caveats": [
            "source_acceptance_package_only",
            "runtime_proof_required",
            "not_provider_execution_proof",
        ],
        "source_label": "lambda:lambda_pg:artifact",
    }


def test_acceptance_package_builds_provider_neutral_ingest_requests() -> None:
    package = _acceptance_package()

    requests = build_autoresearch_ingest_requests(package)

    assert [request.custom_id for request in requests] == ["rcg:D1:summary", "rcg:D1:risk"]
    assert requests[0].provider == "provider-neutral"
    assert "source-only" in requests[0].prompt
    assert "00_no_data" in requests[0].prompt

    metadata = requests[0].metadata
    assert metadata["acceptance_classification"] == "source_acceptance_package_only"
    assert metadata["source_label"] == "lambda:lambda_pg:artifact"
    assert metadata["source_kind"] == "artifact"
    assert metadata["runtime_probe_required"] is True
    assert metadata["protected_runtime_verified"] is False
    assert metadata["publication_status"] == "diagnostics_only_unverified"
    assert metadata["publish_safe"] is False
    assert "not_provider_execution_proof" in metadata["claim_caveats"]
    assert metadata["artifact_provenance"] == {
        "manifest_path": "artifacts/rcg/v-claude-3.0-D1/manifest.json",
        "sha256": "b" * 64,
        "generated_at": "2026-06-02T05:55:00+00:00",
    }
    assert metadata["chunking_policy"]["execution"] == "source_policy_only"
    assert metadata["chunking_policy"]["files_api_execution"] is False
    assert metadata["chunking_policy"]["requires_external_artifact"] is False


def test_request_metadata_prefers_top_level_ark_alias_fields() -> None:
    package = _acceptance_package()
    package["evidence_contract"].update(
        {
            "canonical_model_id": "v-claude-3.0",
            "display_model_id": "Ark-0.1",
            "display_model_alias": "Ark-0.1",
            "alias_publication_status": "diagnostics_only_nested_alias",
            "alias_publish_safe": False,
            "alias_runtime_probe_required": True,
            "alias_protected_runtime_verified": False,
            "alias_claim_caveats": ["nested_alias_caveat"],
        }
    )
    package.update(
        {
            "canonical_model_id": "v-claude-3.0",
            "display_model_id": "Ark-0.1",
            "display_model_alias": "Ark-0.1",
            "alias_publication_status": "diagnostics_only_alias_unverified",
            "alias_publish_safe": False,
            "alias_runtime_probe_required": True,
            "alias_protected_runtime_verified": False,
            "alias_claim_caveats": ["top_level_alias_caveat"],
        }
    )

    metadata = build_autoresearch_ingest_requests(package)[0].metadata

    assert metadata["canonical_model_id"] == "v-claude-3.0"
    assert metadata["display_model_id"] == "Ark-0.1"
    assert metadata["display_model_alias"] == "Ark-0.1"
    assert metadata["alias_publication_status"] == "diagnostics_only_alias_unverified"
    assert metadata["alias_publish_safe"] is False
    assert metadata["alias_runtime_probe_required"] is True
    assert metadata["alias_protected_runtime_verified"] is False
    assert metadata["alias_claim_caveats"] == ["top_level_alias_caveat"]
    assert metadata["evidence_contract"]["alias_claim_caveats"] == ["nested_alias_caveat"]
    assert metadata["publish_safe"] is False
    assert metadata["protected_runtime_verified"] is False


def test_request_metadata_falls_back_to_nested_ark_alias_fields() -> None:
    package = _acceptance_package()
    package["evidence_contract"].update(
        {
            "canonical_model_id": "v-claude-3.0",
            "display_model_id": "Ark-0.1",
            "display_model_alias": "Ark-0.1",
            "alias_publication_status": "diagnostics_only_alias_unverified",
            "alias_publish_safe": False,
            "alias_runtime_probe_required": True,
            "alias_protected_runtime_verified": False,
            "alias_claim_caveats": ["ark_display_alias_only"],
        }
    )

    metadata = build_autoresearch_ingest_requests(package)[0].metadata

    assert metadata["canonical_model_id"] == "v-claude-3.0"
    assert metadata["display_model_id"] == "Ark-0.1"
    assert metadata["display_model_alias"] == "Ark-0.1"
    assert metadata["alias_publication_status"] == "diagnostics_only_alias_unverified"
    assert metadata["alias_publish_safe"] is False
    assert metadata["alias_runtime_probe_required"] is True
    assert metadata["alias_protected_runtime_verified"] is False
    assert metadata["alias_claim_caveats"] == ["ark_display_alias_only"]
    assert metadata["evidence_contract"]["canonical_model_id"] == "v-claude-3.0"


def test_ingest_results_merge_back_without_lifting_claim_safety() -> None:
    package = _acceptance_package()
    results = [
        AutoresearchIngestResult(
            custom_id="rcg:D1:summary",
            status="succeeded",
            output_text="candidate summary",
            usage={"input_tokens": 100, "output_tokens": 25},
            stop_reason="end_turn",
        ),
        AutoresearchIngestResult(
            custom_id="rcg:D1:risk",
            status="errored",
            error="provider timeout",
            usage={"input_tokens": 80, "output_tokens": 0},
        ),
    ]

    merged = merge_autoresearch_ingest_results(package, results)

    assert merged["provider_execution_verified"] is False
    assert merged["runtime_proof_required"] is True
    assert merged["protected_runtime_verified"] is False
    assert merged["publish_safe"] is False
    assert "not_live_provider_execution_proof" in merged["claim_caveats"]

    provider_batch = merged["provider_batch"]
    assert provider_batch["custom_ids"] == ["rcg:D1:summary", "rcg:D1:risk"]
    assert provider_batch["terminal_status_count"] == 2
    assert provider_batch["succeeded_count"] == 1
    assert provider_batch["failed_count"] == 1
    assert provider_batch["passes_static_thresholds"] is False
    assert provider_batch["results"][0]["output_text"] == "candidate summary"
    assert provider_batch["results"][0]["usage"]["input_tokens"] == 100
    assert provider_batch["results"][1]["error"] == "provider timeout"


def test_missing_provider_batch_ids_synthesize_stable_rcg_custom_ids() -> None:
    package = _acceptance_package()
    package["provider_batch"] = {
        "results": [
            {"status": None, "usage": {}},
            {"status": None, "usage": {}},
        ],
    }

    requests = build_autoresearch_ingest_requests(package)

    assert [request.custom_id for request in requests] == [
        "autoresearch:51:v-claude-3.0-D1:v-claude-3.0-accepted-2026:row-1",
        "autoresearch:51:v-claude-3.0-D1:v-claude-3.0-accepted-2026:row-2",
    ]


def test_large_package_chunk_policy_keeps_artifact_refs_without_files_api() -> None:
    package = _acceptance_package()
    package["large_inline_evidence"] = "x" * 256

    requests = build_autoresearch_ingest_requests(
        package,
        chunking_policy=AutoresearchChunkingPolicy(max_inline_bytes=64),
    )

    metadata = requests[0].metadata
    assert metadata["chunking_policy"]["requires_external_artifact"] is True
    assert metadata["chunking_policy"]["files_api_execution"] is False
    assert metadata["chunking_policy"]["execution"] == "source_policy_only"
    assert metadata["artifact_provenance"]["manifest_path"] == (
        "artifacts/rcg/v-claude-3.0-D1/manifest.json"
    )
    assert metadata["artifact_provenance"]["sha256"] == "b" * 64


def test_merge_maps_content_blocks_and_non_success_terminal_statuses() -> None:
    package = _acceptance_package()
    package["provider_batch"] = {
        "custom_ids": ["rcg:D1:cancel", "rcg:D1:expire", "rcg:D1:fail"],
    }

    merged = merge_autoresearch_ingest_results(
        package,
        [
            {
                "custom_id": "rcg:D1:cancel",
                "status": "canceled",
                "content": [{"type": "text", "text": "cancelled by operator"}],
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
            {"custom_id": "rcg:D1:expire", "status": "expired", "error": "batch expired"},
            {"custom_id": "rcg:D1:fail", "status": "failed", "error": "provider failure"},
        ],
    )

    provider_batch = merged["provider_batch"]
    assert provider_batch["terminal_status_count"] == 3
    assert provider_batch["succeeded_count"] == 0
    assert provider_batch["failed_count"] == 3
    assert provider_batch["passes_static_thresholds"] is False
    assert provider_batch["results"][0]["output_text"] == "cancelled by operator"
    assert provider_batch["results"][1]["error"] == "batch expired"
    assert provider_batch["results"][2]["error"] == "provider failure"


def test_readme_documents_source_only_autoresearch_helper() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    readme_lower = readme.lower()

    assert "source-only autoresearch ingest" in readme_lower
    assert "build_autoresearch_ingest_requests" in readme
    assert "merge_autoresearch_ingest_results" in readme
    assert "canonical_model_id" in readme
    assert "display_model_alias" in readme
    assert "No provider/API calls" in readme
