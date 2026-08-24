# SecRL Lite Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the approved SecRL Lite Web benchmark platform as a single-platform-container, single-runner product that supports Protocol-Smoke, SecRL, built-in agents, Agent Service Protocol v1, results, failure analysis, review, and Docker Compose deployment.

**Architecture:** Add a new `secrl_platform` package beside the existing research-oriented `secgym` package. The platform uses FastAPI, SQLAlchemy/Alembic, SQLite WAL, a local content-addressed artifact store, one runner process, versioned Benchmark/Agent protocols, and a React/TypeScript UI. Existing SecRL agents, environment behavior, evaluator, and failure-analysis code are reached only through compatibility adapters so their current behavior can be frozen and regression-tested.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, SQLite WAL, HTTPX, AES-GCM via `cryptography`, Argon2 password hashing, React, TypeScript, Vite, Vitest, Docker Compose v2, Python `unittest`.

---

## 1. Scope And Execution Order

This plan implements the Lite design in four independently testable milestones:

1. **Foundation:** platform package, configuration, SQLite, artifacts, Protocol-Smoke, deterministic runner.
2. **Runtime:** secrets, Model Gateway, built-in Agent adapter, HTTP Agent Service v1, task API.
3. **SecRL:** frozen compatibility adapters for existing Agent/Env/Evaluator and failure analysis.
4. **Product:** React UI, local authentication, Compare, Docker Compose, backup/restore, cross-platform verification.

Do not begin Milestone 3 until the local/remote SecRL baseline is frozen. Milestones 1 and 2 deliberately run without MySQL or an external LLM.

## 2. File Structure

Create the following focused modules:

```text
secrl_platform/
├── __init__.py
├── api/
│   ├── __init__.py
│   ├── app.py
│   ├── dependencies.py
│   ├── errors.py
│   ├── schemas.py
│   └── routes/
│       ├── __init__.py
│       ├── auth.py
│       ├── health.py
│       ├── models.py
│       ├── agents.py
│       ├── benchmarks.py
│       ├── tasks.py
│       ├── runs.py
│       ├── analysis.py
│       ├── artifacts.py
│       └── compare.py
├── auth/
│   ├── __init__.py
│   ├── passwords.py
│   └── sessions.py
├── benchmarks/
│   ├── __init__.py
│   ├── protocol.py
│   ├── registry.py
│   ├── smoke.py
│   ├── secrl.py
│   └── data/protocol_smoke_v1.json
├── agents/
│   ├── __init__.py
│   ├── protocol.py
│   ├── registry.py
│   ├── builtin.py
│   ├── capabilities.py
│   └── service.py
├── models/
│   ├── __init__.py
│   ├── gateway.py
│   ├── pricing.py
│   ├── providers.py
│   └── secrets.py
├── runner/
│   ├── __init__.py
│   ├── state.py
│   ├── engine.py
│   ├── process.py
│   └── recovery.py
├── analysis/
│   ├── __init__.py
│   └── service.py
├── storage/
│   ├── __init__.py
│   ├── artifacts.py
│   ├── database.py
│   ├── orm.py
│   └── repositories.py
├── config.py
└── cli.py

alembic.ini
alembic/
├── env.py
└── versions/0001_lite_schema.py

web/
├── package.json
├── tsconfig.json
├── vite.config.ts
├── index.html
└── src/
    ├── main.tsx
    ├── api/client.ts
    ├── app/App.tsx
    ├── app/router.tsx
    ├── components/AppShell.tsx
    ├── components/HealthBadge.tsx
    ├── components/MetricValue.tsx
    ├── pages/LoginPage.tsx
    ├── pages/DashboardPage.tsx
    ├── pages/ModelsPage.tsx
    ├── pages/AgentsPage.tsx
    ├── pages/BenchmarksPage.tsx
    ├── pages/NewEvaluationPage.tsx
    ├── pages/RunDetailPage.tsx
    ├── pages/AnalysisReviewPage.tsx
    └── pages/ComparePage.tsx

tests/platform/
├── __init__.py
├── helpers.py
├── test_config.py
├── test_database.py
├── test_artifacts.py
├── test_smoke_benchmark.py
├── test_agent_protocol.py
├── test_agent_service.py
├── test_builtin_agents.py
├── test_secrets.py
├── test_model_gateway.py
├── test_runner.py
├── test_api.py
├── test_secrl_adapter.py
├── test_analysis_service.py
├── test_backup_restore.py
└── test_recovery.py

tests/e2e/
├── test_protocol_smoke_e2e.py
└── test_secrl_fixture_e2e.py

docker/
├── lite/Dockerfile
├── lite/entrypoint.sh
├── mysql/init-incident.sh
└── agent-service-reference/Dockerfile

examples/
└── agent_service/
    ├── app.py
    └── manifest.json

compose.yaml
requirements-build.txt
requirements-platform.in
requirements-platform.txt
.env.example
scripts/lite-backup.sh
scripts/lite-restore.sh
```

Responsibilities remain strict:

- `secgym/` keeps existing Benchmark algorithms and agents.
- `secrl_platform/benchmarks/secrl.py` is the only platform module that translates SecRL Incident/Question/SQL semantics.
- `secrl_platform/agents/builtin.py` is the only module that translates current Agent constructors and reset signatures.
- `experiments/failure_analysis/` remains the attribution implementation; `analysis/service.py` only materializes inputs and registers outputs.
- API routes never execute a Benchmark episode directly.
- Full trajectories never enter SQLite JSON columns.

## 3. Milestone 1: Foundation And Protocol-Smoke

### Task 1: Freeze The Research Baseline

**Files:**
- Create: `docs/baselines/secrl-lite-baseline.md`
- Create: `docs/baselines/secrl-lite-files.sha256`
- Create: `tests/fixtures/platform/baseline/README.md`

- [ ] **Step 1: Record local and Ubuntu revisions**

Run locally:

```bash
git rev-parse HEAD
git status --short
```

Run on the Ubuntu source checkout:

```bash
git -C /home/acuraintegurl/Desktop/SecRL-git rev-parse HEAD
git -C /home/acuraintegurl/Desktop/SecRL-git status --short
```

Expected: record local `93daa706d5c093343837381444e1bf31d45bc9cf` and remote `d0f07a8b327f96b41807de5e95d710ca3462300f`, then list every remote modification affecting experiment semantics.

- [ ] **Step 2: Write the baseline decision record**

`docs/baselines/secrl-lite-baseline.md` must record these concrete values from the inspected worktree:

- The clean canonical commit emitted by `git rev-parse HEAD` after approved no-truncation changes are committed.
- Local comparison commit `93daa706d5c093343837381444e1bf31d45bc9cf`.
- Remote comparison commit `d0f07a8b327f96b41807de5e95d710ca3462300f`.
- Exact Python 3.11 patch version emitted by `python --version`.
- Question split `secgym/questions/o1/test` and count `589`.
- MySQL `9.0` per-platform digest emitted by `docker image inspect`.
- Evaluator and built-in Agent SHA-256 rows from `secrl-lite-files.sha256`.
- Every included no-truncation file/commit and every excluded dirty file with its reason.

Do not commit the baseline record unless all values are literal command output or explicit file lists and the canonical worktree is clean.

- [ ] **Step 3: Generate source hashes**

Run:

```bash
find secgym experiments/failure_analysis -type f -name '*.py' -print0 | sort -z | xargs -0 shasum -a 256 > docs/baselines/secrl-lite-files.sha256
```

