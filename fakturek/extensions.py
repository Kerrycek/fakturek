from __future__ import annotations

import importlib
import os
from typing import Any


def register_optional_extensions(app: Any, **context: Any) -> None:
    """Register one explicitly configured deployment extension.

    The core never imports an extension unless FAKTUREK_EXTENSION_MODULE is set.
    The configured module must expose ``register_fakturek_extension``.
    """

    module_name = os.getenv("FAKTUREK_EXTENSION_MODULE", "").strip()
    if not module_name:
        return

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise RuntimeError(
                f"Configured Fakturek extension is not installed: {module_name}"
            ) from exc
        raise

    register = getattr(module, "register_fakturek_extension", None)
    if not callable(register):
        raise RuntimeError(
            f"Configured Fakturek extension {module_name} does not expose "
            "register_fakturek_extension"
        )

    register(app, context=context)
