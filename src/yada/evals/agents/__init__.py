"""Agent adapters usable by the generic evaluation runner."""

from yada.evals.agents.command import CommandAgentAdapter
from yada.evals.agents.yada import YadaAgentAdapter

__all__ = ["CommandAgentAdapter", "YadaAgentAdapter"]
