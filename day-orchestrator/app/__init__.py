"""Day Orchestrator application package.

Importing `app` is side-effect free: the ADK playground objects (`app.app`,
`app.root_agent`) compose lazily on first attribute access (PEP 562), so
unit tests and CLI commands never trigger a full runtime composition
(Config + DB + secrets + channels).
"""

__all__ = ["app", "root_agent"]  # type: ignore[reportUnsupportedDunderAll]


def __getattr__(name: str):
    if name in ("app", "root_agent"):
        from . import agent

        return getattr(agent, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
