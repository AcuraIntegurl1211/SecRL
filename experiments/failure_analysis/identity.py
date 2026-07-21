from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .models import InputError, MappedQuestion, MappingError, QuestionIdentity


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise InputError(f"cannot read input path {path}: {exc}") from exc
    return digest.hexdigest()


def load_json(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"invalid JSON input {path}: {exc}") from exc

    if not isinstance(value, list):
        raise InputError(f"invalid JSON input {path}: expected top-level list")

    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InputError(
                f"invalid JSON input {path}: expected object at index {index}"
            )
    return value


def _normalized_question_text(question: dict[str, Any]) -> str:
    text = question.get("question")
    if not isinstance(text, str):
        raise MappingError("question dictionary is missing string field 'question'")
    return re.sub(r"\s+", " ", text).strip()


def question_identity(
    incident: str,
    index: int,
    question: dict[str, Any],
) -> QuestionIdentity:
    return QuestionIdentity(
        incident=incident,
        question_index=index,
        question_fingerprint_sha256=sha256_text(canonical_json(question)),
        question_text_fingerprint_sha256=sha256_text(
            _normalized_question_text(question)
        ),
    )


def _entry_question(
    source: str,
    source_index: int,
    entry: dict[str, Any],
) -> dict[str, Any]:
    question_dict = entry.get("question_dict")
    if isinstance(question_dict, dict):
        return question_dict

    question_value = entry.get("question")
    if isinstance(question_value, dict):
        return question_value

    raise MappingError(
        f"{source} entry at source index {source_index} has no question dictionary"
    )


def _map_source(
    source: str,
    entries: list[dict[str, Any]],
    full_index: dict[str, int],
    text_index: dict[str, list[int]],
    question_count: int,
) -> dict[int, tuple[int, dict[str, Any]]]:
    mapped: dict[int, tuple[int, dict[str, Any]]] = {}

    for source_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MappingError(
                f"{source} entry at source index {source_index} is not an object"
            )

        entry_question = _entry_question(source, source_index, entry)
        full_fingerprint = sha256_text(canonical_json(entry_question))
        question_index = full_index.get(full_fingerprint)

        if question_index is None:
            text_fingerprint = sha256_text(
                _normalized_question_text(entry_question)
            )
            candidates = text_index.get(text_fingerprint, [])
            if not candidates:
                raise MappingError(
                    f"{source} extra entry at source index {source_index}: "
                    f"fingerprint {full_fingerprint}"
                )
            if len(candidates) != 1:
                raise MappingError(
                    f"{source} ambiguous question text at source index "
                    f"{source_index}: candidates {candidates}"
                )
            question_index = candidates[0]

        if question_index in mapped:
            previous_index = mapped[question_index][0]
            raise MappingError(
                f"{source} duplicate mapping for question index {question_index}: "
                f"source indexes {previous_index} and {source_index}"
            )
        mapped[question_index] = (source_index, entry)

    missing = sorted(set(range(question_count)) - set(mapped))
    if missing:
        raise MappingError(f"{source} missing canonical question indexes: {missing}")

    return mapped


def map_logs(
    incident: str,
    agent_entries: list[dict[str, Any]],
    env_entries: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> list[MappedQuestion]:
    identities: list[QuestionIdentity] = []
    full_index: dict[str, int] = {}
    text_index: dict[str, list[int]] = {}

    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise MappingError(f"canonical question at index {index} is not an object")
        identity = question_identity(incident, index, question)
        fingerprint = identity.question_fingerprint_sha256
        if fingerprint in full_index:
            previous = full_index[fingerprint]
            raise MappingError(
                f"duplicate canonical question fingerprint at indexes "
                f"{previous} and {index}"
            )
        identities.append(identity)
        full_index[fingerprint] = index
        text_index.setdefault(
            identity.question_text_fingerprint_sha256,
            [],
        ).append(index)

    agent_map = _map_source(
        "agent",
        agent_entries,
        full_index,
        text_index,
        len(questions),
    )
    env_map = _map_source(
        "env",
        env_entries,
        full_index,
        text_index,
        len(questions),
    )

    return [
        MappedQuestion(
            identity=identities[index],
            question=question,
            agent=agent_map[index][1],
            env=env_map[index][1],
            agent_source_index=agent_map[index][0],
            env_source_index=env_map[index][0],
        )
        for index, question in enumerate(questions)
    ]
