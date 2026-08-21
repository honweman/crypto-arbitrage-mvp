"""Web package public API.

Implementation modules own their individual responsibilities. Attribute fallback keeps
the historical import surface compatible while callers migrate to focused modules.
"""

from __future__ import annotations

from typing import Any

from .. import order_reconciliation as _order_reconciliation
from .. import portfolio_metrics as _portfolio_metrics
from .. import web_config as _web_config
from . import assets as _assets
from . import constants as _constants
from . import core as _core
from . import background as _background
from . import security as _security
from . import state as _state
from . import user_scope as _user_scope

_COMPAT_MODULES = (
    _core,
    _assets,
    _constants,
    _security,
    _state,
    _background,
    _user_scope,
    _web_config,
    _portfolio_metrics,
    _order_reconciliation,
)


def __getattr__(name: str) -> Any:
    for module in _COMPAT_MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    names = set(globals())
    for module in _COMPAT_MODULES:
        names.update(dir(module))
    return sorted(names)


create_app = _core.create_app
main = _core.main

__all__ = ["create_app", "main"]