Expected: one deterministic SHA-256 row per Python source file.

- [ ] **Step 4: Run the authoritative failure-analysis tests**

Run on Ubuntu:

```bash
/home/acuraintegurl/miniconda3/envs/excytin/bin/python -m unittest discover -s tests/failure_analysis -v
```

Expected: the approved Python 3.11 baseline passes all tests available in that checkout. Record the exact count in the baseline document.

- [ ] **Step 5: Commit the baseline only**

```bash
git add docs/baselines tests/fixtures/platform/baseline
git commit -m "docs: freeze SecRL Lite research baseline"
```

### Task 2: Add Platform Dependencies And Package Entry Point

**Files:**
- Create: `requirements-platform.in`
- Create: `requirements-platform.txt`
- Create: `secrl_platform/__init__.py`
- Create: `secrl_platform/cli.py`
- Modify: `setup.py`
- Test: `tests/platform/test_config.py`

- [ ] **Step 1: Write the import/CLI test**

```python
import unittest
from secrl_platform.cli import build_parser


class PlatformCliTest(unittest.TestCase):
    def test_serve_command_is_registered(self):
        args = build_parser().parse_args(["serve", "--host", "0.0.0.0"])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.host, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Verify the test fails**

Run:

```bash
python -m unittest tests.platform.test_config -v
```

Expected: `ModuleNotFoundError: No module named 'secrl_platform'`.

- [ ] **Step 3: Add direct platform dependencies**

`requirements-platform.in`:

```text
-r requirements.txt
alembic>=1.13,<2
argon2-cffi>=23,<26
cryptography>=43,<47
fastapi>=0.115,<1
httpx>=0.27,<1
pydantic-settings>=2,<3
python-multipart>=0.0.9,<1
sqlalchemy>=2,<3
uvicorn[standard]>=0.30,<1
```

Create `requirements-build.txt` containing `pip-tools>=7,<8`, then generate `requirements-platform.txt` with hashes in the approved Python 3.11 build environment:

```bash
python -m pip install -r requirements-build.txt
python -m piptools compile --generate-hashes --output-file requirements-platform.txt requirements-platform.in
```

Do not hand-edit the generated lock file.

- [ ] **Step 4: Add the CLI entry point**

`secrl_platform/cli.py`:

```python
import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secrl-lite")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    subparsers.add_parser("run-worker")
    subparsers.add_parser("verify-artifacts")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "serve":
        import uvicorn
        uvicorn.run("secrl_platform.api.app:create_app", factory=True, host=args.host, port=args.port)
        return 0
    if args.command == "run-worker":
        from secrl_platform.runner.process import run_forever
        return run_forever()
    from secrl_platform.storage.artifacts import verify_all_artifacts
    return 0 if verify_all_artifacts() else 1
```

Add `secrl-lite=secrl_platform.cli:main` under `console_scripts` in `setup.py`. Keep the existing `secgym` `python_requires` unchanged; the platform Docker image and `requirements-platform.txt` enforce Python 3.11 without removing the research package's declared compatibility.

- [ ] **Step 5: Run and commit**

```bash
python -m unittest tests.platform.test_config -v
git add requirements-build.txt requirements-platform.in requirements-platform.txt setup.py secrl_platform tests/platform/test_config.py
git commit -m "build: add SecRL Lite platform package"
```

Expected: one passing test and an importable `secrl-lite` command.

### Task 3: Implement Typed Configuration

**Files:**
- Create: `secrl_platform/config.py`
- Modify: `tests/platform/test_config.py`

- [ ] **Step 1: Add failing configuration tests**

```python
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError
from secrl_platform.config import Settings


class SettingsTest(unittest.TestCase):
    def test_data_paths_are_derived_from_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), master_key="a" * 64, session_secret="s" * 32)
            self.assertEqual(settings.database_path, Path(tmp) / "secrl-lite.sqlite3")
            self.assertEqual(settings.artifact_dir, Path(tmp) / "artifacts")

    def test_master_key_must_be_32_byte_hex(self):
        with self.assertRaises(ValidationError):
            Settings(master_key="short", session_secret="s" * 32)
```

- [ ] **Step 2: Verify failure**

```bash
python -m unittest tests.platform.test_config -v
```

Expected: import failure for `secrl_platform.config`.

- [ ] **Step 3: Implement settings**

```python
from functools import cached_property
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SECRL_", env_file=".env")

    data_dir: Path = Path("/data")
    master_key: str = Field(min_length=64, max_length=64)
    session_secret: str = Field(min_length=32)
    host: str = "0.0.0.0"
    port: int = 8080
    runner_poll_seconds: float = 1.0
    agent_service_allowlist: tuple[str, ...] = ("agent-service-reference",)

    @field_validator("master_key")
    @classmethod
    def validate_hex_key(cls, value: str) -> str:
        bytes.fromhex(value)
        return value

    @cached_property
    def database_path(self) -> Path:
        return self.data_dir / "secrl-lite.sqlite3"

    @cached_property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"
```

- [ ] **Step 4: Run tests**

```bash
python -m unittest tests.platform.test_config -v
```

Expected: all settings tests pass.

- [ ] **Step 5: Commit**

```bash
git add secrl_platform/config.py tests/platform/test_config.py
git commit -m "feat: add validated Lite platform settings"
```

### Task 4: Add SQLite Schema And Repositories

**Files:**
- Create: `secrl_platform/storage/database.py`
- Create: `secrl_platform/storage/orm.py`
- Create: `secrl_platform/storage/repositories.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_lite_schema.py`
- Create: `tests/platform/test_database.py`

- [ ] **Step 1: Write schema and queue tests**

```python
import tempfile
import unittest
from pathlib import Path

from secrl_platform.storage.database import create_engine_and_session
from secrl_platform.storage.repositories import TaskRepository


class DatabaseTest(unittest.TestCase):
    def test_only_one_task_can_be_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_factory = create_engine_and_session(Path(tmp) / "test.sqlite3", create=True)
            repo = TaskRepository(session_factory)
            first = repo.create({"name": "first"})
            second = repo.create({"name": "second"})
            self.assertEqual(repo.claim_next().id, first.id)
            self.assertIsNone(repo.claim_next())
            repo.finish(first.id, "SUCCEEDED")
            self.assertEqual(repo.claim_next().id, second.id)
```

- [ ] **Step 2: Verify failure**

```bash
python -m unittest tests.platform.test_database -v
```

Expected: storage modules are missing.

- [ ] **Step 3: Define the ORM schema**

Create SQLAlchemy 2 declarative models for the 16 Lite tables from the approved design. Use string UUID4 IDs generated by `uuid.uuid4()`, UTC timestamps, explicit foreign keys, JSON text encoded canonically, and enums stored as constrained strings. `RunORM` must include:

```python
class RunORM(Base):
    __tablename__ = "run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("evaluation_task.id"), index=True)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenario.id"))
    status: Mapped[str] = mapped_column(String(32), index=True)
    run_spec_json: Mapped[str] = mapped_column(Text)
    run_spec_sha256: Mapped[str] = mapped_column(String(64))
    next_case_index: Mapped[int] = mapped_column(default=0)
    pause_requested: Mapped[bool] = mapped_column(default=False)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
