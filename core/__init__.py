"""Cross-cutting infrastructure: config, logging, version, license, orchestration.

Submodules are imported lazily by callers (`from core import log_config`)
to keep this package side-effect free and to avoid circular imports
between `core.task_manager` and the camera/plc/processing subpackages.
"""
