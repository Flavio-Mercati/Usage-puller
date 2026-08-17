#!/usr/bin/env python3
"""Discover local AI CLI credentials and print the exact GitHub Secrets to create.

Run this on the machine where you already use ``claude``, ``codex`` and ``agy``::

    python extract_tokens.py              # masked report, safe to screenshot
    python extract_tokens.py --reveal     # full secret values, ready to paste
    python extract_tokens.py --reveal --format env > .env

Nothing is uploaded and nothing is written unless you redirect the output yourself.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from check_usage import (
    ANTIGRAVITY_CONFIG_PATHS,
    CLAUDE_CREDENTIAL_PATHS,
    CODEX_CREDENTIAL_PATHS,
    GCP_ADC_PATHS,
    dig,
    existing_paths,
    expand,
    iso,
    parse_timestamp,
    read_json_file,
    redact,
    utcnow,
)

__version__ = "1.0.0"

# Secrets that check_usage.py reads, grouped by provider, with whether they are required.
SECRET_CATALOGUE: Dict[str, List[Tuple[str, bool, str]]] = {
    "Claude Code": [
        ("CLAUDE_REFRESH_TOKEN", True, "long-lived OAuth refresh token used by the CLI"),
        ("CLAUDE_ORG_ID", False, "enables the /v1/organizations/{id}/usage fallback"),
        ("CLAUDE_ACCESS_TOKEN", False, "short-lived; only useful for a one-off local run"),
    ],
    "OpenAI Codex": [
        ("OPENAI_API_KEY", True, "API key with billing read access"),
        ("OPENAI_ORG_ID", False, "required when the key spans several organisations"),
        ("OPENAI_PROJECT_ID", False, "scopes rate-limit reads to one project"),
    ],
    "Google Antigravity": [
        ("GCP_SERVICE_ACCOUNT_JSON", True, "full service account JSON (or base64 of it)"),
        ("GCP_PROJECT_ID", True, "project whose aiplatform quota is reported"),
        ("AGY_TIER_OVERRIDE", False, "pin the reported daily tier instead of deriving it"),
    ],
}


@dataclass
class Finding:
    """One discovered credential (or a clearly explained miss)."""

    provider: str
    secret: str
    value: Optional[str]
    origin: str
    required: bool = True
    note: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.value)

    def rendered_value(self, reveal: bool) -> str:
        if not self.value:
            return "<not found>"
        return self.value if reveal else redact(self.value)


# --------------------------------------------------------------------------------------
# Scanners
# --------------------------------------------------------------------------------------


class Scanner:
    """Base scanner: each subclass reports findings for one provider."""

    provider = "provider"

    def scan(self) -> List[Finding]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _env_finding(
        self, secret: str, names: Sequence[str], required: bool, note: str
    ) -> Optional[Finding]:
        for name in names:
            raw = os.environ.get(name)
            if raw and raw.strip():
                return Finding(
                    provider=self.provider,
                    secret=secret,
                    value=raw.strip(),
                    origin=f"environment variable {name}",
                    required=required,
                    note=note,
                )
        return None


class ClaudeScanner(Scanner):
    provider = "Claude Code"

    def scan(self) -> List[Finding]:
        findings: List[Finding] = []
        blob, origin, warnings = self._locate_credentials()

        refresh = None
        access = None
        expires = None
        subscription = None
        if isinstance(blob, dict):
            refresh = blob.get("refreshToken") or blob.get("refresh_token")
            access = blob.get("accessToken") or blob.get("access_token")
            expires = parse_timestamp(blob.get("expiresAt") or blob.get("expires_at"))
            subscription = blob.get("subscriptionType") or blob.get("subscription_type")

        env_refresh = os.environ.get("CLAUDE_REFRESH_TOKEN", "").strip()
        if env_refresh and not refresh:
            refresh, origin = env_refresh, "environment variable CLAUDE_REFRESH_TOKEN"

        note = "the only Claude secret you actually need in GitHub"
        if subscription:
            note += f"; plan reported as {subscription}"
        findings.append(
            Finding(
                provider=self.provider,
                secret="CLAUDE_REFRESH_TOKEN",
                value=refresh if isinstance(refresh, str) else None,
                origin=origin,
                required=True,
                note=note,
                warnings=list(warnings),
            )
        )

        if isinstance(access, str) and access:
            state = "unknown expiry"
            if expires is not None:
                state = (
                    f"expires {iso(expires)}"
                    if expires > utcnow()
                    else f"EXPIRED at {iso(expires)}"
                )
            findings.append(
                Finding(
                    provider=self.provider,
                    secret="CLAUDE_ACCESS_TOKEN",
                    value=access,
                    origin=origin,
                    required=False,
                    note=f"short-lived ({state}) - do not store in GitHub Secrets",
                )
            )

        findings.append(self._org_id_finding())
        return findings

    def _locate_credentials(self) -> Tuple[Optional[Dict[str, Any]], str, List[str]]:
        warnings: List[str] = []
        explicit = os.environ.get("CLAUDE_CREDENTIALS_PATH")
        candidates = ([explicit] if explicit else []) + list(CLAUDE_CREDENTIAL_PATHS)
        for path in existing_paths(candidates):
            payload = read_json_file(path)
            if not isinstance(payload, dict):
                warnings.append(f"{path} exists but is not readable JSON")
                continue
            blob = payload.get("claudeAiOauth") or payload.get("claude_ai_oauth") or payload
            if isinstance(blob, dict) and (
                blob.get("refreshToken")
                or blob.get("refresh_token")
                or blob.get("accessToken")
                or blob.get("access_token")
            ):
                warnings.extend(self._permission_warnings(path))
                return blob, str(path), warnings
            warnings.append(f"{path} held no OAuth tokens")

        keychain, keychain_warning = self._read_macos_keychain()
        if keychain is not None:
            return keychain, "macOS keychain (Claude Code-credentials)", warnings
        if keychain_warning:
            warnings.append(keychain_warning)

        warnings.append(
            "no credential store found - run `claude` once and log in, then re-run this script"
        )
        return None, "not found", warnings

    @staticmethod
    def _permission_warnings(path: Path) -> List[str]:
        if os.name == "nt":
            return []
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            return []
        if mode & 0o077:
            return [f"{path} is world/group readable (mode {mode:o}); consider chmod 600"]
        return []

    @staticmethod
    def _read_macos_keychain() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if sys.platform != "darwin":
            return None, None
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"keychain lookup failed ({exc})"
        if result.returncode != 0:
            return None, "keychain has no `Claude Code-credentials` entry"
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return None, "keychain entry was not JSON"
        blob = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
        return (blob if isinstance(blob, dict) else payload), None

    def _org_id_finding(self) -> Finding:
        from_env = os.environ.get("CLAUDE_ORG_ID") or os.environ.get("ANTHROPIC_ORG_ID")
        if from_env and from_env.strip():
            return Finding(
                provider=self.provider,
                secret="CLAUDE_ORG_ID",
                value=from_env.strip(),
                origin="environment",
                required=False,
                note="optional usage-endpoint fallback",
            )
        # Claude Code caches the organisation UUID in its settings files.
        for candidate in ("~/.claude.json", "~/.claude/settings.json", "~/.claude/config.json"):
            payload = read_json_file(expand(candidate))
            if not isinstance(payload, dict):
                continue
            org = (
                payload.get("organizationUuid")
                or dig(payload, "oauthAccount", "organizationUuid")
                or dig(payload, "account", "organization_uuid")
            )
            if isinstance(org, str) and org:
                return Finding(
                    provider=self.provider,
                    secret="CLAUDE_ORG_ID",
                    value=org,
                    origin=str(expand(candidate)),
                    required=False,
                    note="optional usage-endpoint fallback",
                )
        return Finding(
            provider=self.provider,
            secret="CLAUDE_ORG_ID",
            value=None,
            origin="not found",
            required=False,
            note="optional - /api/oauth/usage works without it",
        )


class CodexScanner(Scanner):
    provider = "OpenAI Codex"

    def scan(self) -> List[Finding]:
        findings: List[Finding] = []
        found = self._env_finding(
            "OPENAI_API_KEY",
            ("OPENAI_API_KEY", "CODEX_API_KEY"),
            True,
            "billing endpoints need a key with read access",
        )
        warnings: List[str] = []
        if found is None:
            value, origin, warnings = self._scan_files()
            found = Finding(
                provider=self.provider,
                secret="OPENAI_API_KEY",
                value=value,
                origin=origin,
                required=True,
                note="billing endpoints need a key with read access",
                warnings=warnings,
            )
        else:
            found.warnings = warnings
        if found.value and found.value.startswith("sk-proj-") and not os.environ.get(
            "OPENAI_PROJECT_ID"
        ):
            found.warnings.append(
                "project-scoped key detected: set OPENAI_PROJECT_ID for accurate rate limits"
            )
        findings.append(found)

        for secret, names, note in (
            (
                "OPENAI_ORG_ID",
                ("OPENAI_ORG_ID", "OPENAI_ORGANIZATION"),
                "needed for multi-org keys",
            ),
            ("OPENAI_PROJECT_ID", ("OPENAI_PROJECT_ID",), "scopes rate-limit reads"),
        ):
            finding = self._env_finding(secret, names, False, note)
            findings.append(
                finding
                or Finding(
                    provider=self.provider,
                    secret=secret,
                    value=None,
                    origin="not found",
                    required=False,
                    note=f"optional - {note}",
                )
            )
        return findings

    def _scan_files(self) -> Tuple[Optional[str], str, List[str]]:
        warnings: List[str] = []
        explicit = os.environ.get("CODEX_CONFIG_PATH")
        for path in existing_paths(([explicit] if explicit else []) + list(CODEX_CREDENTIAL_PATHS)):
            payload = read_json_file(path)
            if not isinstance(payload, dict):
                warnings.append(f"{path} is not readable JSON (TOML configs are not parsed)")
                continue
            for extractor, field_name in (
                (lambda p: p.get("OPENAI_API_KEY"), "OPENAI_API_KEY"),
                (lambda p: p.get("api_key"), "api_key"),
                (lambda p: p.get("apiKey"), "apiKey"),
                (lambda p: dig(p, "openai", "api_key"), "openai.api_key"),
                (lambda p: dig(p, "tokens", "access_token"), "tokens.access_token"),
            ):
                value = extractor(payload)
                if isinstance(value, str) and value.strip():
                    if field_name == "tokens.access_token":
                        warnings.append(
                            "found a ChatGPT-plan Codex access token, not an API key; "
                            "billing endpoints will likely refuse it - create a key at "
                            "https://platform.openai.com/api-keys"
                        )
                    return value.strip(), f"{path} ({field_name})", warnings
            warnings.append(f"{path} held no API key")
        if not warnings:
            warnings.append("no Codex config found - export OPENAI_API_KEY or run `codex login`")
        return None, "not found", warnings


class AntigravityScanner(Scanner):
    provider = "Google Antigravity"

    def scan(self) -> List[Finding]:
        findings: List[Finding] = []
        sa_value, sa_origin, warnings, project_from_sa = self._scan_service_account()
        findings.append(
            Finding(
                provider=self.provider,
                secret="GCP_SERVICE_ACCOUNT_JSON",
                value=sa_value,
                origin=sa_origin,
                required=True,
                note="paste the whole JSON document as one secret",
                warnings=warnings,
            )
        )

        project = (
            os.environ.get("GCP_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT")
            or project_from_sa
            or self._gcloud_project()
        )
        findings.append(
            Finding(
                provider=self.provider,
                secret="GCP_PROJECT_ID",
                value=project,
                origin="environment / credentials / gcloud config" if project else "not found",
                required=True,
                note="project that hosts aiplatform.googleapis.com",
            )
        )

        for path in existing_paths(ANTIGRAVITY_CONFIG_PATHS):
            payload = read_json_file(path)
            if isinstance(payload, dict):
                hinted = payload.get("project") or payload.get("projectId") or dig(
                    payload, "gcp", "project_id"
                )
                if hinted:
                    findings.append(
                        Finding(
                            provider=self.provider,
                            secret="GCP_PROJECT_ID (agy hint)",
                            value=str(hinted),
                            origin=str(path),
                            required=False,
                            note="project referenced by your local agy config",
                        )
                    )
        return findings

    def _scan_service_account(self) -> Tuple[Optional[str], str, List[str], Optional[str]]:
        warnings: List[str] = []
        raw = os.environ.get("GCP_SERVICE_ACCOUNT_JSON") or os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_JSON"
        )
        if raw and raw.strip():
            return raw.strip(), "environment variable GCP_SERVICE_ACCOUNT_JSON", warnings, None

        explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        candidates = ([explicit] if explicit else []) + list(GCP_ADC_PATHS)
        adc_project: Optional[str] = None
        for path in existing_paths(candidates):
            payload = read_json_file(path)
            if not isinstance(payload, dict):
                warnings.append(f"{path} is not readable JSON")
                continue
            kind = payload.get("type")
            if kind == "service_account":
                return (
                    json.dumps(payload, separators=(",", ":")),
                    str(path),
                    warnings,
                    payload.get("project_id"),
                )
            # User ADC works for local runs but cannot be handed to GitHub Actions.
            quota_project = payload.get("quota_project_id")
            if isinstance(quota_project, str) and quota_project:
                adc_project = adc_project or quota_project
            warnings.append(
                f"{path} is `{kind}` (user ADC), which cannot be copied into GitHub Secrets; "
                "it works for local runs, but create a service account key for CI"
            )
        if not warnings:
            warnings.append(
                "no GCP credentials found - run `gcloud auth application-default login` "
                "for local use, or create a service account key for CI"
            )
        return None, "not found", warnings, adc_project

    @staticmethod
    def _gcloud_project() -> Optional[str]:
        try:
            result = subprocess.run(
                ["gcloud", "config", "get-value", "project"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip()
        if result.returncode != 0 or not value or value in {"(unset)", "unset"}:
            return None
        return value


SCANNERS: Tuple[Scanner, ...] = (ClaudeScanner(), CodexScanner(), AntigravityScanner())


# --------------------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------------------


def render_report(findings: Sequence[Finding], reveal: bool) -> str:
    lines: List[str] = [
        "=" * 72,
        "  usage-puller - local credential discovery".upper(),
        f"  host: {platform.system()} {platform.machine()} | python {platform.python_version()}",
        "=" * 72,
    ]
    if not reveal:
        lines.append("  values are MASKED - re-run with --reveal to print them in full")
        lines.append("-" * 72)

    current_provider = None
    for finding in findings:
        if finding.provider != current_provider:
            current_provider = finding.provider
            lines.append("")
            lines.append(f"## {current_provider}")
        marker = "OK  " if finding.found else ("MISS" if finding.required else "----")
        requirement = "required" if finding.required else "optional"
        lines.append(f"  [{marker}] {finding.secret}  ({requirement})")
        lines.append(f"         value : {finding.rendered_value(reveal)}")
        lines.append(f"         from  : {finding.origin}")
        if finding.note:
            lines.append(f"         note  : {finding.note}")
        for warning in finding.warnings:
            lines.append(f"         warn  : {warning}")

    lines.append("")
    lines.append("=" * 72)
    lines.append("  NEXT STEPS")
    lines.append("=" * 72)
    lines.append("  1. Re-run with --reveal to get the raw values.")
    lines.append("  2. Open your repository -> Settings -> Secrets and variables -> Actions.")
    lines.append("  3. Create one 'New repository secret' per required row above.")
    lines.append("  4. Run the 'AI Usage Snapshot' workflow from the GitHub Mobile app.")
    missing = [f.secret for f in findings if f.required and not f.found]
    if missing:
        lines.append("")
        lines.append(f"  Still missing: {', '.join(sorted(set(missing)))}")
        lines.append("  Providers without secrets simply report UNCONFIGURED; nothing breaks.")
    return "\n".join(lines)


def render_env(findings: Sequence[Finding], reveal: bool) -> str:
    """Emit ``KEY=value`` lines suitable for a ``.env`` file."""
    lines = ["# generated by extract_tokens.py - DO NOT COMMIT", f"# {iso(utcnow())}"]
    for finding in findings:
        if "(" in finding.secret:  # informational rows such as "GCP_PROJECT_ID (agy hint)"
            continue
        value = finding.value if reveal else (redact(finding.value) if finding.value else "")
        if not finding.value:
            lines.append(f"# {finding.secret}=            # {finding.origin}")
            continue
        lines.append(f"{finding.secret}={_quote(value)}")
    return "\n".join(lines) + "\n"


def render_json(findings: Sequence[Finding], reveal: bool) -> str:
    payload = {
        "generated_at": iso(utcnow()),
        "revealed": reveal,
        "findings": [
            {
                "provider": f.provider,
                "secret": f.secret,
                "found": f.found,
                "required": f.required,
                "value": f.value if reveal else (redact(f.value) if f.value else None),
                "origin": f.origin,
                "note": f.note,
                "warnings": f.warnings,
            }
            for f in findings
        ],
    }
    return json.dumps(payload, indent=2)


def render_gh_cli(findings: Sequence[Finding], reveal: bool) -> str:
    """Emit ready-to-run ``gh secret set`` commands."""
    if not reveal:
        return (
            "# --format gh requires --reveal so the commands contain real values.\n"
            "# Re-run: python extract_tokens.py --reveal --format gh\n"
        )
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    for finding in findings:
        if "(" in finding.secret or not finding.value:
            continue
        if finding.secret == "CLAUDE_ACCESS_TOKEN":
            lines.append(f"# skipping {finding.secret}: short-lived, not worth storing")
            continue
        lines.append(f"gh secret set {finding.secret} --body {_quote(finding.value)}")
    return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    """Single-quote a value for shell/.env safety."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


