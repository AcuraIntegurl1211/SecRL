from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .features import normalized_equivalent
from .models import Attribution, FeatureRecord, InputError


def load_taxonomy(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            taxonomy = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"invalid taxonomy {path}: {exc}") from exc

    if not isinstance(taxonomy, dict):
        raise InputError(f"invalid taxonomy {path}: expected object")
    if taxonomy.get("taxonomy_version") != "taxonomy_v1":
        raise InputError(f"invalid taxonomy {path}: expected taxonomy_v1")
    categories = taxonomy.get("categories")
    if not isinstance(categories, list) or not all(
        isinstance(item, str) for item in categories
    ):
        raise InputError(f"invalid taxonomy {path}: categories must be strings")
    calibration = taxonomy.get("calibration")
    if not isinstance(calibration, list) or not all(
        isinstance(item, dict) for item in calibration
    ):
        raise InputError(f"invalid taxonomy {path}: calibration must be objects")
    return taxonomy


def _calibration_match(
    features: FeatureRecord,
    taxonomy: dict[str, Any],
) -> dict[str, Any] | None:
    identity = features.mapped.identity
    for item in taxonomy.get("calibration", []):
        if (
            item.get("incident") == identity.incident
            and item.get("question_index") == identity.question_index
            and item.get("question_fingerprint_sha256")
            == identity.question_fingerprint_sha256
        ):
            return item
    return None


def _evidence_kinds(features: FeatureRecord) -> set[str]:
    return {item.kind for item in features.evidence}


def _secondary_causes(features: FeatureRecord, primary: str | None) -> list[str]:
    if primary is None or features.reward_official == 1:
        return []
    secondary: list[str] = []
    if features.duplicate_query_count > 0:
        secondary.append("LOOP")
    if features.steps >= features.max_steps:
        secondary.append("STEP_LIMIT")
    return secondary


def _explanation(features: FeatureRecord, primary: str | None) -> str:
    """Human-readable reason composed from the recorded run features."""
    parts: list[str] = []
    if not features.submitted:
        if features.steps >= features.max_steps:
            parts.append(
                f"agent used all {features.max_steps} steps without submitting an answer"
            )
        else:
            parts.append("run ended without a submission")
    elif features.submitted_answer.strip() in ("", "<answer>"):
        parts.append(
            "agent submitted an empty/placeholder answer after failed exploration"
        )
    if features.empty_result_count:
        parts.append(
            f"{features.empty_result_count} queries returned empty results "
            "(wrong filter, table, or schema expectation)"
        )
    if features.sql_failure:
        parts.append(
            f"{features.sql_failure} SQL errors vs {features.sql_success} successful queries"
        )
    if features.duplicate_query_count:
        parts.append(
            f"{features.duplicate_query_count} duplicate queries suggest looping"
        )
    if primary is not None and primary != "UNKNOWN":
        parts.append(f"primary cause classified as {primary}")
    return "; ".join(parts) if parts else "no distinguishing signals recorded"


def _candidate(
    features: FeatureRecord,
    taxonomy: dict[str, Any],
    primary: str | None,
    *,
    status: str = "candidate",
    confidence: str = "medium",
) -> Attribution:
    mandatory = set(taxonomy.get("always_human_review", []))
    reasons: list[str] = []
    if primary in mandatory:
        reasons.append(f"mandatory:{primary}")
    if confidence == "low":
        reasons.append("low_confidence")
    return Attribution(
        primary_cause_candidate=primary,
        primary_cause_status=status,
        secondary_cause_candidates=_secondary_causes(features, primary),
        confidence=confidence,
        explanation=_explanation(features, primary),
        needs_human_review=bool(reasons),
        human_review_reasons=reasons,
    )


def attribute_record(
    features: FeatureRecord,
    taxonomy: dict[str, Any],
) -> Attribution:
    if features.reward_official == 1:
        return _candidate(
            features,
            taxonomy,
            None,
            status="correct_control",
            confidence="high",
        )

    calibration = _calibration_match(features, taxonomy)
    if calibration is not None:
        primary = calibration.get("expected_primary")
        if calibration.get("review_status") == "confirmed":
            return _candidate(
                features,
                taxonomy,
                primary,
                status="confirmed",
                confidence="high",
            )
        return _candidate(
            features,
            taxonomy,
            primary,
            status="candidate",
            confidence="low",
        )

    kinds = _evidence_kinds(features)
    golden = features.mapped.question.get("answer")

    if "gold_inconsistency" in kinds:
        return _candidate(features, taxonomy, "GOLD", confidence="low")

    if (
        isinstance(golden, str)
        and golden.strip()
        and features.submitted_answer.strip()
        and normalized_equivalent(golden, features.submitted_answer)
    ):
        return _candidate(features, taxonomy, "EVALUATOR", confidence="medium")

    if "data_missing" in kinds:
        return _candidate(features, taxonomy, "DATA")

    if (
        features.sql_failure > 0
        and features.sql_success == 0
        and "sql_error" in kinds
    ):
        return _candidate(features, taxonomy, "SQL_EXEC")

    if (
        features.gold_evidence_match in {"exact", "normalized", "component"}
        and isinstance(golden, str)
        and not normalized_equivalent(golden, features.submitted_answer)
    ):
        return _candidate(features, taxonomy, "ANSWER")

    if "retrieval_mismatch" in kinds:
        return _candidate(features, taxonomy, "SQL_RETRIEVAL")
    if "navigation_mismatch" in kinds:
        return _candidate(features, taxonomy, "NAVIGATION")
    if "reasoning_mismatch" in kinds:
        return _candidate(features, taxonomy, "REASONING")

    return _candidate(features, taxonomy, "UNKNOWN", confidence="low")
