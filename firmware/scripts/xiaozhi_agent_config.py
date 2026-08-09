#!/usr/bin/env python3
"""Apply the PC-side Xiaozhi AI Agent language profile without touching firmware.

The official StackChan PC application updates an Agent with
``POST /api/agents/{id}/config``.  This tool keeps that backend function
available in the reproducible build workflow.  It is plan-only unless
``--apply`` is passed, and it never stores or prints the access token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable
from urllib.parse import urljoin
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
FIRMWARE_ROOT = SCRIPT_DIR.parent
DEFAULT_PROFILE = FIRMWARE_ROOT / "xiaozhi-agent.local.json"
DEFAULT_BASE_URL = "https://xiaozhi.me/"
DEFAULT_TOKEN_ENV = "XIAOZHI_ACCESS_TOKEN"

# Fields sent by the official StackChan AgentCreate model.  Read the current
# Agent first and preserve these values so changing language cannot silently
# reset character, memory, MCP endpoints, or voice settings.
AGENT_CONFIG_FIELDS = (
    "agent_name",
    "assistant_name",
    "llm_model",
    "tts_voice",
    "tts_speech_speed",
    "tts_pitch",
    "asr_speed",
    "language",
    "character",
    "memory",
    "memory_type",
    "knowledge_base_ids",
    "mcp_endpoints",
    "product_mcp_endpoints",
)
EXTENDED_PROTECTED_FIELDS = ("memory_by_speaker", "teen_mode")


def load_profile(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Xiaozhi Agent profile must be a JSON object")
    language = value.get("agent_language")
    if not isinstance(language, str) or not language.strip():
        raise ValueError("Xiaozhi Agent profile has no agent_language")
    return value


def merge_agent_config(agent: dict[str, Any], language: str) -> dict[str, Any]:
    payload = {key: agent[key] for key in AGENT_CONFIG_FIELDS if key in agent}
    payload["language"] = language
    required = ("agent_name", "assistant_name", "llm_model")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise ValueError("Agent details omit required fields: " + ", ".join(missing))
    return payload


def request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    *,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with opener(request, timeout=15) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Xiaozhi API response is not a JSON object")
    return value


def request_public_json(
    url: str, *, opener: Callable[..., Any] = urlopen
) -> dict[str, Any]:
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    with opener(request, timeout=15) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("StackChan token proxy response is not a JSON object")
    return value


def token_from_proxy(
    url: str, *, opener: Callable[..., Any] = urlopen
) -> str:
    value = request_public_json(url, opener=opener)
    token = value.get("data")
    if isinstance(token, dict):
        token = token.get("token")
    if not isinstance(token, str) or len(token) < 20:
        raise ValueError("StackChan token proxy did not return an access token")
    return token


def protected_hashes(agent: dict[str, Any]) -> dict[str, str]:
    keys = tuple(key for key in AGENT_CONFIG_FIELDS if key != "language")
    result: dict[str, str] = {}
    for key in keys + EXTENDED_PROTECTED_FIELDS:
        if key not in agent:
            continue
        encoded = json.dumps(
            agent[key], sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        result[key] = hashlib.sha256(encoded).hexdigest()
    return result


def extract_agent(details: dict[str, Any]) -> dict[str, Any]:
    agent = details.get("data", {}).get("agent") if details.get("success") else None
    if not isinstance(agent, dict):
        raise ValueError("Xiaozhi API did not return the selected Agent")
    return agent


def apply_agent_language(
    base_url: str,
    agent_id: int,
    token: str,
    language: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    endpoint = urljoin(base_url.rstrip("/") + "/", f"api/agents/{agent_id}")
    details = request_json("GET", endpoint, token, opener=opener)
    agent = extract_agent(details)
    before_language = agent.get("language")
    before_hashes = protected_hashes(agent)
    payload = merge_agent_config(agent, language)
    result = request_json(
        "POST", endpoint + "/config", token, payload, opener=opener
    )
    if result.get("success") is not True:
        raise ValueError(
            "Xiaozhi Agent update failed: " + str(result.get("message", "unknown error"))
        )
    after = extract_agent(request_json("GET", endpoint, token, opener=opener))
    after_hashes = protected_hashes(after)
    changed = sorted(
        key for key, value in before_hashes.items() if after_hashes.get(key) != value
    )
    missing = sorted(key for key in before_hashes if key not in after_hashes)
    verified = after.get("language") == language and not changed and not missing
    rollback_attempted = False
    rollback_verified: bool | None = None
    if not verified:
        rollback_attempted = True
        rollback_payload = merge_agent_config(agent, str(before_language or "en"))
        rollback = request_json(
            "POST", endpoint + "/config", token, rollback_payload, opener=opener
        )
        restored = extract_agent(
            request_json("GET", endpoint, token, opener=opener)
        )
        rollback_verified = (
            rollback.get("success") is True
            and restored.get("language") == before_language
            and protected_hashes(restored) == before_hashes
        )
        raise ValueError(
            "Xiaozhi Agent verification failed; "
            f"changed protected fields={changed}, missing={missing}, "
            f"rollback_verified={rollback_verified}"
        )
    return {
        "agent_id": agent_id,
        "before_language": before_language,
        "after_language": after.get("language"),
        "endpoint": endpoint + "/config",
        "updated": True,
        "verified": verified,
        "protected_field_sha256_before": before_hashes,
        "protected_field_sha256_after": after_hashes,
        "changed_protected_fields": changed,
        "missing_protected_fields": missing,
        "rollback_attempted": rollback_attempted,
        "rollback_verified": rollback_verified,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-file", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--agent-id", type=int)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument(
        "--token-proxy-url",
        default="",
        help="official StackChan token proxy; used only when the token env is empty",
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        profile = load_profile(args.profile_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    language = str(profile["agent_language"])
    binding = profile.get("verified_binding")
    profile_agent_id = binding.get("agent_id") if isinstance(binding, dict) else None
    agent_id = args.agent_id or profile_agent_id
    if not isinstance(agent_id, int) or agent_id <= 0:
        print("[ERROR] Agent ID is missing from CLI and verified_binding", file=sys.stderr)
        return 2
    endpoint = urljoin(
        args.base_url.rstrip("/") + "/", f"api/agents/{agent_id}/config"
    )
    print(f"Agent ID: {agent_id}")
    print(f"Required language: {language}")
    print(f"Update endpoint: {endpoint}")
    if not args.apply:
        print("Plan only. No network request was sent; pass --apply after approval.")
        return 0
    token = os.environ.get(args.token_env, "")
    proxy_url = args.token_proxy_url or str(
        profile.get("official_token_proxy_endpoint", "")
    )
    if not token and proxy_url:
        try:
            token = token_from_proxy(proxy_url)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2
    if not token:
        print(
            f"[ERROR] access token is missing from {args.token_env} and no token proxy is configured",
            file=sys.stderr,
        )
        return 2
    try:
        result = apply_agent_language(
            args.base_url, agent_id, token, language
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
