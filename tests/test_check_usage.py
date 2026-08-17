"""Fixture-driven tests for the resolvers, parsers and formatter.

Runs under pytest (``python -m pytest``) and also standalone (``python tests/test_check_usage.py``)
so it works on a Termux install with no test tooling.  No network access is used: the HTTP
client is replaced by a router that serves recorded payload shapes.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_usage as cu  # noqa: E402

NOW = cu.utcnow()


def _iso_in(**kwargs: Any) -> str:
    stamp = cu.iso(NOW + timedelta(**kwargs))
    assert stamp is not None
    return stamp


# --------------------------------------------------------------------------------------
# Payload fixtures
# --------------------------------------------------------------------------------------

# Shape A: flat window keys with dedicated Fable sub-pool keys.
CLAUDE_FLAT = {
    "five_hour": {"utilization": 24, "resets_at": _iso_in(hours=2, minutes=10)},
    "seven_day": {"utilization": 62, "resets_at": _iso_in(days=3)},
    "five_hour_fable": {"utilization": 12, "resets_at": _iso_in(hours=2)},
    "seven_day_fable": {"utilization": 38, "resets_at": _iso_in(days=3)},
    "extra_usage": {"enabled": True, "used_usd": 3.4, "limit_usd": 50},
}

# Shape B: alternative field names, 0..1 ratios, epoch-ms resets, per-model sub-pool list.
CLAUDE_NESTED = {
    "session": {
        "used_percent": 24,
        "reset_at": int((NOW + timedelta(hours=2, minutes=10)).timestamp() * 1000),
    },
    "weekly": {"usage_ratio": 0.62},
    "model_limits": [
        {
            "model": "claude-fable-5",
            "five_hour": {"utilization": 12},
            "seven_day": {"utilization": 38},
        },
        {
            "model": "claude-opus-5",
            "five_hour": {"utilization": 90},
            "seven_day": {"utilization": 91},
        },
    ],
}

CODEX_SUBSCRIPTION = {"hard_limit_usd": 100.0, "plan": {"title": "Pay-as-you-go"}}
CODEX_USAGE = {"total_usage": 4250.0}  # API reports cents
MODELS_HEADERS = {
    "x-ratelimit-limit-requests": "10000",
    "x-ratelimit-remaining-requests": "9994",
}

GCP_SERVICE_ENABLED = {"state": "ENABLED"}


def _time_series(metric: str, value: str) -> Dict[str, Any]:
    return {
        "timeSeries": [
            {
                "metric": {"labels": {"quota_metric": metric}},
                "points": [{"value": {"int64Value": value}}],
            }
        ]
    }


AIP_METRIC = "aiplatform.googleapis.com/online_prediction_requests"


# --------------------------------------------------------------------------------------
# Fake transport
# --------------------------------------------------------------------------------------


class RoutedHttp(cu.HttpClient):
    """Serve fixtures by URL substring; unrouted URLs raise, like a real 404 would."""

    def __init__(
        self,
        routes: Dict[str, Tuple[int, Any]],
        headers: Optional[Dict[str, str]] = None,
    ):
        super().__init__(timeout=10)
        self.routes = routes
        self.extra_headers = headers or {}
        self.calls: list[str] = []

    def request(self, method: str, url: str, **kwargs: Any) -> cu.HttpResponse:
        params = kwargs.get("params")
        if params:
            query = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        self.calls.append(f"{method} {url}")
        for fragment, (status, payload) in self.routes.items():
            if fragment in url:
                body = payload if isinstance(payload, str) else json.dumps(payload)
                return cu.HttpResponse(
                    status=status,
                    headers=cu.CaseInsensitiveDict(self.extra_headers.items()),
                    text=body,
                    url=url,
                )
        raise cu.HttpError(f"unrouted {url}", status=404)


def _claude_routes(payload: Any) -> Dict[str, Tuple[int, Any]]:
    return {
        "oauth/token": (200, {"access_token": "at-test", "expires_in": 3600}),
        "oauth/usage": (200, payload),
    }


def _resolve_claude(payload: Any, monkey_env: bool = True) -> cu.ProviderReport:
    if monkey_env:
        os.environ["CLAUDE_REFRESH_TOKEN"] = "rt-test"
    return cu.ClaudeResolver(RoutedHttp(_claude_routes(payload))).resolve()


def _windows(report: cu.ProviderReport) -> Dict[str, Optional[float]]:
    return {w.label: w.percent for w in report.windows}


# --------------------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------------------


def test_claude_flat_payload() -> None:
    report = _resolve_claude(CLAUDE_FLAT)
    assert report.status == cu.STATUS_OK, report.message
    assert _windows(report) == {"5h": 24.0, "7d": 62.0, "Fable 5h": 12.0, "Fable 7d": 38.0}
    assert dict(report.facts)["Extra credits"] == "$3.40 / $50.00"
    # Only the 5-hour session window keeps a countdown.
    assert report.windows[0].resets_at is not None
    assert all(w.resets_at is None for w in report.windows[1:])


def test_claude_nested_payload_matches_flat() -> None:
    """Alternate field names, ratios and a per-model list must yield the same numbers."""
    report = _resolve_claude(CLAUDE_NESTED)
    assert report.status == cu.STATUS_OK, report.message
    assert _windows(report) == {"5h": 24.0, "7d": 62.0, "Fable 5h": 12.0, "Fable 7d": 38.0}


def test_claude_top_level_window_not_shadowed_by_sub_pool() -> None:
    """A 7d value inside model_limits must never satisfy the account-wide 7d window."""
    report = _resolve_claude(
        {
            "seven_day": {"utilization": 62},
            "model_limits": [{"model": "claude-fable-5", "seven_day": {"utilization": 38}}],
        }
    )
    assert _windows(report)["7d"] == 62.0
    assert _windows(report)["Fable 7d"] == 38.0


def test_claude_refresh_rejected_is_error_not_crash() -> None:
    http = RoutedHttp(
        {"oauth/token": (400, {"error": "invalid_grant", "error_description": "token expired"})}
    )
    os.environ["CLAUDE_REFRESH_TOKEN"] = "rt-expired"
    report = cu.ClaudeResolver(http).resolve()
    assert report.status == cu.STATUS_ERROR
    assert "token expired" in (report.message or "")


def test_claude_html_body_is_error_not_crash() -> None:
    """A Cloudflare-style HTML body must degrade, not raise a JSON decode error."""
    http = RoutedHttp(
        {
            "oauth/token": (200, {"access_token": "at"}),
            "oauth/usage": (200, "<html>challenge</html>"),
        }
    )
    os.environ["CLAUDE_REFRESH_TOKEN"] = "rt-test"
    report = cu.ClaudeResolver(http).resolve()
    assert report.status == cu.STATUS_ERROR
    assert report.healthy is False


def test_claude_unrecognised_payload_is_partial() -> None:
    report = _resolve_claude({"something_else": {"foo": 1}})
    assert report.status == cu.STATUS_PARTIAL
    assert "no recognisable" in (report.message or "")


def test_claude_valid_access_token_is_never_refreshed() -> None:
    """A usable access token must not be traded for a new one — that rotation is what
    invalidated a real user's Keychain credential and logged their Claude CLI out."""
    os.environ.pop("CLAUDE_REFRESH_TOKEN", None)
    os.environ["CLAUDE_ACCESS_TOKEN"] = "at-still-good"
    http = RoutedHttp(_claude_routes(CLAUDE_FLAT))
    try:
        report = cu.ClaudeResolver(http).resolve()
    finally:
        os.environ.pop("CLAUDE_ACCESS_TOKEN", None)
    assert report.status == cu.STATUS_OK, report.message
    assert not any("oauth/token" in call for call in http.calls), http.calls


