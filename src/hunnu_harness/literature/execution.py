"""Harness-controlled literature adapter resolution and execution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from ..browser.port import (
    BrowserCommandPort,
    ensure_browser_command_port,
    validate_browser_command_port,
)
from ..browser.transport import BrowserTransport
from ..paths import require_output_path
from .adapters.base import LiteratureSourceAdapter


class AdapterResolutionError(ValueError):
    """Raised when a literature plan cannot resolve to a registered adapter."""


class AdapterIdentityError(AdapterResolutionError):
    """Raised when a plan and adapter type/name do not agree."""


class LiteratureAdapterFactory:
    """Resolve and construct adapters from one source registry.

    The factory accepts a registry as a dependency so tests and future source
    packages can register types without adding source-specific branches to the
    execution broker.  Production Agent routing passes the existing
    ``LITERATURE_ADAPTER_REGISTRY``.
    """

    def __init__(self, registry: Mapping[str, type[LiteratureSourceAdapter]]) -> None:
        self._registry = dict(registry)

    @property
    def registry(self) -> Mapping[str, type[LiteratureSourceAdapter]]:
        return self._registry

    @staticmethod
    def _normalize_source(source: str) -> str:
        return str(source).strip()

    def resolve_type(self, source: str) -> type[LiteratureSourceAdapter]:
        canonical_source = self._normalize_source(source)
        adapter_type = self._registry.get(canonical_source)
        if adapter_type is None:
            raise AdapterResolutionError(
                f"No literature adapter is registered for source {canonical_source!r}"
            )
        if not isinstance(adapter_type, type) or not issubclass(adapter_type, LiteratureSourceAdapter):
            raise AdapterResolutionError(
                f"Registered literature adapter for {canonical_source!r} is not a LiteratureSourceAdapter"
            )
        declared_name = getattr(adapter_type, "name", None)
        if declared_name != canonical_source:
            raise AdapterIdentityError(
                "Registered literature adapter identity mismatch: "
                f"source={canonical_source!r}, adapter_name={declared_name!r}"
            )
        return adapter_type

    def validate_instance(
        self,
        source: str,
        adapter: LiteratureSourceAdapter,
        *,
        browser: BrowserCommandPort | BrowserTransport | None = None,
    ) -> LiteratureSourceAdapter:
        """Validate a legacy adapter injection against the exact registry type.

        Subclasses are not accepted through ``adapter=``; extensibility must
        be explicit in the registry rather than implicit at the compatibility
        boundary.
        """
        canonical_source = self._normalize_source(source)
        expected_type = self.resolve_type(canonical_source)
        if type(adapter) is not expected_type:
            raise AdapterIdentityError(
                "Literature adapter does not match the plan source: "
                f"source={canonical_source!r}, expected={expected_type.__name__}, "
                f"received={type(adapter).__name__}"
            )
        if getattr(adapter, "name", None) != canonical_source:
            raise AdapterIdentityError(
                "Literature adapter name "
                f"{getattr(adapter, 'name', None)!r} does not match source {canonical_source!r}"
            )

        adapter_browser = validate_browser_command_port(getattr(adapter, "browser", None))
        if browser is not None:
            request_browser = ensure_browser_command_port(browser)
            same_binding = adapter_browser is request_browser
            if not same_binding:
                bound_to = getattr(adapter_browser, "bound_to", None)
                same_binding = callable(bound_to) and bool(bound_to(browser))
            if not same_binding:
                raise AdapterIdentityError(
                    "Caller-supplied adapter is bound to a different BrowserCommandPort "
                    "than the execution request"
                )
        return adapter

    def create(
        self,
        source: str,
        *,
        browser: BrowserCommandPort | BrowserTransport | None,
        adapter_kwargs: Mapping[str, Any] | None = None,
    ) -> LiteratureSourceAdapter:
        canonical_source = self._normalize_source(source)
        adapter_type = self.resolve_type(canonical_source)
        transport = ensure_browser_command_port(browser)
        try:
            adapter = adapter_type(transport, **dict(adapter_kwargs or {}))
        except Exception as exc:
            raise AdapterResolutionError(
                "Could not construct registered literature adapter for "
                f"source {canonical_source!r}: {type(exc).__name__}"
            ) from exc
        return self.validate_instance(canonical_source, adapter, browser=transport)


class AdapterExecutionBroker:
    """Make adapter resolution the only literature workflow entry point."""

    def __init__(self, factory: LiteratureAdapterFactory) -> None:
        self.factory = factory

    async def execute(
        self,
        plan: Any,
        *,
        browser: BrowserCommandPort | BrowserTransport | None,
        adapter: LiteratureSourceAdapter | None,
        run_root: Path,
        workflow_factory: Callable[..., Any],
        institutional_resolver: Any = None,
        institutional_trigger: Any = None,
    ) -> Any:
        """Resolve/validate an adapter, then delegate to the existing workflow.

        ``adapter`` is retained only as a compatibility injection point.  It
        is never trusted based on caller intent: the factory validates both its
        registered type and its bound transport before workflow construction.
        If no adapter is supplied, ``browser`` is required and the factory
        instantiates the type selected by ``plan.source``.
        """

        if adapter is None:
            resolved = self.factory.create(plan.source, browser=browser)
        else:
            resolved = self.factory.validate_instance(
                plan.source,
                adapter,
                browser=browser,
            )

        workflow = workflow_factory(
            resolved,
            run_root=require_output_path(Path(run_root), label="Agent literature run"),
            institutional_resolver=institutional_resolver,
            institutional_trigger=institutional_trigger,
        )
        return await workflow.run(plan.request)


__all__ = [
    "AdapterExecutionBroker",
    "AdapterIdentityError",
    "AdapterResolutionError",
    "LiteratureAdapterFactory",
]
