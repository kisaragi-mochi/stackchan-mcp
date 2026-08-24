#!/usr/bin/env python3
"""Generate a configuration/model/firmware/serial consistency report.

The report intentionally contains hashes and redacted configuration only. It
never emits WebSocket tokens, passwords, or other secret values.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
FIRMWARE_ROOT = SCRIPT_DIR.parent
EVALUATION_ROOT = FIRMWARE_ROOT.parent.parent
DEFAULT_OUTPUT_DIR = EVALUATION_ROOT / "artifacts" / "config-consistency"
DEFAULT_SERIAL_LOG = EVALUATION_ROOT / "artifacts" / "boot_serial.log"
DEFAULT_AGENT_CONFIG_NAME = "xiaozhi-agent.local.json"

ASSIGN_RE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$")
UNSET_RE = re.compile(r"^# (CONFIG_[A-Za-z0-9_]+) is not set$")
DEFINE_RE = re.compile(r"^#define (CONFIG_[A-Za-z0-9_]+)(?:\s+(.*))?$")
SECRET_RE = re.compile(r"TOKEN|SECRET|PASSWORD", re.IGNORECASE)

REPORT_KEYS = [
    "CONFIG_BOARD_TYPE_STACKCHAN",
    "CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL",
    "CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT",
    "CONFIG_STACKCHAN_TOUCH_PTT",
    "CONFIG_USE_AFE_WAKE_WORD",
    "CONFIG_USE_CUSTOM_WAKE_WORD",
    "CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS",
    "CONFIG_SR_MN_CN_NONE",
    "CONFIG_SR_MN_CN_MULTINET6_QUANT",
    "CONFIG_USE_AUDIO_PROCESSOR",
    "CONFIG_USE_DEVICE_AEC",
    "CONFIG_USE_SERVER_AEC",
    "CONFIG_LANGUAGE_ZH_CN",
    "CONFIG_DEFAULT_WEBSOCKET_URL",
    "CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL",
    "CONFIG_DEFAULT_WEBSOCKET_TOKEN",
    "CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_URL",
    "CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_TOKEN",
    "CONFIG_FORCE_DEFAULT_WEBSOCKET_URL",
    "CONFIG_DISABLE_OTA_WEBSOCKET_CONFIG",
]


def iso_time(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


APP_DESC_MAGIC = b"\x32\x54\xcd\xab"
APP_ELF_SHA_OFFSET = 144


def app_elf_sha256(path: Path) -> str | None:
    """Read the device-reportable ELF identity from esp_app_desc_t."""
    if not path.exists():
        return None
    with path.open("rb") as handle:
        head = handle.read(1024)
    start = head.find(APP_DESC_MAGIC)
    digest_start = start + APP_ELF_SHA_OFFSET
    digest_end = digest_start + 32
    if start < 0 or digest_end > len(head):
        return None
    return head[digest_start:digest_end].hex()


def parse_sdkconfig(path: Path) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        match = ASSIGN_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
            continue
        match = UNSET_RE.match(line)
        if match:
            values[match.group(1)] = None
    return values


def parse_sdkconfig_header(path: Path) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = DEFINE_RE.match(raw_line.strip())
        if match:
            values[match.group(1)] = match.group(2) or "1"
    return values


def normalized_enabled(value: str | None) -> bool:
    return value in {"y", "1"}


def redact_config(key: str, value: str | None) -> str | None:
    if value is None:
        return None
    if SECRET_RE.search(key):
        return "<MASKED>"
    return value


def selected_config(values: dict[str, str | None]) -> dict[str, str | None]:
    return {key: redact_config(key, values.get(key)) for key in REPORT_KEYS}


def parse_agent_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"invalid": True}
    return value if isinstance(value, dict) else {"invalid": True}


def agent_binding_errors(
    agent_config: dict[str, Any], audit: dict[str, Any]
) -> list[str]:
    """Validate that a redacted cloud audit proves the configured binding."""
    binding = agent_config.get("verified_binding")
    if not isinstance(binding, dict):
        return ["verified_binding is missing"]
    if not audit or audit.get("invalid"):
        return ["binding audit is missing or invalid"]

    errors: list[str] = []
    agent = audit.get("agent") if isinstance(audit.get("agent"), dict) else {}
    device = audit.get("device") if isinstance(audit.get("device"), dict) else {}
    verification = (
        audit.get("verification")
        if isinstance(audit.get("verification"), dict)
        else {}
    )
    safety = audit.get("safety") if isinstance(audit.get("safety"), dict) else {}

    if agent.get("agent_id") != binding.get("agent_id"):
        errors.append("Agent ID does not match the verified binding")
    expected_mac = str(binding.get("device_mac", "")).lower()
    if not expected_mac or str(device.get("mac_address", "")).lower() != expected_mac:
        errors.append("device MAC does not match the verified binding")
    if agent.get("after_language") != agent_config.get("agent_language"):
        errors.append("audited Agent language does not match the build policy")
    if verification.get("language_verified") is not True:
        errors.append("Agent language was not verified after update")
    if verification.get("protected_fields_verified") is not True:
        errors.append("protected Agent fields were not verified after update")
    before = audit.get("protected_field_sha256_before")
    after = audit.get("protected_field_sha256_after")
    if not isinstance(before, dict) or not before or before != after:
        errors.append("protected Agent field hashes differ or are missing")
    if safety.get("access_token_stored") is not False:
        errors.append("audit does not prove that the access token was omitted")
    if safety.get("developer_secret_stored") is not False:
        errors.append("audit does not prove that the developer secret was omitted")
    if safety.get("flash_invoked") is not False:
        errors.append("Agent audit unexpectedly records a flash operation")
    return errors


def effective_firmware_language(values: dict[str, str | None]) -> str | None:
    profiles = {
        "CONFIG_LANGUAGE_ZH_CN": "zh-cn",
        "CONFIG_LANGUAGE_ZH_TW": "zh-tw",
        "CONFIG_LANGUAGE_EN_US": "en-us",
        "CONFIG_LANGUAGE_JA_JP": "ja-jp",
        "CONFIG_LANGUAGE_KO_KR": "ko-kr",
    }
    selected = [profile for key, profile in profiles.items() if normalized_enabled(values.get(key))]
    return selected[0] if len(selected) == 1 else None


def git_info(repo_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip()

    try:
        status = run("status", "--short")
        return {
            "sha": run("rev-parse", "HEAD"),
            "branch": run("branch", "--show-current"),
            "dirty": bool(status),
            "dirty_paths": status.splitlines(),
        }
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"error": str(exc), "dirty": True, "dirty_paths": []}


def latest_build_log(build_dir: Path) -> Path | None:
    log_dir = build_dir / "log"
    candidates = list(log_dir.glob("idf_py_stdout_output_*")) if log_dir.exists() else []
    if not candidates:
        return None
    model_logs = []
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(
            marker in text
            for marker in (
                "wakenet models:",
                "wakenet_model/",
                "multinet models:",
                "multinet_model/",
            )
        ):
            model_logs.append(path)
    return max(model_logs or candidates, key=lambda path: path.stat().st_mtime)


def config_equivalent(left: str | None, right: str | None) -> bool:
    if normalized_enabled(left) or normalized_enabled(right):
        return normalized_enabled(left) == normalized_enabled(right)
    return left == right


def extract_model_names(text: str) -> dict[str, list[str]]:
    wakenet: set[str] = set()
    multinet: set[str] = set()
    skipped_multinet: set[str] = set()
    for line in text.splitlines():
        lower = line.lower()
        for match in re.findall(r"\bwn[0-9][a-z0-9_]*\b", lower):
            wakenet.add(match)
        for match in re.findall(r"\bmn[0-9][a-z0-9_]*\b", lower):
            if "skipping" in lower or "skip" in lower:
                skipped_multinet.add(match)
            else:
                multinet.add(match)
    return {
        "wakenet": sorted(wakenet),
        "multinet": sorted(multinet),
        "skipped_multinet": sorted(skipped_multinet),
    }


def extract_model_evidence(text: str) -> list[str]:
    """Persist only non-secret SR model evidence after build logs are cleaned."""
    markers = ("wakenet", "multinet", "custom wake word")
    return [
        line.strip()
        for line in text.splitlines()
        if any(marker in line.lower() for marker in markers)
    ]


def extract_serial(text: str) -> dict[str, Any]:
    urls = sorted(set(re.findall(r"wss?://[^\s\"']+", text)))
    session_ids = re.findall(r"Session ID:\s*([^\s]+)", text)
    pipeline_lines = [line.strip() for line in text.splitlines() if "AFE Pipeline:" in line]
    device_elf_hashes = [
        value.lower()
        for value in re.findall(
            r"(?:App identity:.*?ELF SHA256=|ELF file SHA256:\s*)([0-9a-fA-F]{8,64})",
            text,
        )
    ]
    return {
        "urls": urls,
        "session_id_present": bool(session_ids),
        "models": extract_model_names(text),
        "afe_pipeline": pipeline_lines[-1] if pipeline_lines else None,
        "contains_firmware_sha256": bool(
            re.search(r"firmware.*sha-?256|xiaozhi\.bin.*[0-9a-f]{64}", text, re.IGNORECASE)
        ),
        "device_elf_sha256": device_elf_hashes,
    }


def add_finding(
    findings: list[dict[str, str]],
    severity: str,
    code: str,
    message: str,
    scope: str = "preflash",
) -> None:
    findings.append(
        {"severity": severity, "scope": scope, "code": code, "message": message}
    )


def preflash_ready(findings: list[dict[str, str]]) -> bool:
    return not any(
        item["severity"] == "error" and item.get("scope", "preflash") == "preflash"
        for item in findings
    )


def build_report(firmware_root: Path, serial_log: Path | None) -> dict[str, Any]:
    repo_root = firmware_root.parent
    build_dir = firmware_root / "build"
    sdkconfig_path = firmware_root / "sdkconfig"
    build_header_path = build_dir / "config" / "sdkconfig.h"
    local_defaults_path = firmware_root / "sdkconfig.defaults.local"
    agent_config_path = firmware_root / DEFAULT_AGENT_CONFIG_NAME
    binary_path = build_dir / "xiaozhi.bin"
    build_log_path = latest_build_log(build_dir)

    sdkconfig = parse_sdkconfig(sdkconfig_path)
    build_header = parse_sdkconfig_header(build_header_path)
    local_defaults = parse_sdkconfig(local_defaults_path)
    agent_config = parse_agent_config(agent_config_path)
    binding = (
        agent_config.get("verified_binding")
        if isinstance(agent_config.get("verified_binding"), dict)
        else {}
    )
    raw_audit_path = binding.get("audit_path") if binding else None
    agent_audit_path = Path(raw_audit_path) if isinstance(raw_audit_path, str) else None
    if agent_audit_path and not agent_audit_path.is_absolute():
        agent_audit_path = firmware_root / agent_audit_path
    agent_audit = parse_agent_config(agent_audit_path) if agent_audit_path else {}
    effective = build_header or sdkconfig
    build_log_text = (
        build_log_path.read_text(encoding="utf-8", errors="replace")
        if build_log_path
        else ""
    )
    serial_text = (
        serial_log.read_text(encoding="utf-8", errors="replace")
        if serial_log and serial_log.exists()
        else ""
    )
    findings: list[dict[str, str]] = []
    repo = git_info(repo_root)

    if not binary_path.exists():
        add_finding(findings, "error", "firmware_missing", "build/xiaozhi.bin does not exist")
    if repo.get("dirty"):
        add_finding(
            findings,
            "warning",
            "dirty_worktree",
            "The firmware binary cannot represent the current dirty source tree exactly.",
        )
    newest_source_mtime = max(
        (
            path.stat().st_mtime
            for path in (firmware_root / "main").rglob("*")
            if path.is_file()
        ),
        default=0,
    )
    if binary_path.exists() and newest_source_mtime > binary_path.stat().st_mtime:
        add_finding(
            findings,
            "error",
            "binary_stale",
            "Source files are newer than build/xiaozhi.bin; rebuild before any flash decision.",
        )
    if not build_header:
        add_finding(
            findings,
            "error",
            "build_config_missing",
            "build/config/sdkconfig.h is missing, so the binary's effective configuration is unknown.",
        )

    if not agent_config:
        add_finding(
            findings,
            "error",
            "agent_config_missing",
            "The PC build profile has no bound Xiaozhi AI Agent language requirement.",
        )
    elif agent_config.get("invalid"):
        add_finding(
            findings,
            "error",
            "agent_config_invalid",
            "xiaozhi-agent.local.json is not a valid JSON object.",
        )
    else:
        firmware_language = effective_firmware_language(effective)
        if agent_config.get("firmware_language") != firmware_language:
            add_finding(
                findings,
                "error",
                "agent_firmware_language_mismatch",
                "The PC Agent profile is bound to a different firmware language.",
            )
        if not isinstance(agent_config.get("agent_language"), str) or not agent_config.get(
            "agent_language"
        ):
            add_finding(
                findings,
                "error",
                "agent_language_missing",
                "The bound Xiaozhi AI Agent language is not explicit.",
            )
        binding_problems = agent_binding_errors(agent_config, agent_audit)
        expected_audit_hash = binding.get("audit_sha256") if binding else None
        actual_audit_hash = sha256(agent_audit_path) if agent_audit_path else None
        if expected_audit_hash and actual_audit_hash != expected_audit_hash:
            binding_problems.append("binding audit SHA256 does not match the sidecar")
        if binding_problems:
            add_finding(
                findings,
                "error",
                "agent_binding_unverified",
                "The configured Xiaozhi Agent binding is not proven: "
                + "; ".join(binding_problems),
            )

    relevant_local_keys = [key for key in REPORT_KEYS if key in local_defaults and not SECRET_RE.search(key)]
    mismatches = [
        key
        for key in relevant_local_keys
        if not config_equivalent(local_defaults.get(key), effective.get(key))
    ]
    if mismatches:
        add_finding(
            findings,
            "error",
            "local_effective_config_mismatch",
            "Local defaults differ from the built configuration for: " + ", ".join(mismatches),
        )

    voice_mode_enabled = [
        key
        for key in (
            "CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL",
            "CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT",
        )
        if normalized_enabled(effective.get(key))
    ]
    if len(voice_mode_enabled) != 1:
        add_finding(
            findings,
            "error",
            "voice_mode_unbound",
            "The built binary does not identify exactly one StackChan voice mode.",
        )

    xiaozhi_mode = normalized_enabled(
        effective.get("CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL")
    )
    mcp_mode = normalized_enabled(
        effective.get("CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT")
    )
    primary_url = (effective.get("CONFIG_DEFAULT_WEBSOCKET_URL") or '""').strip('"')
    action_url = (
        effective.get("CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_URL") or '""'
    ).strip('"')
    ota_ws_disabled = normalized_enabled(
        effective.get("CONFIG_DISABLE_OTA_WEBSOCKET_CONFIG")
    )
    force_primary = normalized_enabled(
        effective.get("CONFIG_FORCE_DEFAULT_WEBSOCKET_URL")
    )
    if xiaozhi_mode and (
        ota_ws_disabled or force_primary or primary_url or not action_url
    ):
        add_finding(
            findings,
            "error",
            "xiaozhi_transport_ownership_mismatch",
            "Xiaozhi mode must use OTA/NVS for the primary voice transport and a separate non-empty action gateway.",
        )
    if mcp_mode and (
        not ota_ws_disabled or not force_primary or not primary_url or action_url
    ):
        add_finding(
            findings,
            "error",
            "mcp_transport_ownership_mismatch",
            "MCP single-shot mode must force a local primary gateway and disable the second action gateway.",
        )

    configured_mn6 = normalized_enabled(effective.get("CONFIG_SR_MN_CN_MULTINET6_QUANT"))
    custom_wake = normalized_enabled(effective.get("CONFIG_USE_CUSTOM_WAKE_WORD"))
    build_models = extract_model_names(build_log_text)
    if configured_mn6 and not custom_wake and not build_models["multinet"]:
        add_finding(
            findings,
            "observation",
            "mn6_flag_without_packaged_model",
            "MN6 is selected while Custom Wake Word is disabled; no packaged Multinet model was found in the build log. Preserve this as an A/B observation profile.",
        )
    if custom_wake and configured_mn6 and not build_models["multinet"]:
        add_finding(
            findings,
            "error",
            "custom_multinet_model_missing",
            "Custom Wake Word and MN6 are enabled, but the build log does not prove that a MultiNet model was packaged.",
        )

    firmware_elf_sha256 = app_elf_sha256(binary_path)
    serial_data = extract_serial(serial_text) if serial_text else None
    serial_identity_verified = False
    if serial_log and not serial_log.exists():
        add_finding(
            findings,
            "warning",
            "serial_log_missing",
            f"Serial log not found: {serial_log}",
            "device",
        )
    elif serial_data and not serial_data["device_elf_sha256"]:
        add_finding(
            findings,
            "error",
            "serial_not_bound_to_binary",
            "The serial log contains no device-originated App ELF SHA256, so a host-injected file hash cannot prove which binary ran.",
            "device",
        )
    elif serial_data and firmware_elf_sha256 and not any(
        firmware_elf_sha256.startswith(value)
        for value in serial_data["device_elf_sha256"]
    ):
        add_finding(
            findings,
            "error",
            "serial_elf_identity_mismatch",
            "The device-reported App ELF SHA256 does not match the selected firmware image.",
            "device",
        )
    elif serial_data and firmware_elf_sha256:
        serial_identity_verified = any(
            firmware_elf_sha256.startswith(value)
            for value in serial_data["device_elf_sha256"]
        )

    technical_ready = preflash_ready(findings)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "inputs": {
            "firmware_root": str(firmware_root),
            "sdkconfig": {"path": str(sdkconfig_path), "sha256": sha256(sdkconfig_path), "mtime": iso_time(sdkconfig_path)},
            "build_sdkconfig_header": {"path": str(build_header_path), "sha256": sha256(build_header_path), "mtime": iso_time(build_header_path)},
            "local_defaults": {"path": str(local_defaults_path), "sha256": sha256(local_defaults_path), "mtime": iso_time(local_defaults_path)},
            "xiaozhi_agent_config": {"path": str(agent_config_path), "sha256": sha256(agent_config_path), "mtime": iso_time(agent_config_path)},
            "xiaozhi_agent_audit": {"path": str(agent_audit_path) if agent_audit_path else None, "sha256": sha256(agent_audit_path) if agent_audit_path else None, "mtime": iso_time(agent_audit_path) if agent_audit_path else None},
            "build_log": {"path": str(build_log_path) if build_log_path else None, "sha256": sha256(build_log_path) if build_log_path else None, "mtime": iso_time(build_log_path) if build_log_path else None},
            "serial_log": {"path": str(serial_log) if serial_log else None, "sha256": sha256(serial_log) if serial_log else None, "mtime": iso_time(serial_log) if serial_log else None},
        },
        "configuration": {
            "effective_source": str(build_header_path if build_header else sdkconfig_path),
            "effective": selected_config(effective),
            "local_defaults": selected_config(local_defaults),
            "xiaozhi_agent": agent_config,
            "xiaozhi_agent_audit": agent_audit,
        },
        "models": {
            "build_log": build_models,
            "build_log_evidence": extract_model_evidence(build_log_text),
        },
        "firmware": {
            "path": str(binary_path),
            "sha256": sha256(binary_path),
            "size": binary_path.stat().st_size if binary_path.exists() else None,
            "mtime": iso_time(binary_path),
            "app_elf_sha256": firmware_elf_sha256,
        },
        "serial": serial_data,
        "findings": findings,
        "gates": {
            "technical_ready_for_flash_review": technical_ready,
            "device_binary_identity_verified": serial_identity_verified,
            "flash_authorized_by_user": False,
        },
    }


def markdown_report(report: dict[str, Any]) -> str:
    firmware = report["firmware"]
    lines = [
        "# StackChan 构建一致性报告",
        "",
        f"生成时间：`{report['generated_at']}`",
        "",
        "## 结论",
        "",
        f"- 技术上可进入烧录评审：`{report['gates']['technical_ready_for_flash_review']}`",
        "- 用户已授权烧录：`False`",
        f"- Git：`{report['repo'].get('sha', 'unknown')}`，dirty=`{report['repo'].get('dirty', True)}`",
        f"- 固件 SHA256：`{firmware.get('sha256') or 'missing'}`",
        "",
        "## 配置",
        "",
        "| 配置项 | 构建有效值 | 本地默认值 |",
        "|---|---:|---:|",
    ]
    effective = report["configuration"]["effective"]
    local = report["configuration"]["local_defaults"]
    for key in REPORT_KEYS:
        lines.append(f"| `{key}` | `{effective.get(key)}` | `{local.get(key)}` |")
    lines.extend(["", "## 模型", "", "```json", json.dumps(report["models"], ensure_ascii=False, indent=2), "```", "", "## 串口证据", "", "```json", json.dumps(report["serial"], ensure_ascii=False, indent=2), "```", "", "## 发现", ""])
    if report["findings"]:
        for item in report["findings"]:
            lines.append(f"- **{item['severity']} / {item['code']}**：{item['message']}")
    else:
        lines.append("- 无一致性问题。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware-root", type=Path, default=FIRMWARE_ROOT)
    parser.add_argument("--serial-log", type=Path, default=DEFAULT_SERIAL_LOG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(args.firmware_root.resolve(), args.serial_log.resolve())
    if args.stdout:
        print(markdown_report(report))
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "latest.json"
    md_path = args.output_dir / "latest.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
