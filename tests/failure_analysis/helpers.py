from __future__ import annotations

from typing import Any


def question(text: str, answer: str = "answer", nodes: list[str] | None = None) -> dict[str, Any]:
    node_values = list(nodes or ["node-a", "node-b"])
    return {
        "answer": answer,
        "context": "context",
        "end_alert": "alert-end",
        "end_entities": ["entity-end"],
        "nodes": node_values,
        "question": text,
        "shortest_alert_path": node_values,
        "solution": "solution",
        "start_alert": "alert-start",
        "start_entities": ["entity-start"],
    }


def agent_entry(
    question_dict: dict[str, Any],
    reward: float = 0.0,
    nodes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "nodes": list(nodes or question_dict.get("nodes", [])),
        "question_dict": question_dict,
        "reward": reward,
    }


def env_entry(
    question_dict: dict[str, Any],
    reward: float = 0.0,
    trajectory: list[dict[str, Any]] | None = None,
    nodes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "nodes": list(nodes or question_dict.get("nodes", [])),
        "question": question_dict,
        "reward": reward,
        "trajectory": list(trajectory or []),
    }
