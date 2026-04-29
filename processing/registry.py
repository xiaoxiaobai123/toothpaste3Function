"""ProductType → Processor lookup.

Adding a new detection algorithm requires only:
    1. New file `processing/<name>.py` with a Processor subclass.
    2. New ProductType value in plc/enums.py.
    3. One line in PROCESSORS below.
    4. One section in docs/PLC_REGISTERS.md describing the +5..+17 layout.

TaskManager imports `dispatch()` exclusively; algorithm details never
leak into the orchestration layer.
"""

from __future__ import annotations

from plc.enums import ProductType
from processing.base import Processor
from processing.brush_head import BrushHeadProcessor

PROCESSORS: dict[ProductType, Processor] = {
    ProductType.BRUSH_HEAD: BrushHeadProcessor(),
    # ProductType.TOOTHPASTE_FRONTBACK → ToothpasteFrontBackProcessor()  (P3)
    # ProductType.HEIGHT_CHECK         → HeightCheckProcessor()          (P3)
}


def dispatch(product_type: ProductType) -> Processor | None:
    """Return the registered processor or None for NONE / unimplemented types."""
    return PROCESSORS.get(product_type)
