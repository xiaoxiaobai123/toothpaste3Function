"""Backward-compatibility layer for the original toothpastefronback protocol.

Existing customers running the original `toothpastefronback` program have a
fixed PLC ladder against fixed register addresses. The new unified protocol
in `plc/manager.py` is incompatible. This subpackage provides a drop-in
adapter so the new binary can replace the old one with **zero PLC changes**:

    config.json -> {"plc_protocol": "legacy_fronback"} -> LegacyFronbackOrchestrator
    config.json -> {"plc_protocol": "v2_unified"} (default) -> TaskManager

The legacy layer is intentionally isolated from the rest of the project —
its register layout, algorithm details, and main-loop shape are frozen to
match the original program. Improvements to the algorithm framework
(processing/registry.py, Processor abstract base, etc.) live in the v2
path; the legacy path stays byte-compatible with what's deployed today.
"""
