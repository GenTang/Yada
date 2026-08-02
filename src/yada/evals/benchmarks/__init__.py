"""Benchmark adapters shipped with Yada."""

from yada.evals.benchmarks.local import LocalBenchmark
from yada.evals.benchmarks.swebench import SWEbenchBenchmark

__all__ = ["LocalBenchmark", "SWEbenchBenchmark"]
