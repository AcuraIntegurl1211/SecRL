from __future__ import annotations

from typing import Any

from secrl_platform.benchmarks.protocol import AdapterCapabilities


class DuplicateBenchmarkError(ValueError):
    pass


class UnknownBenchmarkError(LookupError):
    pass


class AdapterCapabilitiesError(TypeError):
    """Raised when an adapter does not declare valid capabilities."""


class BenchmarkRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, tuple[Any, AdapterCapabilities]] = {}
        self._factories: dict[str, Any] = {}

    def register(self, adapter: Any, *, factory: Any = None) -> None:
        manifest = adapter.manifest()
        key = manifest.benchmark_id
        if key in self._adapters:
            raise DuplicateBenchmarkError(key)
        # Capabilities are validated and frozen at registration so every
        # platform gate can read them without re-deriving behavior from the
        # benchmark id.
        self._adapters[key] = (adapter, _validate_capabilities(adapter))
        if factory is not None:
            self._factories[key] = factory

    def get(self, benchmark_id: str) -> Any:
        entry = self._adapters.get(benchmark_id)
        if entry is None:
            raise UnknownBenchmarkError(benchmark_id)
        return entry[0]

    def create(self, benchmark_id: str, run_limits: dict[str, int] | None = None) -> Any:
        """Build a fresh run-scoped adapter instance.

        Episode state lives on adapter instances, so runs and per-request
        preflight checks must never share one.
        """
        factory = self._factories.get(benchmark_id)
        if factory is None:
            raise UnknownBenchmarkError(benchmark_id)
        return factory(run_limits)

    def capabilities(self, benchmark_id: str) -> AdapterCapabilities:
        entry = self._adapters.get(benchmark_id)
        if entry is None:
            raise UnknownBenchmarkError(benchmark_id)
        return entry[1]

    def ids(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def samples(self) -> tuple[Any, ...]:
        """Registration-time adapters for read-only catalog listing.

        Catalog rendering only reads manifest/dataset metadata, so the frozen
        samples are safe to expose here; never run an episode against them.
        """
        return tuple(adapter for adapter, _caps in self._adapters.values())

    def copy(self) -> "BenchmarkRegistry":
        """A shallow fork carrying the same adapters, capabilities, factories.

        Used to scope extra benchmarks to a single context (or test) without
        mutating the shared builtin registry.
        """
        forked = BenchmarkRegistry()
        forked._adapters = dict(self._adapters)
        forked._factories = dict(self._factories)
        return forked


def _validate_capabilities(adapter: Any) -> AdapterCapabilities:
    capabilities_fn = getattr(adapter, "capabilities", None)
    if not callable(capabilities_fn):
        raise AdapterCapabilitiesError(
            f"benchmark adapter {type(adapter).__name__} does not declare capabilities"
        )
    try:
        capabilities = capabilities_fn()
    except AdapterCapabilitiesError:
        raise
    except Exception as exc:
        raise AdapterCapabilitiesError(
            f"benchmark adapter capabilities are unreadable: {type(exc).__name__}"
        ) from exc
    if not isinstance(capabilities, AdapterCapabilities):
        raise AdapterCapabilitiesError(
            "benchmark adapter capabilities must be an AdapterCapabilities instance"
        )
    return capabilities


def _builtin_registry() -> BenchmarkRegistry:
    from secrl_platform.benchmarks.secrl import SecRLAdapter, SecRLRunSpec
    from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter

    registry = BenchmarkRegistry()
    # Registration order matches the historical catalog order so the benchmark
    # listing keeps its existing shape.
    registry.register(
        ProtocolSmokeAdapter.load_default(),
        factory=lambda run_limits: ProtocolSmokeAdapter.load_default(),
    )
    registry.register(
        SecRLAdapter(),
        factory=lambda run_limits: SecRLAdapter(run_spec=SecRLRunSpec(**(run_limits or {}))),
    )
    return registry


_REGISTRY: BenchmarkRegistry | None = None


def builtin_benchmarks() -> BenchmarkRegistry:
    """Capabilities catalog for the allowlisted benchmarks.

    Adapters held here are registration samples; run and preflight execution
    always builds a fresh instance through ``create`` because episode state
    lives on the adapter.
    """
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _builtin_registry()
    return _REGISTRY


def build_benchmark_adapter(benchmark_id: str, run_limits: dict[str, int] | None) -> Any:
    """Instantiate the allowlisted adapter for a task or preflight request.

    Only the SecRL adapter consumes run limits; adding a benchmark means
    registering a factory here instead of extending benchmark-id branches in
    the API routes.
    """
    return builtin_benchmarks().create(benchmark_id, run_limits)