```

Enable on every connection:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

- [ ] **Step 4: Implement atomic task claim**

`TaskRepository.claim_next()` must use one short transaction:

```python
with self._session_factory.begin() as session:
    running = session.scalar(select(EvaluationTaskORM.id).where(EvaluationTaskORM.status == "RUNNING"))
    if running is not None:
        return None
    task = session.scalar(
        select(EvaluationTaskORM)
        .where(EvaluationTaskORM.status == "QUEUED")
        .order_by(EvaluationTaskORM.created_at, EvaluationTaskORM.id)
        .limit(1)
    )
    if task is None:
        return None
    task.status = "RUNNING"
    task.started_at = utc_now()
    session.flush()
    return TaskRecord.from_orm(task)
```

- [ ] **Step 5: Run migration and tests**

```bash
alembic upgrade head
python -m unittest tests.platform.test_database -v
```

Expected: migration succeeds twice and queue test passes.

- [ ] **Step 6: Commit**

```bash
git add alembic.ini alembic secrl_platform/storage tests/platform/test_database.py
git commit -m "feat: add Lite SQLite schema and task repository"
```

### Task 5: Add Content-Addressed Artifact Storage

**Files:**
- Create: `secrl_platform/storage/artifacts.py`
- Create: `tests/platform/test_artifacts.py`

- [ ] **Step 1: Write atomicity and integrity tests**

```python
import tempfile
import unittest
from pathlib import Path

from secrl_platform.storage.artifacts import LocalArtifactStore, ArtifactIntegrityError


class ArtifactStoreTest(unittest.TestCase):
    def test_put_is_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalArtifactStore(Path(tmp))
            first = store.put_bytes("trajectory", b'{"steps":[]}')
            second = store.put_bytes("trajectory", b'{"steps":[]}')
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.path, second.path)

    def test_verify_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalArtifactStore(Path(tmp))
            ref = store.put_bytes("log", b"safe")
            ref.path.write_bytes(b"changed")
            with self.assertRaises(ArtifactIntegrityError):
                store.verify(ref)
```

- [ ] **Step 2: Verify failure**

```bash
python -m unittest tests.platform.test_artifacts -v
```

- [ ] **Step 3: Implement atomic writes**

Use `tempfile.NamedTemporaryFile(dir=target.parent, delete=False)`, flush, `os.fsync`, calculate SHA-256 while writing, then set `target = root / "sha256" / digest[:2] / digest[2:4] / digest` and call `os.replace`. Never derive a path from a user filename.

The public return type is:

```python
@dataclass(frozen=True)
class ArtifactRef:
    kind: str
    sha256: str
    size: int
    path: Path
    media_type: str
```

- [ ] **Step 4: Run tests and verifier**

```bash
python -m unittest tests.platform.test_artifacts -v
secrl-lite verify-artifacts
```

Expected: tests pass; an empty or valid store exits 0.

- [ ] **Step 5: Commit**

```bash
git add secrl_platform/storage/artifacts.py tests/platform/test_artifacts.py
git commit -m "feat: add content-addressed artifact storage"
```

### Task 6: Freeze Benchmark Adapter Types

**Files:**
- Create: `secrl_platform/benchmarks/protocol.py`
- Create: `secrl_platform/benchmarks/registry.py`
- Create: `tests/platform/test_smoke_benchmark.py`

- [ ] **Step 1: Write contract validation tests**

```python
import unittest
from pydantic import ValidationError

from secrl_platform.benchmarks.protocol import Observation, parse_agent_action


class BenchmarkProtocolTest(unittest.TestCase):
    def test_tool_call_requires_object_arguments(self):
        action = parse_agent_action({
            "type": "tool_call", "tool": "search", "arguments": {"query": "alpha"}
        })
        self.assertEqual(action.tool, "search")

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValidationError):
            parse_agent_action({"type": "shell", "command": "id"})

    def test_observation_records_truncation(self):
        observation = Observation(type="tool_result", content={}, truncated=True)
        self.assertTrue(observation.truncated)
```

- [ ] **Step 2: Verify failure**

```bash
python -m unittest tests.platform.test_smoke_benchmark.BenchmarkProtocolTest -v
```

- [ ] **Step 3: Implement immutable Pydantic protocol models**

Define `BenchmarkManifest`, `DatasetManifest`, `DatasetRef`, `ValidationReport`, `ScenarioRef`, `CaseRef`, `Scope`, `EpisodeRef`, `Submission`, `ToolDefinition`, `ToolCallAction`, `SubmitAction`, `YieldAction`, `AgentAction`, `Observation`, `EnvironmentLease`, `EvaluationResult`, and `MetricDefinition`. All models use:

```python
class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
```

`AgentAction` must be `Annotated[ToolCallAction | SubmitAction | YieldAction, Field(discriminator="type")]`. Define one module-level `TypeAdapter(AgentAction)` and expose `parse_agent_action(payload)`; do not use arbitrary dict actions inside the runner.

Define the adapter surface exactly once in this module:

```python
class BenchmarkAdapterProtocol(Protocol):
    def manifest(self) -> BenchmarkManifest: ...
    def validate_dataset(self, source: Path) -> ValidationReport: ...
    def enumerate_cases(self, dataset: DatasetRef, scope: Scope) -> list[CaseRef]: ...
    def tool_definitions(self) -> list[ToolDefinition]: ...
    def prepare_scenario(self, scenario: ScenarioRef) -> EnvironmentLease: ...
    def start_episode(self, case: CaseRef, lease: EnvironmentLease) -> Observation: ...
    def execute_action(self, episode: EpisodeRef, action: AgentAction) -> Observation: ...
    def evaluate(self, episode: EpisodeRef, submission: Submission) -> EvaluationResult: ...
    def close_episode(self, episode: EpisodeRef) -> None: ...
    def release_scenario(self, lease: EnvironmentLease) -> None: ...
```

- [ ] **Step 4: Add registry duplicate protection**

```python
class BenchmarkRegistry:
    def register(self, adapter: BenchmarkAdapterProtocol) -> None:
        key = adapter.manifest().benchmark_id
        if key in self._adapters:
            raise DuplicateBenchmarkError(key)
        self._adapters[key] = adapter

    def get(self, benchmark_id: str) -> BenchmarkAdapterProtocol:
        try:
            return self._adapters[benchmark_id]
        except KeyError as exc:
            raise UnknownBenchmarkError(benchmark_id) from exc
```

- [ ] **Step 5: Run and commit**

```bash
python -m unittest tests.platform.test_smoke_benchmark.BenchmarkProtocolTest -v
git add secrl_platform/benchmarks tests/platform/test_smoke_benchmark.py
git commit -m "feat: define Benchmark Adapter v1 contracts"
```

### Task 7: Implement Protocol-Smoke Benchmark

**Files:**
- Create: `secrl_platform/benchmarks/data/protocol_smoke_v1.json`
- Create: `secrl_platform/benchmarks/smoke.py`
- Modify: `tests/platform/test_smoke_benchmark.py`

- [ ] **Step 1: Add a deterministic dataset fixture**

Use 12 cases covering exact answer, normalized answer, search/read multi-step, unknown key, long observation, invalid tool arguments, max steps, and wrong answer. Each record has:

```json
{
  "id": "smoke-001",
  "public_input": {"question": "What value belongs to alpha?"},
  "documents": {"doc-alpha": "alpha = 17"},
  "gold": {"answer": "17"}
}
```

- [ ] **Step 2: Add failing full-episode test**

```python
def test_smoke_search_read_submit_episode(self):
    adapter = ProtocolSmokeAdapter.load_default()
    case = adapter.enumerate_cases(adapter.dataset_ref(), Scope.all())[0]
    episode = adapter.start_episode(case, adapter.prepare_scenario(case.scenario))
    search = adapter.execute_action(
        episode.ref,
        ToolCallAction(type="tool_call", tool="search", arguments={"query": "alpha"}),
    )
    self.assertEqual(search.content["matches"], ["doc-alpha"])
    read = adapter.execute_action(
        episode.ref,
        ToolCallAction(type="tool_call", tool="read", arguments={"id": "doc-alpha"}),
    )
    self.assertIn("17", read.content["text"])
    result = adapter.evaluate(episode.ref, Submission(answer="17"))
    self.assertEqual(result.reward, 1.0)
