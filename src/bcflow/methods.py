"""Methods name registry for bias-correction pipeline.
This is a separate module so that the registry can be imported by other modules
without importing the entire bias-correction pipeline (which would create a circular import).
"""

from __future__ import annotations

import xsdba

from .discovery import METHOD_METADATA

METHOD_REGISTRY = {
    name: getattr(xsdba, class_name) for name, class_name, _summary in METHOD_METADATA
}