def test_claude_keychain_refresh_refused_by_default() -> None:
    """Refreshing a Keychain credential cannot be persisted, so it must be refused."""
    for name in ("CLAUDE_REFRESH_TOKEN", "CLAUDE_ACCESS_TOKEN"):
        os.environ.pop(name, None)
    resolver = cu.ClaudeResolver(RoutedHttp(_claude_routes(CLAUDE_FLAT)))
    resolver.load_credentials = lambda: cu.ClaudeCredentials(  # type: ignore[method-assign]
        access_token="at-expired",
        refresh_token="rt-keychain",
        expires_at=cu.utcnow() - timedelta(hours=1),
        source="macOS keychain",
        source_kind="keychain",
    )
    report = resolver.resolve()
    assert report.status == cu.STATUS_UNCONFIGURED
    assert "Keychain" in (report.message or "")
    assert "claude -p ok" in (report.message or "")


def test_claude_keychain_refresh_allowed_with_override() -> None:
    for name in ("CLAUDE_REFRESH_TOKEN", "CLAUDE_ACCESS_TOKEN"):
        os.environ.pop(name, None)
    resolver = cu.ClaudeResolver(
        RoutedHttp(_claude_routes(CLAUDE_FLAT)),
        options=cu.RunOptions(refresh_mode="always"),
    )
    resolver.load_credentials = lambda: cu.ClaudeCredentials(  # type: ignore[method-assign]
        access_token="at-expired",
        refresh_token="rt-keychain",
        expires_at=cu.utcnow() - timedelta(hours=1),
        source="macOS keychain",
        source_kind="keychain",
    )
    report = resolver.resolve()
    assert report.status == cu.STATUS_OK, report.message