RENDERERS = {
    "report": render_report,
    "env": render_env,
    "json": render_json,
    "gh": render_gh_cli,
}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="extract_tokens.py",
        description="Find local Claude/Codex/GCP credentials and print the GitHub Secrets to set.",
    )
    parser.add_argument(
        "--reveal",
        action="store_true",
        help="print full secret values (default masks them)",
    )
    parser.add_argument(
        "--format",
        choices=sorted(RENDERERS),
        default="report",
        help="output format: report (default), env, json, gh",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="comma separated providers: claude, codex, antigravity",
    )
    parser.add_argument("--version", action="version", version=f"extract_tokens {__version__}")
    return parser.parse_args(argv)


def select_scanners(spec: Optional[str]) -> List[Scanner]:
    if not spec:
        return list(SCANNERS)
    aliases = {
        "claude": ClaudeScanner,
        "anthropic": ClaudeScanner,
        "codex": CodexScanner,
        "openai": CodexScanner,
        "antigravity": AntigravityScanner,
        "agy": AntigravityScanner,
        "gcp": AntigravityScanner,
        "google": AntigravityScanner,
    }
    chosen: List[Scanner] = []
    for token in spec.replace(";", ",").split(","):
        name = token.strip().lower()
        if not name:
            continue
        scanner_cls = aliases.get(name)
        if scanner_cls is None:
            # Exit code 2 matches argparse's convention for a usage error.
            print(
                f"unknown provider: {name} (valid: {', '.join(sorted(aliases))})",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if not any(isinstance(s, scanner_cls) for s in chosen):
            chosen.append(scanner_cls())
    return chosen or list(SCANNERS)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    findings: List[Finding] = []
    for scanner in select_scanners(args.only):
        try:
            findings.extend(scanner.scan())
        except Exception as exc:  # noqa: BLE001 - a broken scanner must not hide the others
            findings.append(
                Finding(
                    provider=scanner.provider,
                    secret="<scan failed>",
                    value=None,
                    origin=f"{type(exc).__name__}: {exc}",
                    required=False,
                )
            )

    print(RENDERERS[args.format](findings, args.reveal))
    if args.reveal and args.format != "json" and sys.stdout.isatty():
        print(
            "\n!! Secrets were printed to your terminal. Clear the scrollback when done.",
            file=sys.stderr,
        )
    return 0 if any(f.found for f in findings) else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
