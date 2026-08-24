# Benchmark Adapter v1

The platform reaches existing research environments only through a frozen
adapter. The adapter is the boundary between a Benchmark revision and the
Runner; API routes never execute an episode directly.

## Frozen identity

Each TaskSpec binds a Benchmark revision, DatasetVersion, Agent revision,
Model revision and RunSpec hash. Compare rejects a different Benchmark or
Dataset revision before producing reward charts.

The SecRL fixture contains eight Incident scenarios and 589 test-split cases.
The canonical dataset SHA-256 and evaluator source hash are recorded in the
baseline documents. The adapter preserves Incident, Question, split, schema,
gold and metadata identities without exposing gold to the Agent.

## Runtime contract

`max_steps`, `max_str_len` and `max_entry_return` are read from the immutable
RunSpec. They are never selected by model name or mutable Agent parameters.
Every Observation records its original length and whether truncation occurred.
Action parsing accepts only one structured `tool_call`, `submit` or `yield`.
The platform executes benchmark tools and records the resulting artifact.

The adapter does not import Docker SDK, access Docker Socket, start or remove
containers, or respawn Incident services. MySQL access is read-only and is
selected by an explicit Compose Incident profile.

## Compatibility evidence

Fixture parity tests compare action, observation, reward, step, SQL-result hash
and truncation semantics against the frozen research behavior. Protocol-Smoke
remains model-free and deterministic.
