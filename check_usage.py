#!/usr/bin/env python3
"""Aggregate real-time quota usage across Claude Code, OpenAI Codex and Google Antigravity.

The script is intentionally dependency-light: ``requests`` is used when available and
the standard library ``urllib`` is used otherwise, so the same file runs unchanged on a
GitHub Actions runner, a laptop, or an Android Termux shell.

Every provider is implemented as a :class:`QuotaResolver` subclass.  A resolver never
raises: any authentication, network, HTTP or parsing problem is captured and turned into
a degraded :class:`ProviderReport`, so one broken provider can never take down the run.

Usage::

    python check_usage.py                    # print the ASCII summary
    python check_usage.py --json             # machine readable output
    python check_usage.py --only claude      # single provider
    python check_usage.py --github-summary   # also append to $GITHUB_STEP_SUMMARY
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

__version__ = "1.0.0"

# --------------------------------------------------------------------------------------
# Optional third-party dependencies.  Each is probed once and degraded gracefully.
# --------------------------------------------------------------------------------------

def safe_import(module_name: str, attribute: Optional[str] = None) -> Any:
    """Import an optional dependency, returning ``None`` if it is unusable.

    A plain ``except ImportError`` is not enough here.  A half-installed native
    extension can fail with almost anything — and ``pyo3``-backed wheels (which
    ``cryptography``, a transitive dependency of ``google-auth``, uses) raise
    ``pyo3_runtime.PanicException``, a *BaseException* subclass that sails straight
    through ``except Exception``.  A broken optional dependency must degrade one
    provider, never abort the run, so everything except genuine interpreter-control
    exceptions is swallowed.
    """
    try:
        module = __import__(module_name, fromlist=["*"] if attribute else [])
        for part in ([] if attribute else module_name.split(".")[1:]):
            module = getattr(module, part)
        return getattr(module, attribute) if attribute else module
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - see docstring
        return None


_requests = safe_import("requests")
_load_dotenv = safe_import("dotenv", "load_dotenv")


# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

DEFAULT_TIMEOUT = 10.0
"""Per-request timeout in seconds (the task envelope allows 8-12s)."""

MIN_TIMEOUT = 3.0
MAX_TIMEOUT = 60.0

SUMMARY_WIDTH = 65
SUMMARY_TITLE = "AI ASSISTANT QUOTA SUMMARY"

ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
# Public client id used by the Claude Code CLI for its PKCE OAuth flow.  It is not a
# secret (it ships inside the CLI); override with CLAUDE_OAUTH_CLIENT_ID if it changes.
ANTHROPIC_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"
ANTHROPIC_API_VERSION = "2023-06-01"

OPENAI_API_BASE = "https://api.openai.com"

GCP_SERVICE_USAGE_BASE = "https://serviceusage.googleapis.com/v1"
GCP_MONITORING_BASE = "https://monitoring.googleapis.com/v3"
GCP_SCOPE_READONLY = "https://www.googleapis.com/auth/cloud-platform.read-only"
GCP_SCOPE_FULL = "https://www.googleapis.com/auth/cloud-platform"
AIPLATFORM_SERVICE = "aiplatform.googleapis.com"

USER_AGENT = f"usage-puller/{__version__} (+https://github.com/flavio-mercati/usage-puller)"

CLAUDE_CREDENTIAL_PATHS: Tuple[str, ...] = (
    "~/.claude/.credentials.json",
    "~/.config/claude/.credentials.json",
    "~/.claude/credentials.json",
)

CODEX_CREDENTIAL_PATHS: Tuple[str, ...] = (
    "~/.codex/auth.json",
    "~/.codex/config.json",
    "~/.config/codex/auth.json",
    "~/.openai/auth.json",
)

GCP_ADC_PATHS: Tuple[str, ...] = (
    "~/.config/gcloud/application_default_credentials.json",
    "~/AppData/Roaming/gcloud/application_default_credentials.json",
)

ANTIGRAVITY_CONFIG_PATHS: Tuple[str, ...] = (
    "~/.antigravity/config.json",
    "~/.config/antigravity/config.json",
    "~/.agy/config.json",
    "~/.gemini/settings.json",
)

# Status vocabulary used by resolvers and the formatter.
STATUS_OK = "OK"
STATUS_UNCONFIGURED = "UNCONFIGURED"
STATUS_ERROR = "ERROR"
STATUS_PARTIAL = "PARTIAL"
STATUS_SKIPPED = "SKIPPED"


# --------------------------------------------------------------------------------------
# Data models
# --------------------------------------------------------------------------------------


@dataclass
class Window:
    """A single rolling quota window (5-hour session, 7-day cap, sub-pool, ...)."""

    label: str
    percent: Optional[float] = None
    resets_at: Optional[datetime] = None
    used: Optional[float] = None
    limit: Optional[float] = None
    unit: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return self.percent is None and self.used is None and self.resets_at is None

    def render(self, *, with_reset: bool = True) -> str:
        """Render as ``label: 24% (resets 2h 10m)``."""
        parts: List[str] = []
        if self.percent is not None:
            parts.append(format_percent(self.percent))
        elif self.used is not None and self.limit:
            parts.append(format_percent(100.0 * self.used / self.limit))
        elif self.used is not None:
            parts.append(f"{self.used:g}{self.unit or ''}")
        else:
            parts.append("n/a")
        if with_reset and self.resets_at is not None:
            remaining = format_timedelta(self.resets_at - utcnow())
            if remaining:
                parts.append(f"(resets {remaining})")
        return f"{self.label}: " + " ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "percent": round(self.percent, 2) if self.percent is not None else None,
            "resets_at": iso(self.resets_at),
            "resets_in_seconds": (
                max(0, int((self.resets_at - utcnow()).total_seconds()))
                if self.resets_at is not None
                else None
            ),
            "used": self.used,
            "limit": self.limit,
            "unit": self.unit,
        }


@dataclass
class ProviderReport:
    """Normalised result for one provider."""

    provider: str
    label: str
    status: str = STATUS_OK
    windows: List[Window] = field(default_factory=list)
    facts: List[Tuple[str, str]] = field(default_factory=list)
    message: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    source: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None
    elapsed_ms: Optional[int] = None

    # -- constructors ------------------------------------------------------------------

    @classmethod
    def unconfigured(cls, provider: str, label: str, message: str) -> "ProviderReport":
        return cls(provider=provider, label=label, status=STATUS_UNCONFIGURED, message=message)

    @classmethod
    def error(cls, provider: str, label: str, message: str) -> "ProviderReport":
        return cls(provider=provider, label=label, status=STATUS_ERROR, message=message)

    @classmethod
    def skipped(cls, provider: str, label: str) -> "ProviderReport":
        return cls(provider=provider, label=label, status=STATUS_SKIPPED, message="not selected")

    # -- rendering ---------------------------------------------------------------------

    def summary_fields(self) -> List[str]:
        """Ordered ``key: value`` fragments joined by ``|`` in the ASCII summary."""
        if self.status in (STATUS_UNCONFIGURED, STATUS_ERROR, STATUS_SKIPPED):
            return [f"Status: {self.status} ({self.message or 'no detail'})"]
        fields = [w.render() for w in self.windows if not w.is_empty]
        fields.extend(f"{key}: {value}" for key, value in self.facts)
        if self.status == STATUS_PARTIAL and self.message:
            fields.append(f"Note: {self.message}")
        return fields or [f"Status: {STATUS_PARTIAL} (no quota fields returned)"]

    @property
    def healthy(self) -> bool:
        return self.status in (STATUS_OK, STATUS_PARTIAL)

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "provider": self.provider,
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "source": self.source,
            "elapsed_ms": self.elapsed_ms,
            "windows": [w.to_dict() for w in self.windows],
            "facts": dict(self.facts),
            "notes": list(self.notes),
        }
        if include_raw and self.raw is not None:
            payload["raw"] = self.raw
        return payload


# --------------------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def env(*names: str, default: Optional[str] = None) -> Optional[str]:
    """First non-empty environment variable among ``names``."""
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    return default


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def format_percent(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    value = max(0.0, min(100.0, float(value)))
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}%"
    return f"{value:.1f}%"


def format_timedelta(delta: timedelta) -> str:
    """``2h 10m`` / ``45m`` / ``<1m`` / ``3d 4h``.  Empty string when already elapsed."""
    total = int(delta.total_seconds())
    if total <= 0:
        return "now"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def format_money(value: Optional[float], currency: str = "$") -> str:
    if value is None:
        return "n/a"
    return f"{currency}{value:,.2f}"


def coerce_percent(value: Any, key: str = "") -> Optional[float]:
    """Normalise a quota figure to a 0-100 percentage.

    Anthropic/OpenAI report integer percentages for utilisation fields, while some
    payloads expose 0..1 ratios.  A value is only treated as a ratio when the field name
    says so (``*_ratio``, ``*_fraction``) so a genuine "1%" is never inflated to 100%.
    """
    number = coerce_float(value)
    if number is None:
        return None
    lowered = key.lower()
    if ("ratio" in lowered or "fraction" in lowered) and 0.0 <= number <= 1.0:
        number *= 100.0
    return max(0.0, min(100.0, number))


def coerce_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%").replace(",", "").replace("$", "")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse ISO-8601 strings and epoch seconds/milliseconds into aware datetimes."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        if seconds <= 0:
            return None
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_timestamp(int(text))
        normalised = text.replace("Z", "+00:00")
        # Trim fractional seconds longer than 6 digits (Go/Java emit 9).
        if "." in normalised:
            head, _, tail = normalised.partition(".")
            digits = "".join(ch for ch in tail if ch.isdigit())
            offset = tail[len(digits):]
            normalised = f"{head}.{digits[:6]}{offset}" if digits else head + offset
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def walk_json(
    node: Any, path: Tuple[str, ...] = (), max_depth: int = 8
) -> Iterator[Tuple[Tuple[str, ...], str, Any]]:
    """Yield ``(ancestor_path, key, value)`` for every mapping entry in a structure.

    The ancestor path lets callers prefer shallow matches and reject matches nested
    inside containers they do not want (e.g. a top-level ``7d`` window must not be
    satisfied by the ``seven_day`` field of a per-model sub-pool).
    """
    if len(path) > max_depth:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, str(key), value
            yield from walk_json(value, path + (str(key),), max_depth)
    elif isinstance(node, list):
        for index, item in enumerate(node[:64]):
            yield from walk_json(item, path + (f"[{index}]",), max_depth)


def first_key(node: Any, aliases: Sequence[str]) -> Optional[Any]:
    """Find the first value whose key matches one of ``aliases`` (case/underscore free)."""
    wanted = {normalise_key(a) for a in aliases}
    for _path, key, value in walk_json(node):
        if normalise_key(key) in wanted:
            return value
    return None


def normalise_key(key: str) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def dig(node: Any, *path: str) -> Any:
    """Safe nested ``dict``/``list`` access; returns ``None`` on any miss."""
    current = node
    for step in path:
        if isinstance(current, dict) and step in current:
            current = current[step]
        else:
            return None
    return current


def read_json_file(path: Path) -> Optional[Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def expand(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser()


def existing_paths(candidates: Iterable[str]) -> List[Path]:
    found: List[Path] = []
    for candidate in candidates:
        resolved = expand(candidate)
        if resolved.is_file():
            found.append(resolved)
    return found


def redact(secret: Optional[str], keep: int = 6) -> str:
    if not secret:
        return "<empty>"
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return f"{secret[:keep]}...{secret[-4:]} ({len(secret)} chars)"


def decode_maybe_base64(value: str) -> str:
    """Return ``value`` decoded from base64 when it clearly is base64-wrapped JSON."""
    stripped = value.strip()
    if stripped.startswith("{"):
        return stripped
    compact = "".join(stripped.split())
    try:
        decoded = base64.b64decode(compact, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return stripped
    return decoded if decoded.lstrip().startswith("{") else stripped


# --------------------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------------------


class CaseInsensitiveDict(Dict[str, str]):
    """Minimal case-insensitive mapping for HTTP response headers."""

    def __init__(self, data: Optional[Iterable[Tuple[str, str]]] = None) -> None:
        super().__init__()
        for key, value in data or ():
            self[key] = value

    def __setitem__(self, key: str, value: str) -> None:  # noqa: D105
        super().__setitem__(str(key).lower(), value)

    def __getitem__(self, key: str) -> str:  # noqa: D105
        return super().__getitem__(str(key).lower())

    def get(self, key: str, default: Any = None) -> Any:  # noqa: D102
        return super().get(str(key).lower(), default)

    def __contains__(self, key: object) -> bool:  # noqa: D105
        return super().__contains__(str(key).lower())


class HttpError(RuntimeError):
    """Raised for non-2xx responses and transport failures."""

    def __init__(self, message: str, *, status: Optional[int] = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    def short(self) -> str:
        detail = self.body.strip().replace("\n", " ")
        if len(detail) > 120:
            detail = detail[:117] + "..."
        if self.status and detail:
            return f"HTTP {self.status}: {detail}"
        if self.status:
            return f"HTTP {self.status}"
        return str(self)


@dataclass
class HttpResponse:
    status: int
    headers: CaseInsensitiveDict
    text: str
    url: str

    def json(self) -> Any:
        try:
            return json.loads(self.text) if self.text.strip() else {}
        except ValueError as exc:
            raise HttpError(
                f"invalid JSON from {self.url}: {exc}", status=self.status, body=self.text[:200]
            ) from exc


class HttpClient:
    """Thin HTTP wrapper: uses ``requests`` when installed, ``urllib`` otherwise."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, verbose: bool = False) -> None:
        self.timeout = max(MIN_TIMEOUT, min(MAX_TIMEOUT, float(timeout)))
        self.verbose = verbose
        self._session = _requests.Session() if _requests is not None else None

    # -- public API --------------------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("POST", url, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        form_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        allow_status: Sequence[int] = (),
    ) -> HttpResponse:
        """Perform a request, raising :class:`HttpError` unless the status is 2xx.

        ``allow_status`` lists extra status codes returned to the caller instead of
        raising, which lets resolvers inspect 401/403/404 bodies to build a useful
        message rather than a bare stack trace.
        """
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        merged.update(headers or {})
        if params:
            query = urllib_parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True
            )
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        effective_timeout = max(MIN_TIMEOUT, min(MAX_TIMEOUT, float(timeout or self.timeout)))
        self._log(f"-> {method} {url}")

        try:
            if self._session is not None:
                response = self._request_requests(
                    method, url, merged, json_body, form_body, effective_timeout
                )
            else:
                response = self._request_urllib(
                    method, url, merged, json_body, form_body, effective_timeout
                )
        except HttpError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors are provider-specific
            raise HttpError(f"{type(exc).__name__}: {exc}") from exc

        self._log(f"<- {response.status} {url} ({len(response.text)} bytes)")
        if 200 <= response.status < 300 or response.status in allow_status:
            return response
        raise HttpError(
            f"request to {url} failed", status=response.status, body=response.text[:500]
        )

    # -- backends ----------------------------------------------------------------------

    def _request_requests(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        json_body: Optional[Any],
        form_body: Optional[Dict[str, Any]],
        timeout: float,
    ) -> HttpResponse:
        assert self._session is not None
        response = self._session.request(
            method,
            url,
            headers=headers,
            json=json_body,
            data=form_body,
            timeout=timeout,
        )
        return HttpResponse(
            status=response.status_code,
            headers=CaseInsensitiveDict(response.headers.items()),
            text=response.text or "",
            url=url,
        )

    def _request_urllib(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        json_body: Optional[Any],
        form_body: Optional[Dict[str, Any]],
        timeout: float,
    ) -> HttpResponse:
        data: Optional[bytes] = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif form_body is not None:
            data = urllib_parse.urlencode(form_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        request = urllib_request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib_request.urlopen(request, timeout=timeout) as raw:  # noqa: S310
                body = raw.read().decode("utf-8", errors="replace")
                return HttpResponse(
                    status=raw.status,
                    headers=CaseInsensitiveDict(raw.headers.items()),
                    text=body,
                    url=url,
                )
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return HttpResponse(
                status=exc.code,
                headers=CaseInsensitiveDict((exc.headers or {}).items()),
                text=body,
                url=url,
            )

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[http] {message}", file=sys.stderr)


# --------------------------------------------------------------------------------------
# Resolver base class
# --------------------------------------------------------------------------------------


class QuotaResolver(ABC):
    """Base class for provider resolvers.

    Subclasses implement :meth:`fetch`; :meth:`resolve` wraps it so that no exception
    can escape into the aggregate run.
    """

    provider: str = "provider"
    label: str = "Provider"

    def __init__(self, http: HttpClient, verbose: bool = False) -> None:
        self.http = http
        self.verbose = verbose

    @abstractmethod
    def fetch(self) -> ProviderReport:
        """Return a report, or raise :class:`ConfigurationError` / :class:`HttpError`."""

    def resolve(self) -> ProviderReport:
        started = time.monotonic()
        try:
            report = self.fetch()
        except ConfigurationError as exc:
            report = ProviderReport.unconfigured(self.provider, self.label, str(exc))
        except HttpError as exc:
            report = ProviderReport.error(self.provider, self.label, exc.short())
        except Exception as exc:  # noqa: BLE001 - last line of defence, never crash
            report = ProviderReport.error(
                self.provider, self.label, f"{type(exc).__name__}: {exc}"
            )
        report.elapsed_ms = int((time.monotonic() - started) * 1000)
        return report

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[{self.provider}] {message}", file=sys.stderr)


class ConfigurationError(RuntimeError):
    """Raised when a provider has no usable credentials (a soft, expected failure)."""


# --------------------------------------------------------------------------------------
# Claude Code resolver
# --------------------------------------------------------------------------------------


@dataclass
class ClaudeCredentials:
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    subscription: Optional[str] = None
    source: str = "unknown"

    @property
    def access_token_valid(self) -> bool:
        if not self.access_token:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at - utcnow() > timedelta(seconds=120)


class ClaudeResolver(QuotaResolver):
    """Claude Code quota via the CLI's own OAuth token.

    Browser session cookies are deliberately avoided: they are short-lived and the
    claude.ai edge applies bot challenges to them.  The OAuth access token that the CLI
    stores in ``~/.claude/.credentials.json`` (or a refresh token supplied through
    ``CLAUDE_REFRESH_TOKEN``) talks to ``api.anthropic.com`` directly, which is a plain
    JSON API with no challenge layer.
    """

    provider = "claude"
    label = "Claude Code"

    # Aliases cover the naming variants Anthropic has shipped for these windows.
    FIVE_HOUR_ALIASES = (
        "five_hour",
        "fiveHour",
        "5h",
        "session",
        "rolling_5h",
        "five_hour_limit",
    )
    SEVEN_DAY_ALIASES = (
        "seven_day",
        "sevenDay",
        "7d",
        "weekly",
        "rolling_7d",
        "seven_day_limit",
    )
    PERCENT_ALIASES = (
        "utilization",
        "utilisation",
        "used_percent",
        "percent_used",
        "usage_percent",
        "percentage",
        "percent",
        "used_pct",
        # Ratio-named variants are rescaled from 0..1 by coerce_percent().
        "usage_ratio",
        "utilization_ratio",
        "used_ratio",
    )
    # A generic 5h/7d lookup must not be satisfied by a value living inside one of
    # these per-model containers — those belong to the sub-pool lookup instead.
    SUB_POOL_CONTAINERS = (
        "model_limits",
        "per_model",
        "sub_pools",
        "subPools",
        "models",
        "model_usage",
    )
    RESET_ALIASES = (
        "resets_at",
        "reset_at",
        "resets",
        "reset",
        "next_reset",
        "resets_at_utc",
        "reset_time",
        "expires_at",
    )
    FABLE_HINTS = ("fable",)

    def __init__(self, http: HttpClient, verbose: bool = False) -> None:
        super().__init__(http, verbose)
        self.api_base = (
            env("ANTHROPIC_USAGE_BASE_URL", default=ANTHROPIC_API_BASE) or ANTHROPIC_API_BASE
        )
        self.org_id = env("CLAUDE_ORG_ID", "ANTHROPIC_ORG_ID")
        self.client_id = env("CLAUDE_OAUTH_CLIENT_ID", default=ANTHROPIC_OAUTH_CLIENT_ID)

    # -- credentials -------------------------------------------------------------------

    def load_credentials(self) -> ClaudeCredentials:
        """Resolve credentials from env vars, the CLI credential file, or the keychain."""
        access = env("CLAUDE_ACCESS_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_ACCESS_TOKEN")
        refresh = env("CLAUDE_REFRESH_TOKEN", "ANTHROPIC_REFRESH_TOKEN")
        if access or refresh:
            self.log("using credentials from environment")
            return ClaudeCredentials(
                access_token=access, refresh_token=refresh, source="environment"
            )

        for candidate in self._credential_files():
            payload = read_json_file(candidate)
            creds = self._parse_credential_payload(payload, source=str(candidate))
            if creds is not None:
                self.log(f"using credentials from {candidate}")
                return creds

        keychain = self._read_macos_keychain()
        if keychain is not None:
            self.log("using credentials from macOS keychain")
            return keychain

        raise ConfigurationError(
            "Missing CLAUDE_REFRESH_TOKEN and no ~/.claude/.credentials.json"
        )

    def _credential_files(self) -> List[Path]:
        explicit = env("CLAUDE_CREDENTIALS_PATH")
        candidates = ([explicit] if explicit else []) + list(CLAUDE_CREDENTIAL_PATHS)
        return existing_paths(candidates)

    def _parse_credential_payload(
        self, payload: Any, source: str
    ) -> Optional[ClaudeCredentials]:
        if not isinstance(payload, dict):
            return None
        blob = payload.get("claudeAiOauth") or payload.get("claude_ai_oauth") or payload
        if not isinstance(blob, dict):
            return None
        access = blob.get("accessToken") or blob.get("access_token")
        refresh = blob.get("refreshToken") or blob.get("refresh_token")
        if not access and not refresh:
            return None
        return ClaudeCredentials(
            access_token=access if isinstance(access, str) else None,
            refresh_token=refresh if isinstance(refresh, str) else None,
            expires_at=parse_timestamp(blob.get("expiresAt") or blob.get("expires_at")),
            subscription=blob.get("subscriptionType") or blob.get("subscription_type"),
            source=source,
        )

    def _read_macos_keychain(self) -> Optional[ClaudeCredentials]:
        if sys.platform != "darwin":
            return None
        try:
            raw = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if raw.returncode != 0 or not raw.stdout.strip():
            return None
        try:
            payload = json.loads(raw.stdout)
        except ValueError:
            return None
        return self._parse_credential_payload(payload, source="macos-keychain")

    def refresh_access_token(self, refresh_token: str) -> str:
        """Exchange a refresh token for a short-lived access token."""
        response = self.http.post(
            ANTHROPIC_OAUTH_TOKEN_URL,
            json_body={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            },
            headers={"Content-Type": "application/json"},
            allow_status=(400, 401, 403),
        )
        payload = response.json() if response.text.strip() else {}
        if response.status >= 400 or not isinstance(payload, dict):
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error_description") or payload.get("error") or "")
            raise HttpError(
                f"token refresh rejected{': ' + detail if detail else ''}",
                status=response.status,
                body=detail or response.text[:200],
            )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise HttpError("token refresh returned no access_token", status=response.status)
        return token

    # -- fetch -------------------------------------------------------------------------

    def fetch(self) -> ProviderReport:
        creds = self.load_credentials()
        if creds.access_token_valid:
            token = creds.access_token
            assert token is not None
        elif creds.refresh_token:
            self.log("access token missing or expired, refreshing")
            token = self.refresh_access_token(creds.refresh_token)
        elif creds.access_token:
            token = creds.access_token  # expired but no refresh token: try anyway
        else:
            raise ConfigurationError("credential store held no usable token")

        payload, endpoint = self._request_usage(token)
        report = self._build_report(payload, endpoint)
        report.notes.append(f"credentials: {creds.source}")
        if creds.subscription:
            report.facts.append(("Plan", str(creds.subscription)))
        return report

    def _request_usage(self, token: str) -> Tuple[Any, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": ANTHROPIC_OAUTH_BETA,
            "anthropic-version": ANTHROPIC_API_VERSION,
        }
        endpoints = [f"{self.api_base}/api/oauth/usage"]
        if self.org_id:
            endpoints.append(f"{self.api_base}/v1/organizations/{self.org_id}/usage")

        last_error: Optional[HttpError] = None
        for endpoint in endpoints:
            try:
                response = self.http.get(endpoint, headers=headers)
            except HttpError as exc:
                self.log(f"{endpoint} -> {exc.short()}")
                last_error = exc
                continue
            payload = response.json()
            if payload:
                return payload, endpoint
            last_error = HttpError(f"empty payload from {endpoint}", status=response.status)
        raise last_error or HttpError("no usage endpoint responded")

    # -- parsing -----------------------------------------------------------------------

    def _build_report(self, payload: Any, endpoint: str) -> ProviderReport:
        report = ProviderReport(provider=self.provider, label=self.label, source=endpoint)
        report.raw = payload if isinstance(payload, dict) else {"payload": payload}

        five_hour = self._window("5h", payload, self.FIVE_HOUR_ALIASES, exclude=self.FABLE_HINTS)
        seven_day = self._window("7d", payload, self.SEVEN_DAY_ALIASES, exclude=self.FABLE_HINTS)
        fable_5h = self._sub_pool_window("Fable 5h", payload, self.FIVE_HOUR_ALIASES)
        fable_7d = self._sub_pool_window("Fable 7d", payload, self.SEVEN_DAY_ALIASES)

        for window in (five_hour, seven_day, fable_5h, fable_7d):
            if window is not None and not window.is_empty:
                # Only the 5-hour session window shows a countdown; four timers in one
                # line stops being copiable.
                if window.label != "5h":
                    window.resets_at = None
                report.windows.append(window)

        extra = self._extra_credits(payload)
        if extra:
            report.facts.append(("Extra credits", extra))

        if not report.windows:
            report.status = STATUS_PARTIAL
            report.message = "usage payload had no recognisable 5h/7d window"
        return report

    def _window(
        self,
        label: str,
        payload: Any,
        aliases: Sequence[str],
        exclude: Sequence[str] = (),
    ) -> Optional[Window]:
        """Locate a window container by key alias and extract percent + reset time.

        Candidates are ranked by nesting depth so the account-wide window always wins
        over an identically named field buried inside a sub-pool.
        """
        wanted = {normalise_key(a) for a in aliases}
        excluded = tuple(e.lower() for e in exclude)
        sub_pool_keys = {normalise_key(c) for c in self.SUB_POOL_CONTAINERS}
        candidates: List[Tuple[int, Window]] = []

        for path, key, value in walk_json(payload):
            if normalise_key(key) not in wanted:
                continue
            if any(hint in key.lower() for hint in excluded):
                continue
            # Reject anything sitting under a per-model container or a Fable-named node.
            ancestors = [normalise_key(step) for step in path]
            if any(step in sub_pool_keys for step in ancestors):
                continue
            if any(hint in step.lower() for step in path for hint in excluded):
                continue
            window = self._window_from_value(label, value)
            if window is not None and not window.is_empty:
                candidates.append((len(path), window))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def _window_from_value(self, label: str, value: Any) -> Optional[Window]:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return Window(label=label, percent=coerce_percent(value))
        if not isinstance(value, dict):
            return None
        percent: Optional[float] = None
        for alias in self.PERCENT_ALIASES:
            for key in value:
                if normalise_key(key) == normalise_key(alias):
                    percent = coerce_percent(value[key], key)
                    break
            if percent is not None:
                break
        used = coerce_float(first_key(value, ("used", "used_tokens", "consumed")))
        limit = coerce_float(first_key(value, ("limit", "max", "total", "cap", "allowance")))
        if percent is None and used is not None and limit:
            percent = max(0.0, min(100.0, 100.0 * used / limit))
        resets_at = None
        for alias in self.RESET_ALIASES:
            for key in value:
                if normalise_key(key) == normalise_key(alias):
                    resets_at = parse_timestamp(value[key])
                    break
            if resets_at is not None:
                break
        return Window(
            label=label, percent=percent, resets_at=resets_at, used=used, limit=limit
        )

    def _sub_pool_window(
        self, label: str, payload: Any, aliases: Sequence[str]
    ) -> Optional[Window]:
        """Find a Claude Fable sub-pool window.

        Two payload shapes are supported: dedicated keys such as ``seven_day_fable`` /
        ``five_hour_fable``, and a collection of per-model objects that identify
        themselves with a ``model``/``name`` field containing ``fable``.
        """
        wanted = {normalise_key(a) for a in aliases}

        # Shape 1: flat keys naming both the model and the window, in either order —
        # e.g. `seven_day_fable`, `fable_5h`, `fableSevenDay`.
        for _path, key, value in walk_json(payload):
            lowered = key.lower()
            if not any(hint in lowered for hint in self.FABLE_HINTS):
                continue
            stripped = normalise_key(lowered.replace("fable", "").replace("claude", ""))
            if stripped in wanted:
                window = self._window_from_value(label, value)
                if window is not None and not window.is_empty:
                    return window

        # Shape 2: containers of per-model pools.
        for container_key in self.SUB_POOL_CONTAINERS:
            container = first_key(payload, (container_key,))
            window = self._sub_pool_from_container(label, container, wanted)
            if window is not None:
                return window
        return None

    def _sub_pool_from_container(
        self, label: str, container: Any, wanted: Iterable[str]
    ) -> Optional[Window]:
        """Scan a per-model pool collection for the Fable entry."""
        entries: List[Any] = []
        if isinstance(container, list):
            entries = list(container)
        elif isinstance(container, dict):
            for key, value in container.items():
                if isinstance(value, dict) and any(
                    hint in str(key).lower() for hint in self.FABLE_HINTS
                ):
                    # Key identifies the model; inject it so the identity check below hits.
                    entries.append({**value, "name": str(key)})
                else:
                    entries.append(value)
        wanted = set(wanted)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identity = " ".join(
                str(entry.get(field_name, "")) for field_name in ("model", "name", "id", "pool")
            ).lower()
            if not any(hint in identity for hint in self.FABLE_HINTS):
                continue
            for key, value in entry.items():
                if normalise_key(key) in wanted:
                    window = self._window_from_value(label, value)
                    if window is not None and not window.is_empty:
                        return window
        return None

    def _extra_credits(self, payload: Any) -> Optional[str]:
        """Render pay-as-you-go / extra-usage credit consumption when present."""
        for alias in ("extra_usage", "extraUsage", "overage", "pay_as_you_go", "credits"):
            block = first_key(payload, (alias,))
            if block is None:
                continue
            if isinstance(block, bool):
                return "ENABLED" if block else "off"
            if isinstance(block, (int, float)):
                return format_money(float(block))
            if not isinstance(block, dict):
                continue
            enabled = block.get("enabled")
            if enabled is False:
                return "off"
            used = coerce_float(
                first_key(block, ("used_usd", "usedUsd", "amount_used", "used", "spent"))
            )
            limit = coerce_float(
                first_key(block, ("limit_usd", "limitUsd", "limit", "cap", "budget"))
            )
            if used is None and limit is None:
                return "ENABLED" if enabled else None
            rendered = format_money(used)
            if limit:
                rendered += f" / {format_money(limit)}"
            return rendered
        return None


# --------------------------------------------------------------------------------------
# OpenAI Codex resolver
# --------------------------------------------------------------------------------------


class CodexResolver(QuotaResolver):
    """OpenAI Codex quota via billing endpoints with a rate-limit-header fallback.

    ``/dashboard/billing/*`` is the authoritative source for spend against a hard limit,
    but it is only reachable by some credential classes.  When it refuses the key, the
    resolver falls back to the ``x-ratelimit-*`` headers on ``/v1/models`` so the run
    still reports a live rolling allowance instead of an error.
    """

    provider = "codex"
    label = "OpenAI Codex"

    def __init__(self, http: HttpClient, verbose: bool = False) -> None:
        super().__init__(http, verbose)
        self.api_base = (
            env("OPENAI_BASE_URL", default=OPENAI_API_BASE) or OPENAI_API_BASE
        ).rstrip("/")
        self.org_id = env("OPENAI_ORG_ID", "OPENAI_ORGANIZATION")
        self.project_id = env("OPENAI_PROJECT_ID")

    # -- credentials -------------------------------------------------------------------

    def load_api_key(self) -> Tuple[str, str]:
        key = env("OPENAI_API_KEY", "CODEX_API_KEY")
        if key:
            return key, "environment"
        for candidate in self._credential_files():
            payload = read_json_file(candidate)
            if not isinstance(payload, dict):
                continue
            found = (
                payload.get("OPENAI_API_KEY")
                or dig(payload, "tokens", "access_token")
                or payload.get("api_key")
                or payload.get("apiKey")
                or dig(payload, "openai", "api_key")
            )
            if isinstance(found, str) and found.strip():
                return found.strip(), str(candidate)
        raise ConfigurationError("Missing OPENAI_API_KEY")

    def _credential_files(self) -> List[Path]:
        explicit = env("CODEX_CONFIG_PATH")
        candidates = ([explicit] if explicit else []) + list(CODEX_CREDENTIAL_PATHS)
        return existing_paths(candidates)

    def _headers(self, key: str) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {key}"}
        if self.org_id:
            headers["OpenAI-Organization"] = self.org_id
        if self.project_id:
            headers["OpenAI-Project"] = self.project_id
        return headers

    # -- fetch -------------------------------------------------------------------------

    def fetch(self) -> ProviderReport:
        key, source = self.load_api_key()
        headers = self._headers(key)
        report = ProviderReport(provider=self.provider, label=self.label, source=self.api_base)
        report.notes.append(f"credentials: {source}")
        report.raw = {}

        subscription = self._safe_json(f"{self.api_base}/dashboard/billing/subscription", headers)
        usage = self._safe_usage(headers)

        hard_limit = None
        plan = None
        if isinstance(subscription, dict):
            report.raw["subscription"] = subscription
            hard_limit = coerce_float(
                subscription.get("hard_limit_usd")
                or subscription.get("system_hard_limit_usd")
                or subscription.get("soft_limit_usd")
            )
            plan = dig(subscription, "plan", "title") or subscription.get("plan_title")

        spent = None
        if isinstance(usage, dict):
            report.raw["usage"] = usage
            total_cents = coerce_float(usage.get("total_usage"))
            if total_cents is not None:
                spent = total_cents / 100.0
            elif coerce_float(usage.get("total_usage_usd")) is not None:
                spent = coerce_float(usage.get("total_usage_usd"))

        if spent is not None and hard_limit:
            report.windows.append(
                Window(label="Limit Used", percent=100.0 * spent / hard_limit)
            )
            report.facts.append(
                ("Spent", f"{format_money(spent)} / {format_money(hard_limit)}")
            )
        elif spent is not None:
            report.facts.append(("Spent (MTD)", format_money(spent)))
        elif hard_limit:
            report.facts.append(("Hard limit", format_money(hard_limit)))

        if plan:
            report.facts.append(("Plan", str(plan)))

        # Rolling request/token allowance from live rate-limit headers.
        limits, limits_note = self._rate_limits(headers)
        if limits:
            report.raw["rate_limits"] = limits
            report.facts.extend(limits.items())

        if not report.windows and not report.facts:
            # Neither billing nor the rate-limit probe yielded anything: the key is
            # present but nothing could be read, which is a failure rather than a
            # partial result.
            report.status = STATUS_ERROR
            report.message = limits_note or "no billing or rate-limit data readable"
        elif not report.windows:
            report.status = STATUS_PARTIAL
            report.message = limits_note or "billing limits not exposed to this key"
        return report

    def _safe_json(self, url: str, headers: Dict[str, str]) -> Optional[Any]:
        """GET a URL, returning ``None`` on any auth/format failure instead of raising."""
        try:
            response = self.http.get(url, headers=headers, allow_status=(400, 401, 403, 404, 429))
        except HttpError as exc:
            self.log(f"{url} -> {exc.short()}")
            return None
        if response.status >= 400:
            self.log(f"{url} -> HTTP {response.status}")
            return None
        try:
            payload = response.json()
        except HttpError as exc:
            self.log(f"{url} -> {exc.short()}")
            return None
        return payload

    def _safe_usage(self, headers: Dict[str, str]) -> Optional[Any]:
        today = utcnow().date()
        start = today.replace(day=1)
        end = today + timedelta(days=1)
        url = (
            f"{self.api_base}/dashboard/billing/usage"
            f"?start_date={start.isoformat()}&end_date={end.isoformat()}"
        )
        return self._safe_json(url, headers)

    def _rate_limits(self, headers: Dict[str, str]) -> Tuple[Dict[str, str], Optional[str]]:
        """Read ``x-ratelimit-*`` headers from ``/v1/models``."""
        try:
            response = self.http.get(
                f"{self.api_base}/v1/models", headers=headers, allow_status=(401, 403, 429)
            )
        except HttpError as exc:
            return {}, f"models probe failed ({exc.short()})"
        if response.status == 401:
            return {}, "API key rejected (HTTP 401)"
        if response.status == 403:
            return {}, "API key lacks permission for /v1/models (HTTP 403)"

        found: Dict[str, str] = {}
        limit = coerce_float(response.headers.get("x-ratelimit-limit-requests"))
        remaining = coerce_float(response.headers.get("x-ratelimit-remaining-requests"))
        if limit and remaining is not None:
            used_pct = 100.0 * (limit - remaining) / limit
            found["Requests"] = (
                f"{format_percent(used_pct)} used "
                f"({int(remaining):,}/{int(limit):,} left)"
            )
        reset = response.headers.get("x-ratelimit-reset-requests")
        if reset:
            found["RPM reset"] = str(reset)
        token_limit = coerce_float(response.headers.get("x-ratelimit-limit-tokens"))
        token_remaining = coerce_float(response.headers.get("x-ratelimit-remaining-tokens"))
        if token_limit and token_remaining is not None:
            found["Tokens"] = (
                f"{format_percent(100.0 * (token_limit - token_remaining) / token_limit)} used"
            )
        if response.status == 429:
            found["Throttled"] = "yes (HTTP 429)"
        if not found and response.status < 400:
            return {"Key": "VALID"}, "key valid; no rate-limit headers on /v1/models"
        return found, None


# --------------------------------------------------------------------------------------
# Google Antigravity resolver
# --------------------------------------------------------------------------------------


class AntigravityResolver(QuotaResolver):
    """Google Antigravity (``agy``) quota via GCP Service Usage + Cloud Monitoring.

    Antigravity is backed by Vertex AI, so the observable signals are the enablement
    state of ``aiplatform.googleapis.com`` and the ``serviceruntime`` quota metrics that
    Cloud Monitoring records for it.  The daily tier is derived from consumption unless
    ``AGY_TIER_OVERRIDE`` pins it.
    """

    provider = "antigravity"
    label = "Antigravity"

    QUOTA_USAGE_METRIC = "serviceruntime.googleapis.com/quota/rate/net_usage"
    QUOTA_LIMIT_METRIC = "serviceruntime.googleapis.com/quota/limit"

    def __init__(self, http: HttpClient, verbose: bool = False) -> None:
        super().__init__(http, verbose)
        self.project_id = env("GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT")
        self.tier_override = env("AGY_TIER_OVERRIDE")
        self.lookback_hours = int(coerce_float(env("AGY_LOOKBACK_HOURS", default="24")) or 24)

    # -- credentials -------------------------------------------------------------------

    def _load_service_account(self) -> Optional[Dict[str, Any]]:
        raw = env("GCP_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON")
        if raw:
            candidate = expand(raw)
            if len(raw) < 4096 and candidate.is_file():
                payload = read_json_file(candidate)
                if isinstance(payload, dict):
                    return payload
                raise ConfigurationError(f"GCP_SERVICE_ACCOUNT_JSON path {candidate} is not JSON")
            try:
                payload = json.loads(decode_maybe_base64(raw))
            except ValueError as exc:
                raise ConfigurationError(
                    f"GCP_SERVICE_ACCOUNT_JSON is not valid JSON or base64 JSON ({exc})"
                ) from exc
            if isinstance(payload, dict):
                return payload
        explicit = env("GOOGLE_APPLICATION_CREDENTIALS")
        for candidate in existing_paths(([explicit] if explicit else []) + list(GCP_ADC_PATHS)):
            payload = read_json_file(candidate)
            if isinstance(payload, dict):
                self.log(f"using ADC file {candidate}")
                return payload
        return None

    def _access_token(self, credential_payload: Optional[Dict[str, Any]]) -> Tuple[str, str]:
        """Mint a bearer token via ``google-auth``, or fall back to ``gcloud``."""
        google_auth = safe_import("google.auth")
        auth_requests = safe_import("google.auth.transport.requests")
        service_account = safe_import("google.oauth2.service_account")
        if google_auth is None or auth_requests is None or service_account is None:
            token = self._gcloud_token()
            if token:
                return token, "gcloud-cli"
            raise ConfigurationError(
                "google-auth is unavailable (pip install -r requirements.txt; a broken "
                "`cryptography` wheel also lands here) and `gcloud auth "
                "print-access-token` is not usable"
            )

        scopes = [GCP_SCOPE_READONLY]
        if credential_payload and credential_payload.get("type") == "service_account":
            credentials = service_account.Credentials.from_service_account_info(
                credential_payload, scopes=scopes
            )
            source = f"service account {credential_payload.get('client_email', '?')}"
            if not self.project_id:
                self.project_id = credential_payload.get("project_id")
        else:
            try:
                credentials, adc_project = google_auth.default(scopes=scopes)
            except Exception as exc:  # noqa: BLE001 - google-auth raises many types
                token = self._gcloud_token()
                if token:
                    return token, "gcloud-cli"
                raise ConfigurationError(f"no usable GCP credentials ({exc})") from exc
            source = "application default credentials"
            if not self.project_id and adc_project:
                self.project_id = adc_project

        credentials.refresh(auth_requests.Request())
        if not credentials.token:
            raise ConfigurationError("GCP credential refresh produced no access token")
        return str(credentials.token), source

    def _gcloud_token(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        token = result.stdout.strip()
        return token if result.returncode == 0 and token else None

    # -- fetch -------------------------------------------------------------------------

    def fetch(self) -> ProviderReport:
        credential_payload = self._load_service_account()
        token, source = self._access_token(credential_payload)
        if not self.project_id:
            raise ConfigurationError(
                "Missing GCP_PROJECT_ID (and credentials carried no project_id)"
            )

        headers = {"Authorization": f"Bearer {token}"}
        report = ProviderReport(
            provider=self.provider, label=self.label, source=f"project {self.project_id}"
        )
        report.notes.append(f"credentials: {source}")
        report.raw = {"project_id": self.project_id}

        service_state = self._service_state(headers)
        report.raw["service_state"] = service_state

        percent, quota_detail = self._quota_usage(headers)
        report.raw["quota"] = quota_detail

        tier = self.tier_override or self._derive_tier(percent, service_state)
        report.facts.append(("Daily Tier", tier))
        if percent is not None:
            report.facts.append(
                ("Quota", f"{format_percent(percent)} used (AI Platform: {service_state})")
            )
        else:
            report.facts.append(("Quota", f"n/a (AI Platform: {service_state})"))
            report.status = STATUS_PARTIAL
            report.message = quota_detail.get("note") or "no quota time series returned"
        return report

    def _service_state(self, headers: Dict[str, str]) -> str:
        url = f"{GCP_SERVICE_USAGE_BASE}/projects/{self.project_id}/services/{AIPLATFORM_SERVICE}"
        # The plain REST call is the default: it is one request, whereas the discovery
        # client first downloads a discovery document. Set USAGE_PREFER_GOOGLE_CLIENT=1
        # to route through google-api-python-client instead (e.g. when a corporate proxy
        # is easier to satisfy with the official client).
        payload = (
            self._maybe_googleapiclient_service_state(headers)
            if env_flag("USAGE_PREFER_GOOGLE_CLIENT")
            else None
        )
        if payload is None:
            try:
                response = self.http.get(url, headers=headers, allow_status=(403, 404))
            except HttpError as exc:
                self.log(f"serviceusage -> {exc.short()}")
                return "UNKNOWN"
            if response.status >= 400:
                return "FORBIDDEN" if response.status == 403 else "UNKNOWN"
            try:
                payload = response.json()
            except HttpError:
                return "UNKNOWN"
        state = payload.get("state") if isinstance(payload, dict) else None
        return str(state) if state else "UNKNOWN"

    def _maybe_googleapiclient_service_state(
        self, headers: Dict[str, str]
    ) -> Optional[Dict[str, Any]]:
        """Opt-in path via ``google-api-python-client`` (USAGE_PREFER_GOOGLE_CLIENT=1)."""
        Credentials = safe_import("google.oauth2.credentials", "Credentials")
        build = safe_import("googleapiclient.discovery", "build")
        if Credentials is None or build is None:
            return None
        token = headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token:
            return None
        try:
            service = build(
                "serviceusage",
                "v1",
                credentials=Credentials(token=token),
                cache_discovery=False,
            )
            return service.services().get(
                name=f"projects/{self.project_id}/services/{AIPLATFORM_SERVICE}"
            ).execute()
        except Exception as exc:  # noqa: BLE001 - discovery client raises broadly
            self.log(f"googleapiclient serviceusage failed, falling back to REST ({exc})")
            return None

    def _quota_usage(self, headers: Dict[str, str]) -> Tuple[Optional[float], Dict[str, Any]]:
        """Highest ``usage / limit`` ratio across aiplatform quota metrics."""
        end = utcnow()
        start = end - timedelta(hours=max(1, self.lookback_hours))
        usage = self._time_series(headers, self.QUOTA_USAGE_METRIC, start, end)
        limits = self._time_series(headers, self.QUOTA_LIMIT_METRIC, start, end)
        if usage is None or limits is None:
            return None, {"note": "Cloud Monitoring unavailable or not permitted"}

        limit_by_metric = {
            key: value for key, value in self._peaks(limits).items() if value and value > 0
        }
        usage_by_metric = self._peaks(usage)
        worst: Optional[float] = None
        worst_metric: Optional[str] = None
        for metric, used in usage_by_metric.items():
            limit = limit_by_metric.get(metric)
            if not limit:
                continue
            ratio = 100.0 * used / limit
            if worst is None or ratio > worst:
                worst, worst_metric = ratio, metric
        detail: Dict[str, Any] = {
            "series_count": len(usage_by_metric),
            "lookback_hours": self.lookback_hours,
            "dominant_metric": worst_metric,
        }
        if worst is None:
            detail["note"] = (
                "no matching usage/limit pair in the last "
                f"{self.lookback_hours}h (quota may be untouched)"
            )
            # No consumption recorded is a legitimate 0%, not a failure.
            if usage_by_metric or limit_by_metric:
                return 0.0, detail
            return None, detail
        return min(100.0, worst), detail

    def _time_series(
        self, headers: Dict[str, str], metric: str, start: datetime, end: datetime
    ) -> Optional[List[Dict[str, Any]]]:
        url = f"{GCP_MONITORING_BASE}/projects/{self.project_id}/timeSeries"
        params = {
            "filter": (
                f'metric.type="{metric}" AND resource.labels.service="{AIPLATFORM_SERVICE}"'
            ),
            "interval.startTime": iso(start),
            "interval.endTime": iso(end),
            "view": "FULL",
            "aggregation.alignmentPeriod": "3600s",
            "aggregation.perSeriesAligner": "ALIGN_MAX",
        }
        try:
            response = self.http.get(
                url, headers=headers, params=params, allow_status=(400, 403, 404)
            )
        except HttpError as exc:
            self.log(f"monitoring {metric} -> {exc.short()}")
            return None
        if response.status >= 400:
            self.log(f"monitoring {metric} -> HTTP {response.status}")
            return None
        try:
            payload = response.json()
        except HttpError:
            return None
        series = payload.get("timeSeries") if isinstance(payload, dict) else None
        return series if isinstance(series, list) else []

    @staticmethod
    def _peaks(series: List[Dict[str, Any]]) -> Dict[str, float]:
        """Peak value per quota metric name across all points of each series."""
        peaks: Dict[str, float] = {}
        for entry in series:
            if not isinstance(entry, dict):
                continue
            metric = entry.get("metric")
            labels = metric.get("labels", {}) if isinstance(metric, dict) else {}
            name = str(labels.get("quota_metric") or labels.get("limit_name") or "unknown")
            for point in entry.get("points", []) or []:
                value_block = point.get("value", {}) if isinstance(point, dict) else {}
                value = coerce_float(
                    value_block.get("int64Value")
                    if isinstance(value_block, dict)
                    else None
                )
                if value is None and isinstance(value_block, dict):
                    value = coerce_float(value_block.get("doubleValue"))
                if value is None:
                    continue
                peaks[name] = max(peaks.get(name, 0.0), value)
        return peaks

    @staticmethod
    def _derive_tier(percent: Optional[float], service_state: str) -> str:
        if service_state not in ("ENABLED",):
            return "UNAVAILABLE"
        if percent is None:
            return "Unknown"
        if percent >= 90:
            return "Exhausted"
        if percent >= 75:
            return "Reduced"
        return "Normal"


# --------------------------------------------------------------------------------------
# Formatter
# --------------------------------------------------------------------------------------


class SummaryFormatter:
    """Render reports as the compact monospace block used everywhere."""

    def __init__(self, width: int = SUMMARY_WIDTH) -> None:
        self.width = max(48, int(width))

    def render(self, reports: Sequence[ProviderReport], *, now: Optional[datetime] = None) -> str:
        stamp = (now or utcnow()).strftime("%Y-%m-%d %H:%M UTC")
        rule = "=" * self.width
        title = f"{SUMMARY_TITLE} ({stamp})"
        lines = [rule, title.center(self.width).rstrip(), rule]
        label_width = max((len(r.label) for r in reports), default=0) + 2
        for report in reports:
            lines.extend(self._provider_lines(report, label_width))
        lines.append(rule)
        return "\n".join(lines)

    def _provider_lines(self, report: ProviderReport, label_width: int) -> List[str]:
        prefix = f"[{report.label}]".ljust(label_width + 1)
        body = " | ".join(report.summary_fields())
        indent = " " * len(prefix)
        wrapped = self._wrap(body, self.width - len(prefix))
        return [prefix + wrapped[0]] + [indent + chunk for chunk in wrapped[1:]]

    @staticmethod
    def _wrap(body: str, width: int) -> List[str]:
        """Wrap on ``|`` separators so a field is never split mid-value."""
        if len(body) <= width or width <= 0:
            return [body]
        lines: List[str] = []
        current = ""
        for index, field_text in enumerate(body.split(" | ")):
            candidate = field_text if not current else f"{current} | {field_text}"
            if len(candidate) <= width or not current:
                current = candidate
            else:
                lines.append(current)
                current = field_text
            del index
        if current:
            lines.append(current)
        return lines


def build_json_payload(
    reports: Sequence[ProviderReport], *, include_raw: bool = False
) -> Dict[str, Any]:
    return {
        "generated_at": iso(utcnow()),
        "tool": "usage-puller",
        "version": __version__,
        "providers": [r.to_dict(include_raw=include_raw) for r in reports],
        "degraded": [r.provider for r in reports if not r.healthy],
    }


def write_github_summary(text: str, *, path: Optional[str] = None) -> Optional[str]:
    """Append a fenced copy of ``text`` to ``$GITHUB_STEP_SUMMARY``."""
    target = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return None
    block = f"### AI quota snapshot\n\n```text\n{text}\n```\n"
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(block)
    except OSError as exc:
        print(f"warning: could not write step summary ({exc})", file=sys.stderr)
        return None
    return target


# --------------------------------------------------------------------------------------
# Optional schema validation (uses pydantic when installed)
# --------------------------------------------------------------------------------------


def validate_schema(payload: Dict[str, Any]) -> List[str]:
    """Validate the JSON payload shape; returns a list of human-readable problems.

    Uses ``pydantic`` when available for a strict check, and falls back to a light
    structural check so ``--strict-schema`` still means something without the dependency.
    """
    BaseModel = safe_import("pydantic", "BaseModel")
    ValidationError = safe_import("pydantic", "ValidationError")
    if BaseModel is None or ValidationError is None:
        problems: List[str] = []
        for provider in payload.get("providers", []):
            for required in ("provider", "label", "status"):
                if not provider.get(required):
                    problems.append(f"{provider.get('provider', '?')}: missing {required}")
        return problems

    class WindowSchema(BaseModel):
        label: str
        percent: Optional[float] = None
        resets_at: Optional[str] = None
        resets_in_seconds: Optional[int] = None
        used: Optional[float] = None
        limit: Optional[float] = None
        unit: Optional[str] = None

    class ProviderSchema(BaseModel):
        provider: str
        label: str
        status: str
        message: Optional[str] = None
        source: Optional[str] = None
        elapsed_ms: Optional[int] = None
        windows: List[WindowSchema] = []
        facts: Dict[str, str] = {}
        notes: List[str] = []

    class SnapshotSchema(BaseModel):
        generated_at: str
        tool: str
        version: str
        providers: List[ProviderSchema]
        degraded: List[str] = []

    try:
        SnapshotSchema(**{k: v for k, v in payload.items() if k != "raw"})
    except ValidationError as exc:
        return [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()]
    return []


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

RESOLVERS: Dict[str, Callable[[HttpClient, bool], QuotaResolver]] = {
    "claude": ClaudeResolver,
    "codex": CodexResolver,
    "antigravity": AntigravityResolver,
}

PROVIDER_ALIASES = {
    "claude": "claude",
    "claude-code": "claude",
    "anthropic": "claude",
    "codex": "codex",
    "openai": "codex",
    "antigravity": "antigravity",
    "agy": "antigravity",
    "google": "antigravity",
    "gcp": "antigravity",
}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_usage.py",
        description="Aggregate AI assistant quota usage into one copiable snippet.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="comma separated providers to query (claude, codex, antigravity/agy)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(env("USAGE_HTTP_TIMEOUT", default=str(DEFAULT_TIMEOUT)) or DEFAULT_TIMEOUT),
        help=f"per-request timeout in seconds (default {DEFAULT_TIMEOUT:g}, clamped 3-60)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of ASCII")
    parser.add_argument(
        "--json-out",
        default=None,
        metavar="PATH",
        help="also write the JSON payload to PATH (one API round-trip for both outputs)",
    )
    parser.add_argument(
        "--raw", action="store_true", help="include raw provider payloads in JSON output"
    )
    parser.add_argument(
        "--width", type=int, default=SUMMARY_WIDTH, help="ASCII block width (default 65)"
    )
    parser.add_argument(
        "--github-summary",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="append the block to $GITHUB_STEP_SUMMARY (or PATH)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any selected provider is unconfigured or errored",
    )
    parser.add_argument(
        "--strict-schema",
        action="store_true",
        help="validate the output payload shape and report violations on stderr",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="load variables from a .env file before running (needs python-dotenv)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log requests to stderr")
    parser.add_argument("--version", action="version", version=f"usage-puller {__version__}")
    return parser.parse_args(argv)


def selected_providers(spec: Optional[str]) -> List[str]:
    if not spec:
        return list(RESOLVERS)
    chosen: List[str] = []
    unknown: List[str] = []
    for token in spec.replace(";", ",").split(","):
        name = token.strip().lower()
        if not name:
            continue
        resolved = PROVIDER_ALIASES.get(name)
        if resolved is None:
            unknown.append(name)
        elif resolved not in chosen:
            chosen.append(resolved)
    if unknown:
        # Exit code 2 matches argparse's convention for a usage error.
        print(
            f"unknown provider(s): {', '.join(unknown)} "
            f"(valid: {', '.join(sorted(set(PROVIDER_ALIASES)))})",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return chosen or list(RESOLVERS)


def load_env_file(path: Optional[str], verbose: bool = False) -> None:
    """Load a ``.env`` file when python-dotenv is present, else parse it inline."""
    candidates = [path] if path else [".env"]
    for candidate in candidates:
        resolved = expand(candidate)
        if not resolved.is_file():
            if path:
                print(f"warning: env file {resolved} not found", file=sys.stderr)
            continue
        if _load_dotenv is not None:
            _load_dotenv(dotenv_path=str(resolved), override=False)
        else:
            for line in resolved.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        if verbose:
            print(f"[env] loaded {resolved}", file=sys.stderr)


def collect_reports(
    providers: Sequence[str], timeout: float, verbose: bool = False
) -> List[ProviderReport]:
    http = HttpClient(timeout=timeout, verbose=verbose)
    reports: List[ProviderReport] = []
    for name in providers:
        resolver = RESOLVERS[name](http, verbose)
        reports.append(resolver.resolve())
    return reports


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(args.env_file, args.verbose)
    providers = selected_providers(args.only)
    reports = collect_reports(providers, args.timeout, args.verbose)

    text = SummaryFormatter(width=args.width).render(reports)
    payload = build_json_payload(reports, include_raw=args.raw)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=False))
    else:
        print(text)

    if args.json_out:
        try:
            expand(args.json_out).write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(f"warning: could not write {args.json_out} ({exc})", file=sys.stderr)

    if args.strict_schema:
        for problem in validate_schema(payload):
            print(f"schema: {problem}", file=sys.stderr)

    if args.github_summary is not None:
        written = write_github_summary(text, path=args.github_summary or None)
        if written and args.verbose:
            print(f"[summary] appended to {written}", file=sys.stderr)

    if args.strict and any(not report.healthy for report in reports):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