def test_claude_no_refresh_mode_refuses_env_refresh() -> None:
    os.environ["CLAUDE_REFRESH_TOKEN"] = "rt-test"
    os.environ.pop("CLAUDE_ACCESS_TOKEN", None)
    report = cu.ClaudeResolver(
        RoutedHttp(_claude_routes(CLAUDE_FLAT)),
        options=cu.RunOptions(refresh_mode="never"),
    ).resolve()
    assert report.status == cu.STATUS_UNCONFIGURED
    assert "--no-refresh" in (report.message or "")


def test_claude_rotated_token_persisted_to_file(tmp_path: Any) -> None:
    """A rotated refresh token must be written back, preserving unrelated fields."""
    for name in ("CLAUDE_REFRESH_TOKEN", "CLAUDE_ACCESS_TOKEN"):
        os.environ.pop(name, None)
    path = Path(tmp_path) / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "at-old",
                    "refreshToken": "rt-old",
                    "expiresAt": 1000,
                    "subscriptionType": "max",
                    "scopes": ["user:inference"],
                },
                "unrelatedTopLevel": {"keep": True},
            }
        )
    )
    os.environ["CLAUDE_CREDENTIALS_PATH"] = str(path)
    routes = {
        "oauth/token": (
            200,
            {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600},
        ),
        "oauth/usage": (200, CLAUDE_FLAT),
    }
    try:
        report = cu.ClaudeResolver(RoutedHttp(routes)).resolve()
    finally:
        os.environ.pop("CLAUDE_CREDENTIALS_PATH", None)

    assert report.status == cu.STATUS_OK, report.message
    written = json.loads(path.read_text())
    assert written["claudeAiOauth"]["refreshToken"] == "rt-new"
    assert written["claudeAiOauth"]["accessToken"] == "at-new"
    assert written["claudeAiOauth"]["subscriptionType"] == "max"
    assert written["claudeAiOauth"]["scopes"] == ["user:inference"]
    assert written["unrelatedTopLevel"] == {"keep": True}
    assert written["claudeAiOauth"]["expiresAt"] > 1000
    assert any("persisted" in note for note in report.notes), report.notes
    assert not list(Path(tmp_path).glob("*.tmp"))


def test_claude_rotated_env_token_warns_secret_is_stale() -> None:
    os.environ["CLAUDE_REFRESH_TOKEN"] = "rt-old"
    os.environ.pop("CLAUDE_ACCESS_TOKEN", None)
    routes = {
        "oauth/token": (200, {"access_token": "at-new", "refresh_token": "rt-new"}),
        "oauth/usage": (200, CLAUDE_FLAT),
    }
    report = cu.ClaudeResolver(RoutedHttp(routes)).resolve()
    assert report.status == cu.STATUS_OK, report.message
    assert any("stale" in note for note in report.notes), report.notes
    # The warning must be visible in the ASCII block, not just the JSON artifact.
    assert "WARNING" in cu.SummaryFormatter().render([report])


def test_claude_unrotated_refresh_writes_nothing(tmp_path: Any) -> None:
    """If the provider does not rotate, the credential file must be left untouched."""
    for name in ("CLAUDE_REFRESH_TOKEN", "CLAUDE_ACCESS_TOKEN"):
        os.environ.pop(name, None)
    path = Path(tmp_path) / "creds-norotate.json"
    original = json.dumps({"claudeAiOauth": {"refreshToken": "rt-old", "expiresAt": 1000}})
    path.write_text(original)
    os.environ["CLAUDE_CREDENTIALS_PATH"] = str(path)
    routes = {
        "oauth/token": (200, {"access_token": "at-new"}),
        "oauth/usage": (200, CLAUDE_FLAT),
    }
    try:
        report = cu.ClaudeResolver(RoutedHttp(routes)).resolve()
    finally:
        os.environ.pop("CLAUDE_CREDENTIALS_PATH", None)
    assert report.status == cu.STATUS_OK, report.message
    assert path.read_text() == original


