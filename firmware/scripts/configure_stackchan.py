#!/usr/bin/env python3
"""Select reproducible StackChan voice/audio build profiles on the host PC.

The script updates only a marked block in the gitignored
``sdkconfig.defaults.local`` file. Existing local gateway addresses/tokens are
reused as either the primary MCP transport or the action-only transport;
camera settings and other user-owned local settings are preserved.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import json
from pathlib import Path
import re
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
FIRMWARE_ROOT = SCRIPT_DIR.parent
CONFIG_ROOT = FIRMWARE_ROOT / "configs"
DEFAULT_LOCAL_FILE = FIRMWARE_ROOT / "sdkconfig.defaults.local"
DEFAULT_SDKCONFIG_FILE = FIRMWARE_ROOT / "sdkconfig"
DEFAULT_AGENT_FILE = FIRMWARE_ROOT / "xiaozhi-agent.local.json"
BEGIN_MARKER = "# BEGIN STACKCHAN MANAGED VOICE PROFILE"
END_MARKER = "# END STACKCHAN MANAGED VOICE PROFILE"

PRIMARY_URL = "CONFIG_DEFAULT_WEBSOCKET_URL"
PRIMARY_TOKEN = "CONFIG_DEFAULT_WEBSOCKET_TOKEN"
ACTION_URL = "CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_URL"
ACTION_TOKEN = "CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_TOKEN"

ASSIGN_RE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$")
UNSET_RE = re.compile(r"^# (CONFIG_[A-Za-z0-9_]+) is not set$")
SENSITIVE_RE = re.compile(r"TOKEN|SECRET|PASSWORD", re.IGNORECASE)

# Keep the firmware language choice in the same host-side managed block as
# voice/audio/transport. Previously this was only an implicit Kconfig default,
# so a stale sdkconfig could silently retain a different language.
LANGUAGE_CHOICES = OrderedDict(
    [
        ("zh-cn", "CONFIG_LANGUAGE_ZH_CN"),
        ("zh-tw", "CONFIG_LANGUAGE_ZH_TW"),
        ("en-us", "CONFIG_LANGUAGE_EN_US"),
        ("ja-jp", "CONFIG_LANGUAGE_JA_JP"),
        ("ko-kr", "CONFIG_LANGUAGE_KO_KR"),
        ("vi-vn", "CONFIG_LANGUAGE_VI_VN"),
        ("th-th", "CONFIG_LANGUAGE_TH_TH"),
        ("de-de", "CONFIG_LANGUAGE_DE_DE"),
        ("fr-fr", "CONFIG_LANGUAGE_FR_FR"),
        ("es-es", "CONFIG_LANGUAGE_ES_ES"),
        ("it-it", "CONFIG_LANGUAGE_IT_IT"),
        ("ru-ru", "CONFIG_LANGUAGE_RU_RU"),
        ("ar-sa", "CONFIG_LANGUAGE_AR_SA"),
        ("hi-in", "CONFIG_LANGUAGE_HI_IN"),
        ("pt-pt", "CONFIG_LANGUAGE_PT_PT"),
        ("pl-pl", "CONFIG_LANGUAGE_PL_PL"),
        ("cs-cz", "CONFIG_LANGUAGE_CS_CZ"),
        ("fi-fi", "CONFIG_LANGUAGE_FI_FI"),
        ("tr-tr", "CONFIG_LANGUAGE_TR_TR"),
        ("id-id", "CONFIG_LANGUAGE_ID_ID"),
        ("uk-ua", "CONFIG_LANGUAGE_UK_UA"),
        ("ro-ro", "CONFIG_LANGUAGE_RO_RO"),
        ("bg-bg", "CONFIG_LANGUAGE_BG_BG"),
        ("ca-es", "CONFIG_LANGUAGE_CA_ES"),
        ("da-dk", "CONFIG_LANGUAGE_DA_DK"),
        ("el-gr", "CONFIG_LANGUAGE_EL_GR"),
        ("fa-ir", "CONFIG_LANGUAGE_FA_IR"),
        ("fil-ph", "CONFIG_LANGUAGE_FIL_PH"),
        ("he-il", "CONFIG_LANGUAGE_HE_IL"),
        ("hr-hr", "CONFIG_LANGUAGE_HR_HR"),
        ("hu-hu", "CONFIG_LANGUAGE_HU_HU"),
        ("ms-my", "CONFIG_LANGUAGE_MS_MY"),
        ("nb-no", "CONFIG_LANGUAGE_NB_NO"),
        ("nl-nl", "CONFIG_LANGUAGE_NL_NL"),
        ("sk-sk", "CONFIG_LANGUAGE_SK_SK"),
        ("sl-si", "CONFIG_LANGUAGE_SL_SI"),
        ("sv-se", "CONFIG_LANGUAGE_SV_SE"),
        ("sr-rs", "CONFIG_LANGUAGE_SR_RS"),
    ]
)

# This is the service-owned AI Agent language, not the firmware/display
# language above.  The official StackChan PC app sends this value to
# xiaozhi.me/api/agents/{id}/config.  Keep it explicit in every build profile
# so WakeNet/MultiNet/AEC comparisons do not accidentally compare different
# bound-agent language policies.
AGENT_LANGUAGE_CHOICES = (
    "zh",
    "en",
    "yue",
    "ja",
    "ko",
    "ru",
    "es",
    "ar",
    "fr",
    "vi",
    "it",
    "id",
    "hi",
    "fi",
    "th",
    "de",
    "pt",
    "uk",
    "tr",
    "cs",
    "pl",
    "ro",
    "ca",
    "nl",
    "sv",
    "da",
    "no",
    "et",
)


def available_profiles(group: str) -> list[str]:
    return sorted(path.stem for path in (CONFIG_ROOT / group).glob("*.defaults"))


def language_values(profile: str) -> OrderedDict[str, str | None]:
    if profile not in LANGUAGE_CHOICES:
        raise ValueError(f"Unknown language profile: {profile}")
    selected = LANGUAGE_CHOICES[profile]
    return OrderedDict(
        (symbol, "y" if symbol == selected else None)
        for symbol in LANGUAGE_CHOICES.values()
    )


def parse_config_lines(text: str) -> OrderedDict[str, str | None]:
    values: OrderedDict[str, str | None] = OrderedDict()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = ASSIGN_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
            continue
        match = UNSET_RE.match(line)
        if match:
            values[match.group(1)] = None
    return values


def load_profile(group: str, name: str) -> OrderedDict[str, str | None]:
    path = CONFIG_ROOT / group / f"{name}.defaults"
    if not path.exists():
        raise ValueError(f"Unknown {group} profile: {name}")
    values = parse_config_lines(path.read_text(encoding="utf-8"))
    if not values:
        raise ValueError(f"Profile has no CONFIG entries: {path}")
    return values


def merge_profiles(*profiles: OrderedDict[str, str | None]) -> OrderedDict[str, str | None]:
    merged: OrderedDict[str, str | None] = OrderedDict()
    for profile in profiles:
        for key, value in profile.items():
            merged[key] = value
    return merged


def enabled(values: OrderedDict[str, str | None], key: str) -> bool:
    return values.get(key) == "y"


def unquote(value: str | None) -> str:
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def quoted(value: str) -> str:
    if '"' in value or "\n" in value or "\r" in value:
        raise ValueError("Kconfig string values cannot contain quotes or newlines")
    return f'"{value}"'


def discover_local_gateway(
    existing_values: OrderedDict[str, str | None],
    explicit_url: str,
    explicit_token: str | None,
) -> tuple[str, str]:
    url = explicit_url or unquote(existing_values.get(ACTION_URL)) or unquote(
        existing_values.get(PRIMARY_URL)
    )
    if explicit_token is not None:
        token = explicit_token
    else:
        token = unquote(existing_values.get(ACTION_TOKEN)) or unquote(
            existing_values.get(PRIMARY_TOKEN)
        )
    return url, token


def bind_transport_values(
    values: OrderedDict[str, str | None],
    transport_profile: str,
    local_gateway_url: str,
    local_gateway_token: str,
) -> None:
    if not local_gateway_url:
        raise ValueError(
            "a local StackChan gateway URL is required; pass --local-gateway-url "
            "or keep CONFIG_DEFAULT_WEBSOCKET_URL/ACTION_GATEWAY_URL in the local file"
        )
    if transport_profile == "xiaozhi-plus-action":
        values[ACTION_URL] = quoted(local_gateway_url)
        values[ACTION_TOKEN] = quoted(local_gateway_token)
    elif transport_profile == "local-mcp":
        values[PRIMARY_URL] = quoted(local_gateway_url)
        values[PRIMARY_TOKEN] = quoted(local_gateway_token)
    else:
        raise ValueError(f"Unknown transport profile: {transport_profile}")


def validate(values: OrderedDict[str, str | None]) -> list[str]:
    errors: list[str] = []
    voice_modes = [
        "CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL",
        "CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT",
    ]
    wake_modes = ["CONFIG_USE_AFE_WAKE_WORD", "CONFIG_USE_CUSTOM_WAKE_WORD"]
    aec_modes = ["CONFIG_USE_DEVICE_AEC", "CONFIG_USE_SERVER_AEC"]

    if sum(enabled(values, key) for key in LANGUAGE_CHOICES.values()) != 1:
        errors.append("exactly one firmware language must be enabled")

    if sum(enabled(values, key) for key in voice_modes) != 1:
        errors.append("exactly one StackChan voice mode must be enabled")
    if sum(enabled(values, key) for key in wake_modes) != 1:
        errors.append("exactly one wake-word implementation must be enabled")
    if sum(enabled(values, key) for key in aec_modes) > 1:
        errors.append("device AEC and server AEC cannot both be enabled")
    if enabled(values, "CONFIG_USE_CUSTOM_WAKE_WORD") and not enabled(
        values, "CONFIG_SR_MN_CN_MULTINET6_QUANT"
    ):
        errors.append("custom Multinet wake-word mode requires the MN6 model")
    if enabled(values, "CONFIG_USE_AFE_WAKE_WORD") and not enabled(
        values, "CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS"
    ):
        errors.append("AFE WakeNet mode requires the 你好小智 WakeNet model")

    xiaozhi = enabled(values, "CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL")
    mcp = enabled(values, "CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT")
    action_url = unquote(values.get(ACTION_URL))
    primary_url = unquote(values.get(PRIMARY_URL))
    ota_ws_disabled = enabled(values, "CONFIG_DISABLE_OTA_WEBSOCKET_CONFIG")
    force_primary = enabled(values, "CONFIG_FORCE_DEFAULT_WEBSOCKET_URL")
    if xiaozhi:
        if ota_ws_disabled or force_primary or primary_url or not action_url:
            errors.append(
                "Xiaozhi conversational mode requires OTA/NVS primary voice "
                "transport and a non-empty action-only gateway"
            )
    if mcp:
        if not ota_ws_disabled or not force_primary or not primary_url or action_url:
            errors.append(
                "MCP single-shot mode requires a forced local primary gateway "
                "and the action-only gateway disabled"
            )
    return errors


def render_managed_block(
    voice_profile: str,
    audio_profile: str,
    transport_profile: str,
    values: OrderedDict[str, str | None],
    language_profile: str = "zh-cn",
) -> str:
    lines = [
        BEGIN_MARKER,
        f"# voice_profile={voice_profile}",
        f"# audio_profile={audio_profile}",
        f"# transport_profile={transport_profile}",
        f"# language_profile={language_profile}",
    ]
    for key, value in values.items():
        lines.append(f"{key}={value}" if value is not None else f"# {key} is not set")
    lines.append(END_MARKER)
    return "\n".join(lines)


def replace_managed_block(existing: str, block: str) -> str:
    pattern = re.compile(
        rf"(?ms)^{re.escape(BEGIN_MARKER)}$.*?^{re.escape(END_MARKER)}$\n?"
    )
    if pattern.search(existing):
        updated = pattern.sub(block + "\n", existing, count=1)
    else:
        separator = "" if not existing or existing.endswith("\n\n") else "\n"
        updated = existing + separator + block + "\n"
    return updated


def replace_sdkconfig_values(
    existing: str, values: OrderedDict[str, str | None]
) -> str:
    """Synchronize managed values into an existing ESP-IDF sdkconfig.

    Kconfig defaults do not override values already persisted in sdkconfig.
    Updating only the keys owned by this configurator makes a profile switch
    effective on the next reconfigure while preserving every unrelated user
    and board setting.
    """
    managed = set(values)
    emitted: set[str] = set()
    lines: list[str] = []
    for line in existing.splitlines():
        match = ASSIGN_RE.match(line) or UNSET_RE.match(line)
        key = match.group(1) if match else None
        if key not in managed:
            lines.append(line)
            continue
        if key in emitted:
            continue
        value = values[key]
        lines.append(f"{key}={value}" if value is not None else f"# {key} is not set")
        emitted.add(key)
    if emitted != managed:
        if lines and lines[-1]:
            lines.append("")
        lines.append("# StackChan host profile synchronized values")
        for key, value in values.items():
            if key in emitted:
                continue
            lines.append(f"{key}={value}" if value is not None else f"# {key} is not set")
    return "\n".join(lines) + "\n"


def mask_line(line: str) -> str:
    match = ASSIGN_RE.match(line)
    if match and SENSITIVE_RE.search(match.group(1)):
        return f'{match.group(1)}="<MASKED>"'
    return line


def reusable_verified_binding(
    path: Path, firmware_language: str, agent_language: str
) -> dict[str, object] | None:
    """Keep a verified binding only while both language policies are unchanged."""
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("firmware_language") != firmware_language:
        return None
    if value.get("agent_language") != agent_language:
        return None
    binding = value.get("verified_binding")
    return binding if isinstance(binding, dict) else None


def reusable_agent_token_proxy(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    endpoint = value.get("official_token_proxy_endpoint") if isinstance(value, dict) else None
    return endpoint if isinstance(endpoint, str) and endpoint else None


def agent_profile(
    firmware_language: str,
    agent_language: str,
    verified_binding: dict[str, object] | None = None,
    token_proxy_endpoint: str | None = None,
) -> dict[str, object]:
    if firmware_language not in LANGUAGE_CHOICES:
        raise ValueError(f"Unknown firmware language profile: {firmware_language}")
    if agent_language not in AGENT_LANGUAGE_CHOICES:
        raise ValueError(f"Unknown Xiaozhi agent language: {agent_language}")
    profile: dict[str, object] = {
        "schema_version": 1,
        "firmware_language": firmware_language,
        "agent_language": agent_language,
        "owner": "AI.AGENT/Xiaozhi service",
        "official_update_endpoint": "https://xiaozhi.me/api/agents/{agent_id}/config",
        "must_match_bound_agent_before_device_scoring": True,
    }
    if verified_binding:
        profile["verified_binding"] = verified_binding
    if token_proxy_endpoint:
        profile["official_token_proxy_endpoint"] = token_proxy_endpoint
    return profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select StackChan voice and audio A/B build profiles"
    )
    parser.add_argument(
        "--voice-mode",
        choices=available_profiles("voice"),
        default="xiaozhi-conversational",
    )
    parser.add_argument(
        "--transport-profile",
        choices=available_profiles("transport"),
        default="xiaozhi-plus-action",
    )
    parser.add_argument(
        "--language",
        choices=list(LANGUAGE_CHOICES),
        default="zh-cn",
        help=(
            "firmware display/OTA language; cloud ASR and bound-agent "
            "language are configured separately"
        ),
    )
    parser.add_argument(
        "--agent-language",
        choices=AGENT_LANGUAGE_CHOICES,
        default="zh",
        help="bound Xiaozhi AI Agent language; independent of firmware language",
    )
    parser.add_argument(
        "--local-gateway-url",
        default="",
        help="local StackChan gateway WebSocket URL; otherwise reuse local defaults",
    )
    parser.add_argument(
        "--local-gateway-token",
        default=None,
        help="local gateway token; otherwise reuse local defaults",
    )
    parser.add_argument(
        "--audio-profile",
        choices=available_profiles("audio_ab"),
        default="wakenet",
    )
    parser.add_argument("--local-file", type=Path, default=DEFAULT_LOCAL_FILE)
    parser.add_argument("--sdkconfig-file", type=Path, default=DEFAULT_SDKCONFIG_FILE)
    parser.add_argument("--agent-file", type=Path, default=DEFAULT_AGENT_FILE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true", help="list available profiles")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        print("voice modes:", ", ".join(available_profiles("voice")))
        print("audio profiles:", ", ".join(available_profiles("audio_ab")))
        print("transport profiles:", ", ".join(available_profiles("transport")))
        print("languages:", ", ".join(LANGUAGE_CHOICES))
        print("agent languages:", ", ".join(AGENT_LANGUAGE_CHOICES))
        return 0

    existing = args.local_file.read_text(encoding="utf-8") if args.local_file.exists() else ""
    existing_values = parse_config_lines(existing)
    local_url, local_token = discover_local_gateway(
        existing_values, args.local_gateway_url, args.local_gateway_token
    )
    values = merge_profiles(
        language_values(args.language),
        load_profile("voice", args.voice_mode),
        load_profile("audio_ab", args.audio_profile),
        load_profile("transport", args.transport_profile),
    )
    try:
        bind_transport_values(
            values, args.transport_profile, local_url, local_token
        )
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    errors = validate(values)
    if errors:
        for error in errors:
            print(f"[ERROR] {error}", file=sys.stderr)
        return 2

    block = render_managed_block(
        args.voice_mode,
        args.audio_profile,
        args.transport_profile,
        values,
        args.language,
    )
    updated = replace_managed_block(existing, block)
    existing_sdkconfig = (
        args.sdkconfig_file.read_text(encoding="utf-8", errors="replace")
        if args.sdkconfig_file.is_file()
        else ""
    )
    updated_sdkconfig = replace_sdkconfig_values(existing_sdkconfig, values)
    binding = reusable_verified_binding(
        args.agent_file, args.language, args.agent_language
    )
    token_proxy = reusable_agent_token_proxy(args.agent_file)
    agent = agent_profile(
        args.language, args.agent_language, binding, token_proxy
    )
    print("\n".join(mask_line(line) for line in block.splitlines()))
    print(json.dumps(agent, ensure_ascii=False, indent=2))
    if args.dry_run:
        print(
            f"[DRY-RUN] no changes written to {args.local_file}, "
            f"{args.sdkconfig_file}, or {args.agent_file}"
        )
        return 0

    args.local_file.parent.mkdir(parents=True, exist_ok=True)
    args.local_file.write_text(updated, encoding="utf-8", newline="\n")
    args.sdkconfig_file.parent.mkdir(parents=True, exist_ok=True)
    args.sdkconfig_file.write_text(
        updated_sdkconfig, encoding="utf-8", newline="\n"
    )
    args.agent_file.parent.mkdir(parents=True, exist_ok=True)
    args.agent_file.write_text(
        json.dumps(agent, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Updated {args.local_file}")
    print(f"Updated {args.sdkconfig_file}")
    print(f"Updated {args.agent_file}")
    if binding:
        print("Cloud binding: preserved verified Agent audit evidence.")
    else:
        print(
            "Cloud reminder: this records the required Xiaozhi agent/ASR policy; "
            "apply it to the bound agent with the official PC app before scoring."
        )
    print("Next build: ESP-IDF 5.5 PowerShell -> python ./scripts/release.py stackchan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