```

- [ ] **Step 3: Verify failure**

```bash
python -m unittest tests.platform.test_smoke_benchmark -v
```

- [ ] **Step 4: Implement the adapter**

Keep environment state in a per-episode dataclass keyed by an opaque episode ID. Validate every action against the registered tool schema. The evaluator performs Unicode normalization, surrounding whitespace trim, and exact comparison; it never calls an LLM.

- [ ] **Step 5: Add canonical dataset hash assertion**

The test calculates the canonical JSON SHA-256 and asserts the exact digest stored in the adapter manifest. Updating dataset content must intentionally update the expected digest and DatasetVersion.

- [ ] **Step 6: Run and commit**

```bash
python -m unittest tests.platform.test_smoke_benchmark -v
git add secrl_platform/benchmarks tests/platform/test_smoke_benchmark.py
git commit -m "feat: add Protocol-Smoke benchmark adapter"
```

## 4. Milestone 2: Agent, Model, Runner, And API

### Task 8: Define Agent Runtime And Deterministic Built-In Agent

**Files:**
- Create: `secrl_platform/agents/protocol.py`
- Create: `secrl_platform/agents/registry.py`
- Create: `secrl_platform/agents/builtin.py`
- Create: `tests/platform/test_agent_protocol.py`

- [x] **Step 1: Write runtime equivalence tests**

```python
class AgentRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_deterministic_agent_returns_typed_action(self):
        runtime = DeterministicSmokeAgent()
        await runtime.reset(smoke_episode_context())
        action = await runtime.act(initial_smoke_observation())
        self.assertEqual(action.type, "tool_call")
        self.assertEqual(action.tool, "search")
        await runtime.close()

    def test_registry_rejects_unapproved_revision(self):
        registry = AgentRegistry()
        registry.register(unapproved_agent_revision())
        with self.assertRaises(UnapprovedAgentError):
            registry.resolve("agent-revision-id")
```

- [x] **Step 2: Define runtime models and protocol**

Implement immutable `AgentManifest`, `AgentRevisionRef`, `EpisodeContext`, `UsageSnapshot`, and:

```python
class AgentRuntime(Protocol):
    @property
    def name(self) -> str: ...
    async def reset(self, episode: EpisodeContext) -> None: ...
    async def act(self, observation: Observation) -> AgentAction: ...
    def usage(self) -> UsageSnapshot: ...
    async def close(self) -> None: ...
```

- [x] **Step 3: Implement deterministic smoke agent**

The agent follows a fixed state machine: `search -> read -> submit`. It must not depend on a model, making it suitable for CI and runtime equivalence tests. Built-in adapters expose async methods even when the wrapped research Agent is synchronous, so the runner has one interface for local and HTTP runtimes.

- [x] **Step 4: Run and commit**

```bash
python -m unittest tests.platform.test_agent_protocol -v
git add secrl_platform/agents tests/platform/test_agent_protocol.py
git commit -m "feat: add Agent Runtime v1 contracts"
```

### Task 9: Implement Encrypted Secret Store

**Files:**
- Create: `secrl_platform/models/secrets.py`
- Create: `secrl_platform/auth/passwords.py`
- Create: `tests/platform/test_secrets.py`

- [x] **Step 1: Write non-disclosure and round-trip tests**

```python
class SecretStoreTest(unittest.TestCase):
    def test_ciphertext_does_not_contain_plaintext(self):
        store = SecretStore(bytes.fromhex("11" * 32))
        encrypted = store.encrypt("sk-private-value")
        self.assertNotIn(b"sk-private-value", encrypted.ciphertext)
        self.assertEqual(store.decrypt(encrypted), "sk-private-value")

    def test_mask_never_returns_secret_fragments(self):
        self.assertEqual(mask_secret("sk-private-value"), "configured")
```

- [x] **Step 2: Implement AES-GCM envelopes**

Use a random 96-bit nonce and associated data containing `secret_ref_id`, `owner_id`, provider, and key version. Store nonce, ciphertext, tag, key version, created time, and status. Never define a serializer that exposes decrypted values.

- [x] **Step 3: Implement Argon2 password hashing**

```python
from argon2 import PasswordHasher

_hasher = PasswordHasher()

def hash_password(password: str) -> str:
    return _hasher.hash(password)

def verify_password(encoded: str, password: str) -> bool:
    try:
        return _hasher.verify(encoded, password)
    except Exception:
        return False
```

Catch the specific Argon2 verification exceptions in final code rather than broad `Exception`.

- [x] **Step 4: Run and commit**

```bash
python -m unittest tests.platform.test_secrets -v
git add secrl_platform/models/secrets.py secrl_platform/auth tests/platform/test_secrets.py
git commit -m "feat: encrypt model secrets and hash local passwords"
```

### Task 10: Implement Model Gateway

**Files:**
- Create: `secrl_platform/models/providers.py`
- Create: `secrl_platform/models/pricing.py`
- Create: `secrl_platform/models/gateway.py`
- Create: `tests/platform/test_model_gateway.py`

- [x] **Step 1: Write provider retry and accounting tests**

```python
class ModelGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_429_retries_and_records_one_successful_usage(self):
        provider = FakeProvider([
            ProviderError("RATE_LIMITED", retry_after=0),
            ModelResponse(text="ok", usage=Usage(prompt=10, completion=2)),
        ])
        gateway = ModelGateway(provider=provider, pricing=Pricing(input_per_million=1, output_per_million=2))
        result = await gateway.complete(model_request())
        self.assertEqual(provider.calls, 2)
        self.assertEqual(result.usage.total, 12)
        self.assertEqual(result.estimated_cost, Decimal("0.000014"))
