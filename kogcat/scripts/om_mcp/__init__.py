"""om MCP wrapper — HTTP dispatch to om-core sidecar."""

__all__ = ["dispatch"]


def __getattr__(name):  # pragma: no cover — convenience re-export
    if name in __all__:
        from . import tools

        return getattr(tools, name)
    raise AttributeError(name)
