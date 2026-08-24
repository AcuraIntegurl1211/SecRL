from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .models import Evidence, FeatureRecord, MappedQuestion, MappingError


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b", re.IGNORECASE)
_SHA1_RE = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)
_GUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_SID_RE = re.compile(r"\bs-\d+(?:-\d+){2,}\b", re.IGNORECASE)
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})\b",
    re.IGNORECASE,
)
_PROCESS_RE = re.compile(
    r"\b[a-z0-9_.-]+\.(?:exe|dll|sys|com|bat|cmd|ps1)\b",
    re.IGNORECASE,
)
_FILE_RE = re.compile(
    r"\b[a-z0-9_.-]+\.(?:docx?|xlsx?|pptx?|pdf|txt|csv|json|xml|"
    r"zip|rar|7z|log)\b",
    re.IGNORECASE,
)
_FQDN_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+"
    r"[a-z]{2,63}\b",
    re.IGNORECASE,
)
_SHORT_HOST_RE = re.compile(
    r"\b(?:host|server|srv|pc|vm|dc|ws|workstation|desktop|laptop|"
    r"machine|device|endpoint|client|node|win)[a-z0-9-]*\d[a-z0-9-]*\b",
    re.IGNORECASE,
)


def normalize_sql(value: str) -> str:
    normalized = value.strip().rstrip(";").strip()
    return re.sub(r"\s+", " ", normalized).lower()


def normalize_entities(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip().lower()
    substitutions = (
        (_URL_RE, "<url>"),
        (_SHA256_RE, "<sha256>"),
        (_SHA1_RE, "<sha1>"),
        (_GUID_RE, "<guid>"),
        (_SID_RE, "<sid>"),
        (_IP_RE, "<ip>"),
        (_TIMESTAMP_RE, "<timestamp>"),
        (_PROCESS_RE, "<process>"),
        (_FILE_RE, "<file>"),
        (_FQDN_RE, "<host>"),
        (_SHORT_HOST_RE, "<host>"),
    )
    for pattern, marker in substitutions:
        normalized = pattern.sub(marker, normalized)
    return normalized


def _canonical_timestamp(match: re.Match[str]) -> str:
    value = match.group(0).lower()
    if value.endswith("z"):
        value = value[:-1] + "+00:00"
    elif re.search(r"[+-]\d{4}$", value):
        value = value[:-2] + ":" + value[-2:]
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc).isoformat()


