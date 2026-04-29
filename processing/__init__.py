"""Detection algorithms and the display pipeline."""

from processing.base import Processor
from processing.registry import PROCESSORS, dispatch
from processing.result import Outcome, ProcessResult

__all__ = ["PROCESSORS", "Outcome", "ProcessResult", "Processor", "dispatch"]
