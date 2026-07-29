from __future__ import annotations

import sys
from types import ModuleType

import pytest

from fakturek.extensions import register_optional_extensions


def test_extension_loader_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FAKTUREK_EXTENSION_MODULE", raising=False)
    register_optional_extensions(object(), marker="unused")


def test_extension_loader_passes_application_context(monkeypatch):
    module = ModuleType("example_fakturek_extension")
    calls: list[tuple[object, dict[str, object]]] = []

    def register(app, *, context):
        calls.append((app, context))

    module.register_fakturek_extension = register  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("FAKTUREK_EXTENSION_MODULE", module.__name__)
    app = object()

    register_optional_extensions(app, marker="ready")

    assert calls == [(app, {"marker": "ready"})]


def test_extension_loader_rejects_invalid_module(monkeypatch):
    module = ModuleType("invalid_fakturek_extension")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("FAKTUREK_EXTENSION_MODULE", module.__name__)

    with pytest.raises(RuntimeError, match="register_fakturek_extension"):
        register_optional_extensions(object())
