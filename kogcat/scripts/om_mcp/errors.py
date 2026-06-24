"""Wrapper-specific exceptions. Each carries a SCREAMING_SNAKE ``code``."""
from __future__ import annotations


class OmWrapperError(Exception):
    """Base for wrapper-side failures (binary not found, sidecar dead, ...)."""

    code: str = "OM_WRAPPER_ERROR"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.hint = hint
        super().__init__(message + (f" — {hint}" if hint else ""))


class OmCoreBinNotFound(OmWrapperError):
    code = "OM_CORE_BIN_NOT_FOUND"


class OmServerStartupTimeout(OmWrapperError):
    code = "OM_SERVER_STARTUP_TIMEOUT"


class OmSidecarUnhealthy(OmWrapperError):
    """Sidecar binds but ``/v1/capabilities`` fails after bounded recovery.

    Distinct from ``OmServerStartupTimeout``: a process exists and listens,
    but the HTTP layer is unhealthy even after respawn.
    """

    code = "OM_SIDECAR_UNHEALTHY"


class OmVersionMismatch(OmWrapperError):
    code = "OM_VERSION_MISMATCH"

    def __init__(
        self,
        caps: dict,
        required_series: tuple[int, int],
        required_minor: int,
    ) -> None:
        self.caps = caps
        self.required_series = required_series
        self.required_minor = required_minor
        series_str = f"{required_series[0]}.{required_series[1]}.x"
        super().__init__(
            f"om-core binary_version={caps.get('binary_version')!r} "
            f"api_minor={caps.get('api_minor')} but wrapper requires "
            f"series >= {series_str} with api_minor >= {required_minor} "
            f"within that series",
            hint=(
                "Run tools/pull-om-core.sh to upgrade the bundled binary, "
                "or set OM_CORE_BIN to a newer build."
            ),
        )


class OmSidecarUnavailable(OmWrapperError):
    """Sidecar not running; wrapper not allowed to spawn one.

    Supervised mode (default) relies on the OS supervisor. Direct spawn
    requires ``OM_ALLOW_DIRECT_SPAWN=1``.
    """

    code = "OM_SIDECAR_UNAVAILABLE"


class OmApiError(Exception):
    """Mirrors the HTTP ``{code, message, hint, details}`` error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        details: dict | None = None,
        status_code: int = 500,
    ) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details
        self.status_code = status_code
        super().__init__(f"{code}: {message}" + (f" — {hint}" if hint else ""))