```

- [x] **Step 2: Define normalized request/response types**

`ModelRequest` includes provider adapter version, model role, messages, requested/effective parameters, timeout, Run/Case/Attempt correlation IDs, and cache metadata. `ModelResponse` preserves raw provider usage plus normalized prompt/completion/cached/reasoning values.

- [x] **Step 3: Implement OpenAI-compatible adapter first**

Use HTTPX with explicit timeout and no automatic unbounded retries. Classify 401/403/404 as permanent, 408/429/5xx as transient, parse `Retry-After`, and cap attempts from ModelConfigRevision.

- [x] **Step 4: Implement frozen pricing**

Use `Decimal` only. Missing usage or price returns `estimated_cost=None`, never zero. Store the PricingProfileRevision hash with every calculated cost.

- [x] **Step 5: Run and commit**

```bash
python -m unittest tests.platform.test_model_gateway -v
git add secrl_platform/models tests/platform/test_model_gateway.py
git commit -m "feat: add normalized model gateway and cost accounting"
```

### Task 11: Implement Agent Service Protocol v1 Client And Reference Server

**Files:**
- Create: `secrl_platform/agents/capabilities.py`
- Create: `secrl_platform/agents/service.py`
- Create: `examples/agent_service/app.py`
- Create: `examples/agent_service/manifest.json`
- Create: `tests/platform/test_agent_service.py`

- [x] **Step 1: Write idempotency and security tests**

```python
class AgentServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_retry_reuses_request_id_and_sequence(self):
        transport = RecordingTransport(timeout_once=True)
        runtime = AgentServiceRuntime(config=service_config(), transport=transport)
        await runtime.reset(smoke_episode_context())
        await runtime.act(observation())
        first, second = transport.requests
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["sequence"], second["sequence"])

    async def test_unknown_tool_action_is_rejected_before_execution(self):
        runtime = runtime_returning(
            {"type": "tool_call", "tool": "shell", "arguments": {}},
            allowed_tools={"search", "read", "submit"},
        )
        await runtime.reset(smoke_episode_context())
        with self.assertRaises(InvalidAgentAction):
            await runtime.act(observation())
```

- [x] **Step 2: Implement endpoint allowlist and manifest handshake**

Resolve the configured hostname, reject userinfo/fragments, reject hosts outside `Settings.agent_service_allowlist`, fetch `/v1/manifest`, and require protocol version `1` plus the registered manifest SHA-256.

- [x] **Step 3: Implement session lifecycle**

Use the approved endpoints and immutable payload models. `AgentServiceRuntime` maintains monotonically increasing sequence numbers, caches the last request/response pair, closes sessions in `finally`, and maps protocol errors to the standard platform error codes.

- [x] **Step 4: Implement short-lived capability tokens**

Use an HMAC-SHA256 signed canonical JSON token with claims for Run ID, AgentRevision ID, allowed model roles, maximum token/cost budget, issued-at, expiry, and random nonce. Model Gateway rejects altered, expired, wrong-run, wrong-agent, or over-budget tokens. Add tests for every rejection path; token lifetime defaults to five minutes and is refreshed only while the Run lease is active.

```python
def test_capability_rejects_tamper_expiry_scope_and_budget(self):
    token = self.signer.issue(valid_claims(expires_in_seconds=300))
    with self.assertRaises(InvalidCapability):
        self.signer.verify(tamper(token), expected_run="run-1", expected_agent="agent-1")
    with self.assertRaises(ExpiredCapability):
        self.signer.verify(self.signer.issue(valid_claims(expires_in_seconds=-1)))
    with self.assertRaises(CapabilityScopeError):
        self.signer.verify(token, expected_run="run-2", expected_agent="agent-1")
    with self.assertRaises(CapabilityBudgetError):
        self.signer.authorize_usage(token, additional_tokens=10_001, additional_cost=Decimal("0"))
```

- [x] **Step 5: Add the deterministic reference service**

The example server wraps `DeterministicSmokeAgent`. It accepts only a short-lived signed capability token, exposes health/manifest, and implements idempotent `:act` responses.

- [x] **Step 6: Prove built-in/service equivalence**

Run the same Protocol-Smoke case through the built-in runtime and ASGI in-memory reference service. Assert identical canonical Action/Observation sequences and reward.

- [x] **Step 7: Run and commit**

```bash
python -m unittest tests.platform.test_agent_service -v
git add secrl_platform/agents/capabilities.py secrl_platform/agents/service.py examples/agent_service tests/platform/test_agent_service.py
git commit -m "feat: add Agent Service Protocol v1 runtime"
```

### Task 12: Implement Runner State Machine And Recovery

**Files:**
- Create: `secrl_platform/runner/state.py`
- Create: `secrl_platform/runner/engine.py`
- Create: `secrl_platform/runner/process.py`
- Create: `secrl_platform/runner/recovery.py`
- Create: `tests/platform/test_runner.py`
- Create: `tests/platform/test_recovery.py`
- Create: `tests/e2e/test_protocol_smoke_e2e.py`

- [x] **Step 1: Write transition tests**

```python
class RunStateTest(unittest.TestCase):
    def test_pause_only_becomes_paused_after_case_commit(self):
        machine = RunStateMachine("RUNNING")
        machine.request_pause()
        self.assertEqual(machine.state, "PAUSE_REQUESTED")
        machine.case_committed()
        self.assertEqual(machine.state, "PAUSED")

    def test_invalid_transition_is_rejected(self):
        with self.assertRaises(InvalidTransition):
            RunStateMachine("SUCCEEDED").transition("RUNNING")
