"""Per-project collectors and their registry."""

from dispatcher.core.collectors.arbiter import ArbiterCollector
from dispatcher.core.collectors.atp import AtpCollector
from dispatcher.core.collectors.base import CollectContext, Collector
from dispatcher.core.collectors.maestro import MaestroCollector
from dispatcher.core.collectors.proctor import ProctorCollector
from dispatcher.core.collectors.spec_runner import SpecRunnerCollector

COLLECTORS: list[Collector] = [
    AtpCollector(),
    MaestroCollector(),
    ArbiterCollector(),
    SpecRunnerCollector(),
    ProctorCollector(),
]

__all__ = ["COLLECTORS", "CollectContext", "Collector"]