def _canonical_comparison(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip().lower()
    normalized = _TIMESTAMP_RE.sub(_canonical_timestamp, normalized)
    normalized = _FQDN_RE.sub(
        lambda match: match.group(0).split(".", 1)[0],
        normalized,
    )
    return normalized


def normalized_equivalent(left: str, right: str) -> bool:
    return _canonical_comparison(left) == _canonical_comparison(right)


def _clip_excerpt(value: object, limit: int = 240) -> tuple[str, bool]:
    excerpt = re.sub(r"\s+", " ", str(value)).strip()
    if len(excerpt) <= limit:
        return excerpt, False
    return excerpt[:limit], True


def _reward(entry: dict[str, Any], source: str) -> float | None:
    value = entry.get("reward")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MappingError(f"{source} reward is not numeric: {value!r}")
    return float(value)


def _official_reward(mapped: MappedQuestion) -> float:
    agent_reward = _reward(mapped.agent, "agent")
    env_reward = _reward(mapped.env, "env")
    if agent_reward is None and env_reward is None:
        raise MappingError("official reward is missing from agent and env logs")
    if (
        agent_reward is not None
        and env_reward is not None
        and agent_reward != env_reward
    ):
        raise MappingError(
            f"reward conflict: agent={agent_reward} env={env_reward}"
        )
    return agent_reward if agent_reward is not None else float(env_reward)


def _token_values(agent: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    summaries: list[dict[str, Any]] = []
    direct = agent.get("usage_summary")
    if isinstance(direct, dict):
        summaries.append(direct)

    trials = agent.get("trials")
    trial_values: list[Any] = []
    if isinstance(trials, dict):
        trial_values = list(trials.values())
    elif isinstance(trials, list):
        trial_values = trials
    for trial in trial_values:
        if isinstance(trial, dict) and isinstance(trial.get("usage_summary"), dict):
            summaries.append(trial["usage_summary"])

    for summary in reversed(summaries):
        model_values = [value for value in summary.values() if isinstance(value, dict)]
        for usage in reversed(model_values):
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            total = usage.get("total_tokens")
            if any(isinstance(value, int) for value in (prompt, completion, total)):
                return (
                    prompt if isinstance(prompt, int) else None,
                    completion if isinstance(completion, int) else None,
                    total if isinstance(total, int) else None,
                )
    return None, None, None


def _gold_components(value: object) -> list[str]:
    if isinstance(value, dict):
        components: list[str] = []
        for item in value.values():
            components.extend(_gold_components(item))
        return components
    if isinstance(value, (list, tuple, set)):
        components = []
        for item in value:
            components.extend(_gold_components(item))
        return components
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _gold_match(
    golden: object,
    observations: list[tuple[int, str]],
) -> tuple[str, list[int]]:
    components = _gold_components(golden)
    comparable = [(step, text) for step, text in observations if text.strip()]
    if not components or not comparable:
        return "indeterminate", []

    if isinstance(golden, str):
        whole_exact_steps = [
            step for step, text in comparable if golden.strip() == text.strip()
        ]
        if whole_exact_steps:
            return "exact", sorted(set(whole_exact_steps))
        normalized_steps = [
            step
            for step, text in comparable
            if normalized_equivalent(golden, text)
        ]
        if normalized_steps:
            return "normalized", sorted(set(normalized_steps))
        contained_steps = [step for step, text in comparable if golden in text]
        if contained_steps:
            return "exact", sorted(set(contained_steps))

    component_steps: list[int] = []
    for component in components:
        matches = [
            step
            for step, text in comparable
            if component in text or normalized_equivalent(component, text)
        ]
        if not matches:
            return "not_found", []
        component_steps.append(matches[0])
    return "component", sorted(set(component_steps))


def extract_features(mapped: MappedQuestion, max_steps: int) -> FeatureRecord:
    trajectory = mapped.env.get("trajectory")
    if not isinstance(trajectory, list):
        raise MappingError("env trajectory is not a list")

    sql_total = 0
    sql_success = 0
    sql_failure = 0
    empty_result_count = 0
    duplicate_query_count = 0
    submitted = False
    submitted_answer = ""
    submission_steps: list[int] = []
    evaluator_fields_complete = False
    evaluator_tokens: int | None = None
    evidence: list[Evidence] = []
    observations: list[tuple[int, str]] = []
    query_counts: dict[str, int] = {}

    evaluator_fields = (
        "check_ans_response",
        "check_ans_reflection",
        "check_sol_response",
        "check_sol_reflection",
    )

    for index, step in enumerate(trajectory):
        step_number = index + 1
        if not isinstance(step, dict):
            raise MappingError(f"env trajectory step {step_number} is not an object")
        info = step.get("info")
        if not isinstance(info, dict):
            info = {}

        if info.get("submit") is True:
            submitted = True
            submission_steps.append(step_number)
            answer = info.get("submitted_answer", step.get("action", ""))
            submitted_answer = answer if isinstance(answer, str) else str(answer)
            evaluator_fields_complete = all(
                info.get(field) is not None for field in evaluator_fields
            )
            direct_evaluator_tokens = info.get("evaluator_tokens")
            if isinstance(direct_evaluator_tokens, int):
                evaluator_tokens = direct_evaluator_tokens
            continue

        sql_total += 1
        action = step.get("action", "")
        normalized_query = normalize_sql(action if isinstance(action, str) else str(action))
        previous_count = query_counts.get(normalized_query, 0)
        if previous_count:
            duplicate_query_count += 1
            excerpt, truncated = _clip_excerpt(action)
            evidence.append(
                Evidence(
                    "duplicate_query",
                    step_number,
                    "env",
                    f"trajectory[{index}].action",
                    excerpt,
                    truncated,
                )
            )
        query_counts[normalized_query] = previous_count + 1

        observation = step.get("observation", "")
        observation_text = observation if isinstance(observation, str) else str(observation)
        query_success = info.get("query_success")
        if query_success is True:
            sql_success += 1
            observations.append((step_number, observation_text))
            if observation_text.strip() == "[]":
                empty_result_count += 1
                evidence.append(
                    Evidence(
                        "empty_result",
                        step_number,
                        "env",
                        f"trajectory[{index}].observation",
                        "[]",
                        False,
                    )
                )
        elif query_success is False:
            sql_failure += 1
            excerpt, truncated = _clip_excerpt(observation_text)
            evidence.append(
                Evidence(
                    "sql_error",
                    step_number,
                    "env",
                    f"trajectory[{index}].observation",
                    excerpt,
                    truncated,
                )
            )
        else:
            excerpt, truncated = _clip_excerpt(observation_text)
            evidence.append(
                Evidence(
                    "log_integrity",
                    step_number,
                    "env",
                    f"trajectory[{index}].info.query_success",
                    excerpt,
                    truncated,
                )
            )

    gold_match, gold_steps = _gold_match(
        mapped.question.get("answer"),
        observations,
    )
    prompt_tokens, completion_tokens, total_tokens = _token_values(mapped.agent)

    return FeatureRecord(
        mapped=mapped,
        reward_official=_official_reward(mapped),
        submitted_answer=submitted_answer,
        sql_total=sql_total,
        sql_success=sql_success,
        sql_failure=sql_failure,
        empty_result_count=empty_result_count,
        duplicate_query_count=duplicate_query_count,
        steps=len(trajectory),
        max_steps=max_steps,
        submitted=submitted,
        submitted_at_step_limit=(submitted and max_steps in submission_steps),
        gold_evidence_match=gold_match,
        gold_evidence_steps=gold_steps,
        evaluator_fields_complete=evaluator_fields_complete,
        agent_prompt_tokens=prompt_tokens,
        agent_completion_tokens=completion_tokens,
        agent_total_tokens=total_tokens,
        evaluator_tokens=evaluator_tokens,
        evidence=evidence,
    )