def test_claude_unconfigured_when_no_credentials(tmp_path: Any = None) -> None:
    for name in ("CLAUDE_REFRESH_TOKEN", "CLAUDE_ACCESS_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        os.environ.pop(name, None)
    os.environ["CLAUDE_CREDENTIALS_PATH"] = "/nonexistent/path/.credentials.json"
    saved = cu.CLAUDE_CREDENTIAL_PATHS
    cu.CLAUDE_CREDENTIAL_PATHS = ("/nonexistent/a.json",)
    try:
        report = cu.ClaudeResolver(RoutedHttp({})).resolve()
    finally:
        cu.CLAUDE_CREDENTIAL_PATHS = saved
        os.environ.pop("CLAUDE_CREDENTIALS_PATH", None)
    assert report.status == cu.STATUS_UNCONFIGURED
    assert "CLAUDE_REFRESH_TOKEN" in (report.message or "")


# --------------------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------------------


def test_codex_billing_happy_path() -> None:
    os.environ["OPENAI_API_KEY"] = "sk-test"
    http = RoutedHttp(
        {
            "billing/subscription": (200, CODEX_SUBSCRIPTION),
            "billing/usage": (200, CODEX_USAGE),
            "/v1/models": (200, {"data": []}),
        },
        headers=MODELS_HEADERS,
    )
    report = cu.CodexResolver(http).resolve()
    assert report.status == cu.STATUS_OK, report.message
    assert _windows(report)["Limit Used"] == 42.5
    facts = dict(report.facts)
    assert facts["Spent"] == "$42.50 / $100.00"
    assert facts["Plan"] == "Pay-as-you-go"


def test_codex_billing_401_falls_back_to_rate_limit_headers() -> None:
    os.environ["OPENAI_API_KEY"] = "sk-test"
    http = RoutedHttp(
        {
            "billing/subscription": (401, {"error": {"message": "no access"}}),
            "billing/usage": (401, {"error": {"message": "no access"}}),
            "/v1/models": (200, {"data": []}),
        },
        headers={
            "x-ratelimit-limit-requests": "500",
            "x-ratelimit-remaining-requests": "375",
        },
    )
    report = cu.CodexResolver(http).resolve()
    assert report.status == cu.STATUS_PARTIAL
    assert "25% used (375/500 left)" in dict(report.facts)["Requests"]


def test_codex_total_failure_is_error() -> None:
    os.environ["OPENAI_API_KEY"] = "sk-test"
    report = cu.CodexResolver(RoutedHttp({})).resolve()
    assert report.status == cu.STATUS_ERROR


def test_codex_unconfigured_without_key() -> None:
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("CODEX_API_KEY", None)
    saved = cu.CODEX_CREDENTIAL_PATHS
    cu.CODEX_CREDENTIAL_PATHS = ("/nonexistent/codex.json",)
    try:
        report = cu.CodexResolver(RoutedHttp({})).resolve()
    finally:
        cu.CODEX_CREDENTIAL_PATHS = saved
    assert report.status == cu.STATUS_UNCONFIGURED
    assert report.message == "Missing OPENAI_API_KEY"


# --------------------------------------------------------------------------------------
# Antigravity
# --------------------------------------------------------------------------------------


def _agy(routes: Dict[str, Tuple[int, Any]]) -> cu.ProviderReport:
    resolver = cu.AntigravityResolver(RoutedHttp(routes))
    resolver.project_id = "demo-project"
    resolver._load_service_account = lambda: {  # type: ignore[method-assign]
        "type": "service_account",
        "project_id": "demo-project",
    }
    resolver._access_token = lambda payload: ("tok", "test sa")  # type: ignore[method-assign]
    return resolver.resolve()


def test_antigravity_happy_path() -> None:
    report = _agy(
        {
            f"services/{cu.AIPLATFORM_SERVICE}": (200, GCP_SERVICE_ENABLED),
            "net_usage": (200, _time_series(AIP_METRIC, "150")),
            "timeSeries": (200, _time_series(AIP_METRIC, "1000")),
        }
    )
    assert report.status == cu.STATUS_OK, report.message
    facts = dict(report.facts)
    assert facts["Daily Tier"] == "Normal"
    assert facts["Quota"] == "15% used (AI Platform: ENABLED)"


def test_antigravity_tier_escalates_with_consumption() -> None:
    for usage, expected in (("800", "Reduced"), ("950", "Exhausted"), ("100", "Normal")):
        report = _agy(
            {
                f"services/{cu.AIPLATFORM_SERVICE}": (200, GCP_SERVICE_ENABLED),
                "net_usage": (200, _time_series(AIP_METRIC, usage)),
                "timeSeries": (200, _time_series(AIP_METRIC, "1000")),
            }
        )
        assert dict(report.facts)["Daily Tier"] == expected, (usage, report.facts)


def test_antigravity_monitoring_denied_is_partial() -> None:
    report = _agy({f"services/{cu.AIPLATFORM_SERVICE}": (200, GCP_SERVICE_ENABLED)})
    assert report.status == cu.STATUS_PARTIAL
    assert dict(report.facts)["Daily Tier"] == "Unknown"


def test_antigravity_service_disabled() -> None:
    report = _agy(
        {
            f"services/{cu.AIPLATFORM_SERVICE}": (200, {"state": "DISABLED"}),
            "net_usage": (200, _time_series(AIP_METRIC, "0")),
            "timeSeries": (200, _time_series(AIP_METRIC, "1000")),
        }
    )
    assert dict(report.facts)["Daily Tier"] == "UNAVAILABLE"


def test_antigravity_malformed_service_account_json() -> None:
    os.environ["GCP_SERVICE_ACCOUNT_JSON"] = "{not json"
    os.environ["GCP_PROJECT_ID"] = "p"
    try:
        report = cu.AntigravityResolver(RoutedHttp({})).resolve()
    finally:
        os.environ.pop("GCP_SERVICE_ACCOUNT_JSON", None)
        os.environ.pop("GCP_PROJECT_ID", None)
    assert report.status == cu.STATUS_UNCONFIGURED
    assert "not valid JSON" in (report.message or "")


# --------------------------------------------------------------------------------------
# Parsers and helpers
# --------------------------------------------------------------------------------------


def test_format_timedelta() -> None:
    assert cu.format_timedelta(timedelta(hours=2, minutes=10)) == "2h 10m"
    assert cu.format_timedelta(timedelta(hours=3)) == "3h"
    assert cu.format_timedelta(timedelta(minutes=45)) == "45m"
    assert cu.format_timedelta(timedelta(days=3, hours=4)) == "3d 4h"
    assert cu.format_timedelta(timedelta(seconds=20)) == "<1m"
    assert cu.format_timedelta(timedelta(seconds=-30)) == "now"


def test_format_percent() -> None:
    assert cu.format_percent(24.0) == "24%"
    assert cu.format_percent(18.53) == "18.5%"
    assert cu.format_percent(None) == "n/a"
    assert cu.format_percent(150) == "100%"
    assert cu.format_percent(-5) == "0%"


def test_coerce_percent_only_rescales_named_ratios() -> None:
    assert cu.coerce_percent(0.62, "usage_ratio") == 62.0
    # A bare 0.62 under a percent-named field is 0.62%, not 62%.
    assert cu.coerce_percent(0.62, "utilization") == 0.62
    assert cu.coerce_percent("47%") == 47.0
    assert cu.coerce_percent(None) is None
    assert cu.coerce_percent("nonsense") is None


def test_parse_timestamp_variants() -> None:
    assert cu.parse_timestamp("2026-08-17T10:00:00Z") is not None
    assert cu.parse_timestamp("2026-08-17T10:00:00.123456789Z") is not None
    assert cu.parse_timestamp(1755000000) is not None
    assert cu.parse_timestamp(1755000000000) is not None
    assert cu.parse_timestamp("garbage") is None
    assert cu.parse_timestamp(None) is None
    assert cu.parse_timestamp(True) is None


def test_decode_maybe_base64() -> None:
    assert cu.decode_maybe_base64("eyJhIjogMX0=") == '{"a": 1}'
    assert cu.decode_maybe_base64('{"a": 1}') == '{"a": 1}'
    assert cu.decode_maybe_base64("not-base64!!") == "not-base64!!"


def test_redact_never_leaks_full_secret() -> None:
    secret = "sk-ant-ort01-abcdefghijklmnopqrstuvwxyz"
    masked = cu.redact(secret)
    assert secret not in masked
    assert masked.startswith("sk-ant")
    assert cu.redact(None) == "<empty>"
    assert cu.redact("short") == "*****"


def test_provider_alias_resolution() -> None:
    assert cu.selected_providers("agy,openai") == ["antigravity", "codex"]
    assert cu.selected_providers(None) == ["claude", "codex", "antigravity"]
    assert cu.selected_providers("claude,claude-code") == ["claude"]


def test_timeout_is_clamped() -> None:
    assert cu.HttpClient(timeout=0.1).timeout == cu.MIN_TIMEOUT
    assert cu.HttpClient(timeout=999).timeout == cu.MAX_TIMEOUT
    assert cu.HttpClient(timeout=10).timeout == 10.0


def test_safe_import_returns_none_for_missing_module() -> None:
    assert cu.safe_import("definitely_not_a_real_module_xyz") is None
    assert cu.safe_import("json") is not None
    assert cu.safe_import("json", "dumps") is not None


# --------------------------------------------------------------------------------------
# Formatter and JSON payload
# --------------------------------------------------------------------------------------


def test_formatter_shape() -> None:
    report = _resolve_claude(CLAUDE_FLAT)
    text = cu.SummaryFormatter().render([report])
    lines = text.splitlines()
    assert lines[0] == "=" * cu.SUMMARY_WIDTH
    assert cu.SUMMARY_TITLE in lines[1]
    assert lines[2] == "=" * cu.SUMMARY_WIDTH
    assert lines[-1] == "=" * cu.SUMMARY_WIDTH
    assert "[Claude Code]" in text
    assert "5h: 24%" in text and "Fable 7d: 38%" in text


def test_formatter_wraps_on_field_boundaries() -> None:
    """Long lines wrap between fields, never mid-value."""
    report = _resolve_claude(CLAUDE_FLAT)
    for line in cu.SummaryFormatter(width=60).render([report]).splitlines():
        assert not line.rstrip().endswith("|"), line


def test_formatter_handles_all_providers_unconfigured() -> None:
    reports = [
        cu.ProviderReport.unconfigured("claude", "Claude Code", "Missing CLAUDE_REFRESH_TOKEN"),
        cu.ProviderReport.unconfigured("codex", "OpenAI Codex", "Missing OPENAI_API_KEY"),
        cu.ProviderReport.error("antigravity", "Antigravity", "HTTP 403"),
    ]
    text = cu.SummaryFormatter().render(reports)
    assert "Status: UNCONFIGURED (Missing OPENAI_API_KEY)" in text
    assert "Status: ERROR (HTTP 403)" in text


def test_json_payload_validates_against_schema() -> None:
    reports = [_resolve_claude(CLAUDE_FLAT)]
    payload = cu.build_json_payload(reports)
    assert cu.validate_schema(payload) == []
    assert payload["providers"][0]["windows"][0]["resets_in_seconds"] > 0


def test_json_payload_excludes_raw_by_default() -> None:
    payload = cu.build_json_payload([_resolve_claude(CLAUDE_FLAT)])
    assert "raw" not in payload["providers"][0]
    with_raw = cu.build_json_payload([_resolve_claude(CLAUDE_FLAT)], include_raw=True)
    assert "raw" in with_raw["providers"][0]


def test_github_step_summary_written(tmp_path: Any) -> None:
    target = Path(tmp_path) / "summary.md"
    written = cu.write_github_summary("hello", path=str(target))
    assert written == str(target)
    assert "```text" in target.read_text()


# --------------------------------------------------------------------------------------
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------------------


def _main() -> int:
    import tempfile
    import traceback

    tests = [
        (name, func)
        for name, func in sorted(globals().items())
        if name.startswith("test_") and callable(func)
    ]
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, func in tests:
            try:
                if "tmp_path" in func.__code__.co_varnames[: func.__code__.co_argcount]:
                    func(tmp)
                else:
                    func()
            except Exception:  # noqa: BLE001 - report and continue
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
            else:
                print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