```

- [x] **Step 2: Implement explicit transition table**

```python
ALLOWED_TRANSITIONS = {
    "DRAFT": {"QUEUED", "CANCELED"},
    "QUEUED": {"RUNNING", "CANCELED"},
    "RUNNING": {"PAUSE_REQUESTED", "SUCCEEDED", "FAILED", "BUDGET_EXHAUSTED", "CANCELED"},
    "PAUSE_REQUESTED": {"PAUSED", "FAILED", "CANCELED"},
    "PAUSED": {"QUEUED", "CANCELED"},
    "SUCCEEDED": set(),
    "FAILED": {"QUEUED"},
    "BUDGET_EXHAUSTED": set(),
    "CANCELED": set(),
}
```

- [x] **Step 3: Write an interrupted-case recovery test**

Create a task with three smoke cases, inject a crash after artifact write but before database commit on case two, restart the engine, and assert:

```python
self.assertEqual(repo.final_attempt_count(task.id, "smoke-001"), 1)
self.assertEqual(repo.attempt_count(task.id, "smoke-002"), 2)
self.assertEqual(repo.final_result_count(task.id), 3)
self.assertTrue(store.unreferenced_artifacts())
```

- [x] **Step 4: Implement episode loop**

For each Case: create attempt, reset Agent, loop `act -> validate -> execute`, write trajectory artifact, atomically register artifact/result/checkpoint, then honor pause/cancel. Benchmark errors remain result evidence; platform errors choose retry/fail using typed error codes.

- [x] **Step 5: Add budgets**

Before each model call and new Case, compare accumulated token/cost against TaskSpec. Crossing the hard limit transitions to `BUDGET_EXHAUSTED` after committing the current evidence.

- [x] **Step 6: Run e2e tests**

```bash
python -m unittest tests.platform.test_runner tests.platform.test_recovery tests.e2e.test_protocol_smoke_e2e -v
```

Expected: the 12-case dataset completes without MySQL or LLM, pause/recovery tests pass, and all artifacts verify.

- [x] **Step 7: Commit**

```bash
git add secrl_platform/runner tests/platform/test_runner.py tests/platform/test_recovery.py tests/e2e/test_protocol_smoke_e2e.py
git commit -m "feat: run and recover Protocol-Smoke evaluations"
```

### Task 13: Expose The Minimal API

**Files:**
- Create: `secrl_platform/api/app.py`
- Create: `secrl_platform/api/dependencies.py`
- Create: `secrl_platform/api/errors.py`
- Create: `secrl_platform/api/schemas.py`
- Create: `secrl_platform/api/routes/*.py`
- Create: `secrl_platform/auth/sessions.py`
- Create: `tests/platform/test_api.py`

- [x] **Step 1: Write unauthenticated and lifecycle API tests**

```python
class ApiTest(unittest.TestCase):
    def test_secret_endpoint_requires_login(self):
        response = self.client.get("/api/v1/models")
        self.assertEqual(response.status_code, 401)

    def test_create_task_returns_frozen_spec_hash(self):
        self.login()
        response = self.client.post("/api/v1/tasks", json=valid_smoke_task())
        self.assertEqual(response.status_code, 201)
        self.assertRegex(response.json()["task_spec_sha256"], r"^[0-9a-f]{64}$")
```

- [x] **Step 2: Implement app factory and error envelope**

All errors use:

```json
{
  "error": {
    "code": "INVALID_TASK_SPEC",
    "message": "Human-readable summary",
    "details": {},
    "request_id": "7bf92f5a-7b9d-4c8e-a53b-7d36fa8a6d4d"
  }
}
```

Generate request IDs at middleware entry and return them in `X-Request-ID`.

- [x] **Step 3: Implement single-admin sessions**

Use an HttpOnly, SameSite=Strict, Secure-when-HTTPS cookie containing an opaque random session ID. Store only its SHA-256 in SQLite with expiry and CSRF token. State-changing requests require the CSRF header.

- [x] **Step 4: Add route surface**

Implement:

```text
POST /api/v1/auth/login
POST /api/v1/auth/logout
GET  /api/v1/health
GET/POST /api/v1/models
GET/POST /api/v1/agents
POST /api/v1/agents/{id}:check
GET /api/v1/benchmarks
POST /api/v1/tasks
GET /api/v1/tasks
GET /api/v1/runs/{id}
POST /api/v1/runs/{id}:pause
POST /api/v1/runs/{id}:resume
POST /api/v1/runs/{id}:cancel
GET /api/v1/runs/{id}/cases
POST /api/v1/runs/{id}/cases/{case_id}:retry
POST /api/v1/runs/{id}:analyze
GET /api/v1/runs/{id}/analysis
POST /api/v1/attributions/{id}/reviews
GET /api/v1/artifacts/{id}/metadata
GET /api/v1/artifacts/{id}
GET /api/v1/compare
```

Artifact download validates authorization, hash, and path before returning a file response.

- [x] **Step 5: Run API tests and OpenAPI snapshot**

```bash
python -m unittest tests.platform.test_api -v
python -c 'from secrl_platform.api.app import create_app; import json; print(json.dumps(create_app().openapi(), sort_keys=True))' > tests/fixtures/platform/openapi-v1.json
```

Expected: API tests pass and the OpenAPI snapshot contains no secret plaintext response fields.

- [x] **Step 6: Commit**

```bash
git add secrl_platform/api secrl_platform/auth/sessions.py tests/platform/test_api.py tests/fixtures/platform/openapi-v1.json
git commit -m "feat: expose authenticated Lite evaluation API"
```

## 5. Milestone 3: SecRL And Failure Analysis

### Task 14: Import SecRL Dataset As Scenario/Case

**Files:**
- Create: `secrl_platform/benchmarks/secrl.py`
- Create: `tests/platform/test_secrl_adapter.py`
- Create: `tests/fixtures/platform/secrl_question_sample.json`

- [ ] **Step 1: Write 589-question integrity test**

```python
class SecRLImportTest(unittest.TestCase):
    def test_test_split_contains_expected_incidents_and_count(self):
        report = SecRLAdapter().validate_dataset(Path("secgym/questions/o1/test"))
        self.assertEqual(report.total_cases, 589)
        self.assertEqual(report.scenario_counts, {
            "incident_5": 98, "incident_34": 82, "incident_38": 11,
            "incident_39": 98, "incident_55": 100, "incident_134": 57,
            "incident_166": 87, "incident_322": 56,
        })
```

- [ ] **Step 2: Implement canonical import**

Map Incident to Scenario and Question to Case. Store `answer` and `solution` in the encrypted/restricted gold payload, not public input. Preserve all source fields in the canonical source artifact. Use `incident + index + full canonical question SHA-256` as the stable identity tuple.

- [ ] **Step 3: Implement SecRL Tool schema**

Register only `sql_query` and `submit`. Before forwarding SQL, reject multiple statements, DML, DDL, file operations, and administrative commands. Database credentials are held by the EnvironmentProvider, never the Agent runtime.

- [ ] **Step 4: Run import tests**

```bash
python -m unittest tests.platform.test_secrl_adapter.SecRLImportTest -v
```

Expected: exactly eight scenarios and 589 cases with deterministic hashes.

- [ ] **Step 5: Commit**

```bash
git add secrl_platform/benchmarks/secrl.py tests/platform/test_secrl_adapter.py tests/fixtures/platform/secrl_question_sample.json
git commit -m "feat: import SecRL as Benchmark Adapter v1 data"
```

### Task 15: Adapt Excytin Environment Without Runtime Docker Control

**Files:**
- Modify: `secrl_platform/benchmarks/secrl.py`
- Create: `tests/e2e/test_secrl_fixture_e2e.py`
- Test against: `secgym/excytin_env.py`

- [ ] **Step 1: Extract a regression fixture from an approved run**

Create a small, non-secret fixture containing Question identity, initial observation, ordered SQL/actions, normalized observations, submitted answer, reward, steps, and truncation flags. Record the original artifact SHA-256.

- [ ] **Step 2: Write adapter parity test**

```python
class SecRLAdapterParityTest(unittest.TestCase):
    def test_fixture_actions_preserve_observation_and_step_semantics(self):
        result = replay_fixture_through_adapter("tests/fixtures/platform/secrl_run_sample.json")
        self.assertEqual(result.submitted_answer, result.fixture.submitted_answer)
        self.assertEqual(result.steps, result.fixture.steps)
        self.assertEqual(result.reward, result.fixture.reward)
        self.assertEqual(result.observation_hashes, result.fixture.observation_hashes)
```

- [ ] **Step 3: Implement external environment connection**

`prepare_scenario()` resolves the Compose service name from a fixed registry, checks health and manifest hash, obtains the read-only Benchmark account, and returns an opaque lease. It must not import Docker SDK or call `respawn`.

- [ ] **Step 4: Preserve truncation semantics**

Pass `max_str_len` and `max_entry_return` from RunSpec. Record both original size and truncation flag in Observation metadata. The no-truncation values are valid explicit settings, not magic model-name branches.

- [ ] **Step 5: Run parity tests**

```bash
python -m unittest tests.platform.test_secrl_adapter tests.e2e.test_secrl_fixture_e2e -v
```

Expected: fixture parity passes and no platform module accesses the Docker SDK.

- [ ] **Step 6: Commit**

```bash
git add secrl_platform/benchmarks/secrl.py tests/e2e tests/fixtures/platform/secrl_run_sample.json
git commit -m "feat: execute SecRL through a fixed environment adapter"
```

### Task 16: Add Existing Agent Compatibility Adapters

**Files:**
- Modify: `secrl_platform/agents/builtin.py`
- Create: `tests/platform/test_builtin_agents.py`
- Test against: `secgym/agents/*.py`

- [ ] **Step 1: Write constructor/reset contract tests**

Create one fake model client and instantiate each approved built-in Agent through the registry. Assert every adapter supports the same `reset(EpisodeContext)`, `act(Observation)`, `usage()`, and `close()` surface, including Expel's question dictionary requirement.

- [ ] **Step 2: Implement explicit constructor registry**

Use a source-controlled mapping of approved agent revision IDs to adapter factories. Do not use `eval`, arbitrary import strings, or model-name substring behavior.

- [ ] **Step 3: Normalize action and usage**

Convert current Agent action strings to typed `AgentAction`; preserve parser errors as `INVALID_ACTION`. Map each existing usage format into `UsageSnapshot` while retaining the raw usage artifact.

- [ ] **Step 4: Run tests**

```bash
python -m unittest tests.platform.test_builtin_agents -v
```

Expected: all approved built-in Agent adapters satisfy the same contract using fake model responses.

- [ ] **Step 5: Commit**

```bash
git add secrl_platform/agents/builtin.py tests/platform/test_builtin_agents.py
git commit -m "feat: adapt built-in SecRL agents to Agent Runtime v1"
```

### Task 17: Wrap The Official Evaluator

**Files:**
- Modify: `secrl_platform/benchmarks/secrl.py`
- Create: `secrl_platform/models/evaluator.py`
- Modify: `tests/platform/test_secrl_adapter.py`

- [ ] **Step 1: Write frozen evaluator request test**

Given a fixture Question, gold answer, submission, and EvaluatorProfileRevision, assert exact prompt SHA-256, requested/effective model params, parser version, and normalized reward.

- [ ] **Step 2: Implement evaluator profile**

The official profile contains model revision, prompt template artifact hash, temperature, seed, retry policy, parser version, and success rule. The runner does not accept per-Task overrides when `formal=True`.

- [ ] **Step 3: Separate usage accounting**

Store evaluator usage under role `evaluator`; never add it to `agent_tokens_per_question`. Preserve raw response as a restricted artifact.

- [ ] **Step 4: Run and commit**

```bash
python -m unittest tests.platform.test_secrl_adapter -v
git add secrl_platform/models/evaluator.py secrl_platform/benchmarks/secrl.py tests/platform/test_secrl_adapter.py
git commit -m "feat: freeze SecRL evaluator profiles and usage"
```

### Task 18: Integrate Failure Analysis And Human Review

**Files:**
- Create: `secrl_platform/analysis/service.py`
- Create: `tests/platform/test_analysis_service.py`
- Reuse: `experiments/failure_analysis/*`

- [ ] **Step 1: Write immutable analysis input test**

```python
class AnalysisServiceTest(unittest.TestCase):
    def test_analysis_records_all_input_and_output_hashes(self):
        result = self.service.analyze(completed_fixture_run())
        self.assertRegex(result.input_manifest_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(result.output_manifest_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(result.taxonomy_version, "taxonomy_v1")
```

- [ ] **Step 2: Materialize read-only CLI inputs**

Create a temporary analysis directory containing linked/copied Agent JSON, Env JSON, Question JSON, taxonomy, and an input manifest. Verify all hashes before invoking analysis.

- [ ] **Step 3: Invoke existing analysis entry points**

Call the existing library/CLI in a subprocess using the platform Python executable, capture stdout/stderr as artifacts, and import structured outputs only after manifest verification. Do not duplicate attribution rules in platform code.

- [ ] **Step 4: Implement append-only review**

`submit_review()` inserts a new HumanReview row with monotonically increasing revision, prior review ID, reviewer, primary, secondary, confidence, evidence references, and notes. It never updates Attribution or prior reviews.

- [ ] **Step 5: Run and commit**

```bash
python -m unittest tests.platform.test_analysis_service -v
python -m unittest discover -s tests/failure_analysis -v
git add secrl_platform/analysis tests/platform/test_analysis_service.py
git commit -m "feat: integrate versioned failure analysis and review"
```

## 6. Milestone 4: Web Product And Deployment

### Task 19: Build The React Application Shell

**Files:**
- Create: `web/package.json`
- Create: `web/tsconfig.json`
- Create: `web/vite.config.ts`
- Create: `web/index.html`
- Create: `web/src/main.tsx`
- Create: `web/src/app/App.tsx`
- Create: `web/src/app/router.tsx`
- Create: `web/src/api/client.ts`
- Create: `web/src/components/AppShell.tsx`
- Create: `web/src/components/HealthBadge.tsx`
- Create: `web/src/components/MetricValue.tsx`

- [x] **Step 1: Add frontend test/build scripts**

`web/package.json` scripts:

```json
{
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "test": "vitest run",
    "lint": "eslint ."
  }
}
```

Use React Router, TanStack Query, React Hook Form, Zod, and Lucide React. Pin exact versions in the generated lockfile.

- [x] **Step 2: Write navigation test**

Render `AppShell` with a memory router and assert Dashboard, Models, Agents, Benchmarks, New Evaluation, Runs, Analysis, and Compare navigation targets are present with accessible names.

- [x] **Step 3: Implement typed API client**

Generate TypeScript API types from `tests/fixtures/platform/openapi-v1.json`. `apiFetch()` always sends credentials and CSRF for mutations, parses the common error envelope, and never logs request bodies containing secrets.

- [x] **Step 4: Implement the operational shell**

Use a fixed-width responsive sidebar on desktop and a drawer on mobile. Keep content unframed except for repeated result items and modals. Use Lucide icons with tooltips for icon-only controls.

- [x] **Step 5: Run and commit**

```bash
npm --prefix web ci
npm --prefix web test
npm --prefix web run build
git add web
git commit -m "feat: add SecRL Lite web application shell"
```

Expected: tests and production build pass with no TypeScript errors.

### Task 20: Implement Core Pages

**Files:**
- Create: `web/src/pages/LoginPage.tsx`
- Create: `web/src/pages/DashboardPage.tsx`
- Create: `web/src/pages/ModelsPage.tsx`
- Create: `web/src/pages/AgentsPage.tsx`
- Create: `web/src/pages/BenchmarksPage.tsx`
- Create: `web/src/pages/NewEvaluationPage.tsx`
- Create: `web/src/pages/RunDetailPage.tsx`
- Create: `web/src/pages/AnalysisReviewPage.tsx`
- Create: `web/src/pages/ComparePage.tsx`
- Create: `web/src/pages/pages.test.tsx`

- [x] **Step 1: Write workflow tests**

Mock the API and test:

1. Reject an unauthenticated route and complete local-admin login with CSRF setup.
2. Create ModelConfig with secret status displayed as `Configured`, never the entered key.
3. Register an allowlisted Agent Service and display manifest health.
4. Create a Protocol-Smoke task through scope/runtime/budget confirmation.
5. Pause and resume a Run.
6. Open a trajectory step without loading the whole artifact.
7. Submit a HumanReview revision.
8. Compare two same-Benchmark tasks and reject a cross-Benchmark reward chart.

- [x] **Step 2: Implement dashboard and configuration pages**

Dashboard polls summarized status every five seconds only while a task is active. Models uses a password input that clears after submission. Agents shows runtime type, revision hash, protocol and health. Benchmarks shows DatasetVersion and manifest integrity.

- [x] **Step 3: Implement task creation**

Use four compact steps: Scope, Runtime, Reliability, Budget/Review. Render Agent parameters from JSON Schema. Show validation errors next to their fields and a final immutable-spec summary.

- [x] **Step 4: Implement run and trajectory views**

Use stable table/grid dimensions, server pagination, lazy artifact ranges, and tabs for Overview, Cases, Trajectory, Analysis, Artifacts, Audit. Long SQL and observations use code blocks with explicit expand controls.

- [x] **Step 5: Implement analysis/review and compare**

Review keeps automatic candidate read-only and appends a review revision. Compare requires identical BenchmarkRevision/DatasetVersion for reward charts; token/cost metadata identifies missing values rather than treating them as zero.

- [x] **Step 6: Run and commit**

```bash
npm --prefix web test
npm --prefix web run build
git add web/src/pages
git commit -m "feat: add Lite evaluation management pages"
```

### Task 21: Package Docker Compose And Incident Profiles

**Files:**
- Create: `docker/lite/Dockerfile`
- Create: `docker/lite/entrypoint.sh`
- Create: `docker/mysql/init-incident.sh`
- Create: `docker/agent-service-reference/Dockerfile`
- Create: `compose.yaml`
- Create: `.env.example`
- Modify: `.gitignore`

- [x] **Step 1: Add a multi-stage platform image**

Stages:

1. Node builds `web/dist` from lockfile.
2. Python installs locked platform dependencies and package.
3. Runtime copies static assets, uses a non-root UID, and declares `/data` only as writable.

Do not copy `.env`, result directories, local SQLite, API keys, or `data_anonymized` into the image.

- [x] **Step 2: Add entrypoint checks**

`entrypoint.sh` must:

```text
validate SECRL_MASTER_KEY and initialization password
create /data with safe ownership
run alembic upgrade head
create the first local admin only when no user exists
start the API and one runner child
forward SIGTERM and wait for both children
```

- [x] **Step 3: Define Compose profiles**

Publish Web as `127.0.0.1:${SECRL_PORT:-8080}:8080`. Add `smoke`, one profile per Incident, `secrl-all`, and `agent-service-reference`. MySQL services have no host ports, use read-only source mounts during init, named data volumes, health checks, resource limits, and pinned multi-architecture image digests.

- [x] **Step 4: Add environment example**

`.env.example` contains names and safe defaults only:

```text
SECRL_PORT=8080
SECRL_DATA_DIR=./.secrl-lite-data
SECRL_MASTER_KEY=
SECRL_SESSION_SECRET=
SECRL_INITIAL_ADMIN_PASSWORD=
SECRL_DATA_ANONYMIZED_DIR=
```

- [x] **Step 5: Build and smoke test**

```bash
docker compose --profile smoke build
docker compose --profile smoke up -d
curl --fail http://127.0.0.1:8080/api/v1/health
docker compose --profile smoke down
```

Expected: health is `ok`, Protocol-Smoke dependencies are healthy, and persistent `/data` survives restart.

- [x] **Step 6: Commit**

```bash
git add docker compose.yaml .env.example .gitignore
git commit -m "build: package SecRL Lite with Docker Compose"
```

### Task 22: Add Backup, Restore, And Integrity Commands

**Files:**
- Create: `scripts/lite-backup.sh`
- Create: `scripts/lite-restore.sh`
- Modify: `secrl_platform/cli.py`
- Create: `tests/platform/test_backup_restore.py`

- [x] **Step 1: Write round-trip test**

Create a completed Protocol-Smoke run, back up SQLite using the SQLite online backup API, copy content-addressed artifacts, restore into an empty data directory, and assert identical TaskSpec/RunSpec/artifact hashes.

- [x] **Step 2: Implement backup manifest**

Each backup contains:

```json
{
  "schema_version": 1,
  "created_at": "UTC ISO-8601",
  "database_sha256": "...",
  "artifact_manifest_sha256": "...",
  "platform_version": "..."
}
```

- [x] **Step 3: Implement safe restore**

Restore only into an empty target, verify every hash before replacing the live directory, and refuse a schema version newer than the running platform.

- [x] **Step 4: Run and commit**

```bash
python -m unittest tests.platform.test_backup_restore -v
git add scripts/lite-backup.sh scripts/lite-restore.sh secrl_platform/cli.py tests/platform/test_backup_restore.py
git commit -m "feat: back up and restore Lite platform data"
```

### Task 23: Final Verification And Documentation

**Files:**
- Create: `docs/secrl-lite/installation.md`
- Create: `docs/secrl-lite/agent-service-v1.md`
- Create: `docs/secrl-lite/benchmark-adapter-v1.md`
- Create: `docs/secrl-lite/operations.md`
- Create: `docs/secrl-lite/security.md`
- Modify: `README.md`

- [x] **Step 1: Run all Python tests on Python 3.11**

```bash
python -m unittest discover -s tests -v
```

Expected: all platform and existing failure-analysis tests pass. If the full discovery count differs by checkout, record the exact count in the verification report.

- [x] **Step 2: Run frontend checks**

```bash
npm --prefix web ci
npm --prefix web run lint
npm --prefix web test
npm --prefix web run build
```

Expected: all commands exit 0.

- [x] **Step 3: Run Protocol-Smoke Compose e2e**

Create a model-free task using the deterministic built-in Agent, then repeat with the reference Agent Service. Expected: 12/12 results, identical Action/Observation sequence hashes, verified artifacts, and no MySQL dependency.

- [x] **Step 4: Run one SecRL fixture e2e on Ubuntu arm64**

Start one Incident profile, run the approved small fixture, and compare reward, steps, SQL result hashes, truncation flags, Agent/evaluator usage separation, and failure-analysis output against the frozen baseline.

- [x] **Step 5: Verify browser layout with Playwright**

Capture desktop `1440x900` and mobile `390x844` screenshots for Dashboard, New Evaluation, Run Detail, Analysis Review, and Compare. Assert no horizontal overflow, overlapping text, clipped controls, blank charts, or eagerly loaded full trajectories.

- [x] **Step 6: Verify secret and network boundaries**

Search API responses, logs, SQLite text columns, exported artifacts, and Docker inspect output for the test API key. Expected: zero matches. Verify Agent Service cannot resolve/connect to Incident MySQL and no platform container mounts Docker Socket.

- [x] **Step 7: Write operator documentation**

Document exact install, Incident profile selection, model setup, Agent Service registration, backup/restore, log locations, disk cleanup, upgrade, and migration triggers to the full platform.

- [x] **Step 8: Commit final verification evidence**

```bash
git add docs/secrl-lite README.md tests/fixtures/platform/verification
git commit -m "docs: verify and operate SecRL Lite platform"
```

## 7. Release Gates

The Lite release is accepted only when all gates pass:

1. Protocol-Smoke runs without MySQL or external LLM.
2. Built-in and reference Agent Service produce equivalent deterministic traces.
3. One approved SecRL fixture matches frozen behavior.
4. Pause/recovery never overwrites a prior attempt.
5. API keys and gold are absent from Agent payloads, logs, exports, and public API responses.
6. All artifacts verify by SHA-256 after backup/restore.
7. Different Benchmark revisions cannot be mixed in reward Compare.
8. Linux `amd64` and `arm64` images build; Mac and Windows local smoke instructions are verified.
9. No App, Runner, Agent Service, or Adapter receives Docker Socket.
10. The implementation remains within Lite boundaries: one active Task, SQLite, local artifacts, no public exposure, no code upload.

## 8. Deferred To The Full Platform

Do not add these while executing this plan:

- PostgreSQL, Redis, distributed Scheduler, multiple Workers.
- Public leaderboard snapshots or cross-Benchmark ranking.
- Organization/multi-user RBAC or OIDC.
- Browser-based wheel/Benchmark Adapter uploads.
- S3/MinIO, remote Workers, mTLS service mesh, HA/DR.
- Public internet exposure.

When one of these becomes required, stop extending Lite and execute the migration path in the approved full design.
