"""Plugin model for connectors/extraction strategies/eval metrics (C8).

Real ``importlib.metadata`` entry_points discovery -- the exact mechanism
pytest, flake8, and most of the Python ecosystem use for third-party
plugins -- not a bespoke registration API a customer would have to learn.
A third party ships an installable package that declares, in its own
``pyproject.toml``:

    [project.entry-points."vouchstone.eval_graders"]
    my_grader = "my_package.graders:strict_json_grader"

and it becomes discoverable here with zero code on Vouchstone's side.
In-process registration (``PluginRegistry.register()``) covers the other
real case: a single script or notebook that wants to add one plugin
without publishing a whole installable package.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any


class PluginLoadError(Exception):
    """Raised when a discovered entry point fails to import/load. A
    broken plugin must never be silently dropped from the list -- that
    makes the failure invisible exactly when a customer is debugging why
    their plugin "isn't showing up"."""


class PluginRegistry:
    """A named group of pluggable implementations, combining Python
    entry_points discovery with optional in-process registration."""

    def __init__(self, group: str):
        self.group = group
        self._manual: dict[str, Any] = {}

    def register(self, name: str, obj: Any) -> None:
        self._manual[name] = obj

    def unregister(self, name: str) -> None:
        self._manual.pop(name, None)

    def discover(self) -> dict[str, Any]:
        """Real setuptools/importlib.metadata entry_points discovery.
        Raises PluginLoadError naming the failing entry point if any
        plugin fails to import."""
        discovered: dict[str, Any] = {}
        for ep in importlib.metadata.entry_points(group=self.group):
            try:
                discovered[ep.name] = ep.load()
            except Exception as exc:
                raise PluginLoadError(
                    f"failed to load plugin '{ep.name}' from group '{self.group}': {exc}"
                ) from exc
        return discovered

    def all(self) -> dict[str, Any]:
        """Manual registrations take precedence over entry_point-discovered
        ones of the same name -- an explicit in-process registration is a
        deliberate override, not a conflict to error on."""
        combined = self.discover()
        combined.update(self._manual)
        return combined

    def get(self, name: str) -> Any:
        plugins = self.all()
        if name not in plugins:
            raise KeyError(
                f"no plugin named '{name}' registered in group '{self.group}' "
                f"(available: {sorted(plugins)})"
            )
        return plugins[name]

    def names(self) -> list[str]:
        return sorted(self.all())


# Three well-known registries. Built-in implementations are manually
# pre-registered so the registry has real members from the start, not
# just an empty extension point -- third parties add to the same
# namespace their code already imports from.
ENGINE_ADAPTERS = PluginRegistry("vouchstone.engine_adapters")
EXTRACTION_STRATEGIES = PluginRegistry("vouchstone.extraction_strategies")
EVAL_GRADERS = PluginRegistry("vouchstone.eval_graders")


def _register_builtins() -> None:
    from .eval import default_grader
    from .forge import ClaudeEngineAdapter, EchoEngineAdapter, OpenCodeEngineAdapter

    ENGINE_ADAPTERS.register("claude", ClaudeEngineAdapter)
    ENGINE_ADAPTERS.register("echo", EchoEngineAdapter)
    # Default Forge engine as of Phase 5 -- see forge.get_default_engine_adapter().
    ENGINE_ADAPTERS.register("opencode", OpenCodeEngineAdapter)
    try:
        from .transform import TemplateEngineAdapter
        ENGINE_ADAPTERS.register("template", TemplateEngineAdapter)
    except ImportError:  # pragma: no cover -- transform.py has no optional deps today
        pass

    EVAL_GRADERS.register("default", default_grader)


_register_builtins()
