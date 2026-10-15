"""Compatibility import; implementation lives in adapters.pb_master."""

import sys

from .adapters import pb_master as _implementation

sys.modules[__name__] = _implementation
