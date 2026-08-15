from secrl_platform.benchmarks.protocol import BenchmarkAdapterProtocol


class DuplicateBenchmarkError(ValueError):
    pass


class UnknownBenchmarkError(LookupError):
    pass


class BenchmarkRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, BenchmarkAdapterProtocol] = {}

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
