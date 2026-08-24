#!/usr/bin/env python3
"""Plan or execute a guarded StackChan real-device A/B session.

The default is plan-only and never opens a serial port.  The device workflow is
deliberately split into two user-visible phases:

1. ``--backup`` records security state and reads the complete 16 MiB flash.
2. ``--flash`` requires both the approved candidate App SHA256 and the exact
   approved backup SHA256 from phase 1.

The current OTA application slot is never overwritten.  The candidate is
written to the other OTA slot, the shared assets partition is updated, and a
new OTA selection record switches boot only after those writes succeed.  The
original OTA slot, original assets, and original OTA metadata remain
recoverable from the verified full backup.  ``merged-binary.bin`` is never
used by this script.
"""

from __future__ import annotations

import argparse
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
FIRMWARE_ROOT = SCRIPT_DIR.parent
EVALUATION_ROOT = FIRMWARE_ROOT.parent.parent
ARTIFACT_ROOT = EVALUATION_ROOT / "artifacts" / "firmware-ab"
DEVICE_ROOT = EVALUATION_ROOT / "artifacts" / "device-ab"
FLASH_SIZE = 0x1000000
PARTITION_ENTRY_SIZE = 32
PARTITION_MAGIC = 0x50AA
PARTITION_MD5_MAGIC = 0xEBEB
OTA_ENTRY_SIZE = 32
OTA_COPY_STRIDE = 0x1000
OTA_STATE_NEW = 0
OTA_STATE_UNDEFINED = 0xFFFFFFFF
OTA_STATE_INVALID = 3
OTA_STATE_ABORTED = 4
APP_DESC_MAGIC = b"\x32\x54\xcd\xab"
APP_ELF_SHA_OFFSET = 144
APP_IMAGE_MAGIC = 0xE9
APP_IMAGE_HEADER_SIZE = 0x18
APP_SEGMENT_HEADER_SIZE = 8
MMAP_NAME_LENGTH = 32
MMAP_ENTRY_SIZE = MMAP_NAME_LENGTH + 4 + 4 + 2 + 2


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def app_elf_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        head = handle.read(1024)
    start = head.find(APP_DESC_MAGIC)
    digest_start = start + APP_ELF_SHA_OFFSET
    digest_end = digest_start + 32
    if start < 0 or digest_end > len(head):
        raise ValueError(f"ESP app description/ELF SHA256 not found: {path}")
    return head[digest_start:digest_end].hex()


def parse_app_image(data: bytes) -> dict[str, Any]:
    """Validate one ESP application image and return its boot-visible identity."""
    if len(data) < APP_IMAGE_HEADER_SIZE or data[0] != APP_IMAGE_MAGIC:
        raise ValueError("active OTA slot does not start with an ESP application image")

    segment_count = data[1]
    if segment_count == 0 or segment_count > 16:
        raise ValueError(f"invalid ESP application segment count: {segment_count}")

    offset = APP_IMAGE_HEADER_SIZE
    first_segment = b""
    for index in range(segment_count):
        header_end = offset + APP_SEGMENT_HEADER_SIZE
        if header_end > len(data):
            raise ValueError(f"truncated ESP application segment header {index}")
        segment_size = struct.unpack_from("<I", data, offset + 4)[0]
        segment_start = header_end
        segment_end = segment_start + segment_size
        if segment_end > len(data):
            raise ValueError(f"truncated ESP application segment {index}")
        if index == 0:
            first_segment = data[segment_start:segment_end]
        offset = segment_end

    hashed_size = (offset + 1 + 15) & ~15
    hash_appended = data[0x17] == 1
    image_size = hashed_size + (32 if hash_appended else 0)
    if image_size > len(data):
        raise ValueError("truncated ESP application checksum/hash trailer")
    if hash_appended:
        expected = hashlib.sha256(data[:hashed_size]).digest()
        if data[hashed_size:image_size] != expected:
            raise ValueError("ESP application appended SHA256 is invalid")

    if len(first_segment) < APP_ELF_SHA_OFFSET + 32:
        raise ValueError("ESP application description is truncated")
    if first_segment[:4] != APP_DESC_MAGIC:
        raise ValueError("ESP application description magic is invalid")

    def text_field(start: int, size: int) -> str:
        return first_segment[start : start + size].split(b"\0", 1)[0].decode(
            "utf-8", errors="replace"
        )

    trailing = data[image_size:]
    return {
        "project_name": text_field(0x30, 32),
        "version": text_field(0x10, 32),
        "compile_time": text_field(0x60, 16) + "T" + text_field(0x50, 16),
        "idf_version": text_field(0x70, 32),
        "secure_version": struct.unpack_from("<I", first_segment, 4)[0],
        "app_elf_sha256": first_segment[
            APP_ELF_SHA_OFFSET : APP_ELF_SHA_OFFSET + 32
        ].hex(),
        "image_size": image_size,
        "image_sha256": sha256_bytes(data[:image_size]),
        "hash_appended": hash_appended,
        "trailing_non_ff_bytes": sum(value != 0xFF for value in trailing),
    }


def extract_mmap_asset(path: Path, asset_name: str) -> bytes:
    """Extract one file from the esp_mmap_assets image used by this build."""
    data = path.read_bytes()
    if len(data) < 12:
        raise ValueError(f"asset image is too small: {path}")
    file_count, expected_checksum, combined_length = struct.unpack_from("<III", data, 0)
    combined_end = 12 + combined_length
    table_end = 12 + file_count * MMAP_ENTRY_SIZE
    if combined_end > len(data) or table_end > combined_end:
        raise ValueError("invalid mmap asset header lengths")
    if sum(data[12:combined_end]) & 0xFFFF != expected_checksum:
        raise ValueError("mmap asset checksum mismatch")
    merged_start = table_end
    for index in range(file_count):
        start = 12 + index * MMAP_ENTRY_SIZE
        entry = data[start : start + MMAP_ENTRY_SIZE]
        name = entry[:MMAP_NAME_LENGTH].split(b"\0", 1)[0].decode("utf-8")
        file_size, relative_offset = struct.unpack_from("<II", entry, MMAP_NAME_LENGTH)
        if name != asset_name:
            continue
        payload_start = merged_start + relative_offset
        payload_end = payload_start + 2 + file_size
        if payload_end > combined_end or data[payload_start : payload_start + 2] != b"ZZ":
            raise ValueError(f"invalid mmap payload bounds/prefix for {asset_name}")
        return data[payload_start + 2 : payload_end]
    raise ValueError(f"mmap asset not found: {asset_name}")


def validate_speech_asset_route(
    manifest: dict[str, Any], assets: Path
) -> dict[str, Any]:
    try:
        index = json.loads(extract_mmap_asset(assets, "index.json").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"assets index.json is invalid: {exc}") from exc
    if not isinstance(index.get("srmodels"), str):
        raise ValueError("speech profile has no packaged srmodels entry")
    multinet = index.get("multinet_model")
    if manifest.get("audio_profile") == "custom-multinet":
        if not isinstance(multinet, dict):
            raise ValueError("custom-multinet has no runtime multinet_model route")
        commands = multinet.get("commands")
        if not isinstance(commands, list) or len(commands) != 1:
            raise ValueError("custom-multinet must expose exactly one acceptance command")
        command = commands[0]
        expected = {
            "command": "ni hao xiao zhi",
            "text": "你好小智",
            "action": "wake",
        }
        if not isinstance(command, dict) or any(
            command.get(key) != value for key, value in expected.items()
        ):
            raise ValueError("custom-multinet command route differs from the tested phrase")
        if multinet.get("language") != "cn":
            raise ValueError("custom-multinet runtime language is not Chinese")
    elif multinet is not None:
        raise ValueError("non-custom profile unexpectedly routes wake detection to MultiNet")
    return index


def ensure_under(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    return resolved


def parse_partition_table(data: bytes) -> dict[str, dict[str, int | str]]:
    result: dict[str, dict[str, int | str]] = {}
    for start in range(0, len(data), PARTITION_ENTRY_SIZE):
        entry = data[start : start + PARTITION_ENTRY_SIZE]
        if len(entry) != PARTITION_ENTRY_SIZE:
            break
        magic = struct.unpack_from("<H", entry)[0]
        if magic in (0xFFFF, PARTITION_MD5_MAGIC):
            break
        if magic != PARTITION_MAGIC:
            raise ValueError(f"invalid partition entry magic at 0x{start:x}")
        _, part_type, subtype, offset, size, raw_name, flags = struct.unpack(
            "<HBBII16sI", entry
        )
        name = raw_name.split(b"\0", 1)[0].decode("ascii", errors="strict")
        if not name or name in result:
            raise ValueError(f"invalid or duplicate partition label: {name!r}")
        result[name] = {
            "name": name,
            "type": part_type,
            "subtype": subtype,
            "offset": offset,
            "size": size,
            "flags": flags,
        }
    return result


def require_partition(
    partitions: dict[str, dict[str, int | str]], name: str
) -> dict[str, int | str]:
    partition = partitions.get(name)
    if partition is None:
        raise ValueError(f"candidate partition table has no {name!r} partition")
    return partition


def load_candidate(run_dir: Path, variant: str) -> dict[str, Any]:
    run_dir = ensure_under(run_dir, ARTIFACT_ROOT)
    variant_dir = ensure_under(run_dir / variant, run_dir)
    manifest_path = variant_dir / "manifest.json"
    matrix_path = run_dir / "matrix.json"
    consistency_path = variant_dir / "consistency.json"
    if (
        not manifest_path.is_file()
        or not matrix_path.is_file()
        or not consistency_path.is_file()
    ):
        raise ValueError("selected run/variant has no final manifest and matrix")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    consistency = json.loads(consistency_path.read_text(encoding="utf-8"))
    if matrix.get("flash_invoked") is not False:
        raise ValueError("matrix does not prove flash_invoked=false")
    if manifest.get("technical_ready_for_flash_review") is not True:
        raise ValueError("variant is not technically ready for flash review")
    if manifest.get("flash_authorized_by_user") is not False:
        raise ValueError("candidate manifest has unexpected authorization state")
    if consistency.get("gates", {}).get("technical_ready_for_flash_review") is not True:
        raise ValueError("archived configuration/model consistency gate is not ready")

    bundle = variant_dir / manifest.get("flash_bundle", "flash-bundle")
    app = bundle / "xiaozhi.bin"
    assets = bundle / "generated_assets.bin"
    partition_table = bundle / "partition_table" / "partition-table.bin"
    flasher_args = bundle / "flasher_args.json"
    for path in (app, assets, partition_table, flasher_args):
        if not path.is_file():
            raise ValueError(f"candidate bundle is missing {path.name}")
    if sha256(app) != manifest.get("firmware_sha256"):
        raise ValueError("application SHA256 does not match manifest")

    flasher = json.loads(flasher_args.read_text(encoding="utf-8"))
    flash_files = flasher.get("flash_files", {})
    partition_offsets = [
        int(offset, 0)
        for offset, path in flash_files.items()
        if "partition" in str(path).lower()
    ]
    if len(partition_offsets) != 1:
        raise ValueError("flasher_args.json does not identify one partition table")
    partition_table_offset = partition_offsets[0]
    partition_table_bytes = partition_table.read_bytes()
    partitions = parse_partition_table(partition_table_bytes)
    ota_slots = [require_partition(partitions, "ota_0"), require_partition(partitions, "ota_1")]
    ota_slots.sort(key=lambda item: int(item["subtype"]))
    otadata = require_partition(partitions, "otadata")
    assets_partition = require_partition(partitions, "assets")
    if [int(item["subtype"]) for item in ota_slots] != [0x10, 0x11]:
        raise ValueError("only the expected ota_0/ota_1 layout is supported")
    if int(otadata["size"]) != 0x2000:
        raise ValueError("otadata partition must be exactly 8 KiB")
    if app.stat().st_size > min(int(item["size"]) for item in ota_slots):
        raise ValueError("candidate application does not fit both OTA slots")
    if assets.stat().st_size > int(assets_partition["size"]):
        raise ValueError("candidate assets do not fit the assets partition")

    assets_index = validate_speech_asset_route(manifest, assets)

    embedded_elf_sha = app_elf_sha256(app)
    manifest_elf_sha = manifest.get("app_elf_sha256")
    if manifest_elf_sha and manifest_elf_sha != embedded_elf_sha:
        raise ValueError("embedded App ELF SHA256 does not match manifest")
    if consistency.get("firmware", {}).get("sha256") != manifest["firmware_sha256"]:
        raise ValueError("archived consistency report is bound to another App file")
    if consistency.get("firmware", {}).get("app_elf_sha256") != embedded_elf_sha:
        raise ValueError("archived consistency report is bound to another App ELF")
    return {
        "run_dir": run_dir,
        "variant": variant,
        "manifest": manifest,
        "consistency": consistency,
        "bundle": bundle,
        "app": app,
        "assets": assets,
        "assets_sha256": sha256(assets),
        "assets_index": assets_index,
        "app_elf_sha256": embedded_elf_sha,
        "partition_table": partition_table,
        "partition_table_bytes": partition_table_bytes,
        "partition_table_sha256": sha256(partition_table),
        "partition_table_offset": partition_table_offset,
        "partitions": partitions,
        "ota_slots": ota_slots,
        "otadata": otadata,
        "assets_partition": assets_partition,
    }


def esptool_prefix(
    port: str, *, before: str = "default_reset", after: str = "hard_reset"
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        "esp32s3",
        "--port",
        port,
        "-b",
        "460800",
        "--before",
        before,
        "--after",
        after,
    ]


def security_command(port: str) -> list[str]:
    return [*esptool_prefix(port), "get_security_info"]


def backup_command(port: str, output: Path) -> list[str]:
    return [*esptool_prefix(port), "read_flash", "0x0", hex(FLASH_SIZE), str(output)]


def read_region_command(port: str, offset: int, size: int, output: Path) -> list[str]:
    return [
        *esptool_prefix(port),
        "read_flash",
        hex(offset),
        hex(size),
        str(output),
    ]


def write_region_command(
    port: str,
    offset: int,
    image: Path,
    *,
    before: str = "default_reset",
    after: str = "hard_reset",
) -> list[str]:
    return [
        *esptool_prefix(port, before=before, after=after),
        "write_flash",
        hex(offset),
        str(image),
    ]


def rollback_region_commands(
    port: str,
    candidate: dict[str, Any],
    original_assets: Path,
    original_otadata: Path,
) -> list[list[str]]:
    return [
        write_region_command(
            port,
            int(candidate["assets_partition"]["offset"]),
            original_assets,
            after="no_reset",
        ),
        write_region_command(
            port,
            int(candidate["otadata"]["offset"]),
            original_otadata,
        ),
    ]


def execute_rollback(commands: list[list[str]]) -> list[str]:
    """Try every recovery write so OTA selection is attempted even if assets fail."""
    errors: list[str] = []
    for command in commands:
        try:
            subprocess.run(command, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            errors.append(f"{render_command(command)}: {exc}")
    return errors


def validate_backup(path: Path) -> str:
    if not path.is_file() or path.stat().st_size != FLASH_SIZE:
        raise ValueError(f"backup must be exactly {FLASH_SIZE} bytes: {path}")
    return sha256(path)


def ota_crc(sequence: int) -> int:
    return binascii.crc32(struct.pack("<I", sequence), 0xFFFFFFFF) & 0xFFFFFFFF


def parse_ota_entries(otadata: bytes) -> list[dict[str, int | bool]]:
    if len(otadata) != 0x2000:
        raise ValueError("otadata evidence must be exactly 8 KiB")
    result: list[dict[str, int | bool]] = []
    for copy in range(2):
        start = copy * OTA_COPY_STRIDE
        sequence = struct.unpack_from("<I", otadata, start)[0]
        state = struct.unpack_from("<I", otadata, start + 24)[0]
        crc = struct.unpack_from("<I", otadata, start + 28)[0]
        valid = (
            sequence != 0xFFFFFFFF
            and state not in (OTA_STATE_INVALID, OTA_STATE_ABORTED)
            and crc == ota_crc(sequence)
        )
        result.append(
            {"copy": copy, "sequence": sequence, "state": state, "crc": crc, "valid": valid}
        )
    return result


def active_ota_slot(otadata: bytes, ota_slot_count: int = 2) -> dict[str, Any]:
    entries = parse_ota_entries(otadata)
    valid_entries = [entry for entry in entries if entry["valid"]]
    if not valid_entries:
        return {
            "slot": 0,
            "copy": None,
            "sequence": None,
            "reason": "no valid otadata; bootloader fallback is ota_0",
            "entries": entries,
        }
    active = max(valid_entries, key=lambda entry: int(entry["sequence"]))
    return {
        "slot": (int(active["sequence"]) - 1) % ota_slot_count,
        "copy": int(active["copy"]),
        "sequence": int(active["sequence"]),
        "reason": "highest bootloader-valid OTA sequence",
        "entries": entries,
    }


def prepare_next_otadata(
    original: bytes, active: dict[str, Any], target_slot: int, ota_slot_count: int = 2
) -> tuple[bytes, dict[str, int]]:
    if target_slot == active["slot"]:
        raise ValueError("refusing to select the currently active OTA slot")
    base_sequence = int(active["sequence"] or 0)
    next_sequence = target_slot + 1
    while next_sequence <= base_sequence:
        next_sequence += ota_slot_count
    if next_sequence >= 0xFFFFFFFF:
        raise ValueError("OTA sequence is too close to overflow for guarded switching")
    target_copy = 1 - int(active["copy"]) if active["copy"] is not None else 0
    result = bytearray(original)
    start = target_copy * OTA_COPY_STRIDE
    result[start : start + OTA_ENTRY_SIZE] = b"\xff" * OTA_ENTRY_SIZE
    struct.pack_into("<I", result, start, next_sequence)
    # Match esp_ota_set_boot_partition(): NEW becomes PENDING_VERIFY in the
    # bootloader, then the application must explicitly mark the boot VALID.
    # UNDEFINED would boot indefinitely and silently bypass IDF rollback.
    struct.pack_into("<I", result, start + 24, OTA_STATE_NEW)
    struct.pack_into("<I", result, start + 28, ota_crc(next_sequence))
    verified = active_ota_slot(bytes(result), ota_slot_count)
    if verified["slot"] != target_slot:
        raise ValueError("generated OTA metadata does not select the target slot")
    return bytes(result), {"target_copy": target_copy, "target_sequence": next_sequence}


def analyze_backup(path: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    data = path.read_bytes()
    table_offset = int(candidate["partition_table_offset"])
    expected_table = candidate["partition_table_bytes"]
    if data[table_offset : table_offset + len(expected_table)] != expected_table:
        raise ValueError(
            "device partition table differs from the candidate; no OTA offset is safe"
        )
    otadata_partition = candidate["otadata"]
    otadata_offset = int(otadata_partition["offset"])
    otadata_size = int(otadata_partition["size"])
    original_otadata = data[otadata_offset : otadata_offset + otadata_size]
    active = active_ota_slot(original_otadata, len(candidate["ota_slots"]))
    target_slot = 1 - int(active["slot"])
    target_partition = candidate["ota_slots"][target_slot]
    active_partition = candidate["ota_slots"][int(active["slot"])]
    assets_partition = candidate["assets_partition"]
    assets_offset = int(assets_partition["offset"])
    assets_size = int(assets_partition["size"])
    original_assets = data[assets_offset : assets_offset + assets_size]
    current_app = data[
        int(active_partition["offset"]) : int(active_partition["offset"])
        + int(active_partition["size"])
    ]
    current_app_identity = parse_app_image(current_app)
    next_otadata, switch = prepare_next_otadata(
        original_otadata, active, target_slot, len(candidate["ota_slots"])
    )
    return {
        "active": active,
        "active_partition": active_partition,
        "target_slot": target_slot,
        "target_partition": target_partition,
        "original_otadata": original_otadata,
        "next_otadata": next_otadata,
        "original_otadata_sha256": sha256_bytes(original_otadata),
        "next_otadata_sha256": sha256_bytes(next_otadata),
        "original_assets": original_assets,
        "original_assets_sha256": sha256_bytes(original_assets),
        "current_app_partition_sha256": sha256_bytes(current_app),
        "current_app_identity": current_app_identity,
        **switch,
    }


def validate_live_prewrite(
    candidate: dict[str, Any],
    analysis: dict[str, Any],
    partition_table: Path,
    otadata: Path,
    active_app: Path,
    assets: Path,
    require_assets_match: bool = False,
) -> dict[str, Any]:
    """Prove phase-1 critical state is still live immediately before writes."""
    if partition_table.read_bytes() != candidate["partition_table_bytes"]:
        raise ValueError("live partition table changed since the approved backup")
    if otadata.read_bytes() != analysis["original_otadata"]:
        raise ValueError("live OTA selection changed since the approved backup")
    live_app = parse_app_image(active_app.read_bytes())
    if live_app["image_sha256"] != analysis["current_app_identity"]["image_sha256"]:
        raise ValueError("live active App changed since the approved backup")
    live_assets_sha = sha256(assets)
    if require_assets_match:
        if not asset_image_matches_partition(candidate["assets"], assets):
            raise ValueError("live assets do not match the approved candidate image")
        assets_scope = "candidate-image-prefix"
    else:
        if live_assets_sha != analysis["original_assets_sha256"]:
            raise ValueError("live assets changed since the approved backup")
        assets_scope = "full-partition"
    return {
        "partition_table_sha256": sha256(partition_table),
        "otadata_sha256": sha256(otadata),
        "active_app_identity": live_app,
        "assets_sha256": live_assets_sha,
        "assets_scope": assets_scope,
    }


def asset_image_matches_partition(candidate_assets: Path, live_partition: Path) -> bool:
    """Return true when the candidate mmap image is already installed.

    The assets partition is larger than ``generated_assets.bin`` and may retain
    unrelated bytes after the image.  Compare only the exact candidate image
    prefix so an already-installed asset image does not get erased and written
    again during an App-only A/B switch.
    """
    candidate_data = candidate_assets.read_bytes()
    live_data = live_partition.read_bytes()
    return len(candidate_data) <= len(live_data) and live_data[: len(candidate_data)] == candidate_data


def require_idf55() -> str:
    idf_path = os.environ.get("IDF_PATH")
    if not idf_path:
        raise ValueError("IDF_PATH is missing; use the ESP-IDF 5.5 PowerShell")
    idf_script = Path(idf_path) / "tools" / "idf.py"
    if not idf_script.is_file():
        raise ValueError(f"idf.py is missing under IDF_PATH: {idf_path}")
    completed = subprocess.run(
        [sys.executable, str(idf_script), "--version"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    version = completed.stdout.strip() or completed.stderr.strip()
    if "v5.5" not in version:
        raise ValueError(f"device workflow requires ESP-IDF v5.5.x, got {version}")
    return version


def run_security_check(port: str) -> tuple[str, dict[str, bool]]:
    completed = subprocess.run(
        security_command(port),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = completed.stdout + completed.stderr
    state = {
        "secure_boot_enabled": "Secure Boot: Enabled" in output,
        "flash_encryption_enabled": "Flash Encryption: Enabled" in output,
    }
    return output, state


def render_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def run_read_with_retries(command: list[str], attempts: int = 3) -> None:
    """Retry transient USB/JTAG short reads without ever changing to a write."""
    if "read_flash" not in command or "write_flash" in command:
        raise ValueError("read retry helper accepts read_flash commands only")
    output = Path(command[-1])
    for attempt in range(1, attempts + 1):
        output.unlink(missing_ok=True)
        try:
            subprocess.run(command, check=True)
            return
        except subprocess.CalledProcessError:
            if attempt == attempts:
                raise
            print(f"  transient read failure; retrying ({attempt + 1}/{attempts})")
            time.sleep(1)


def reset_for_boot_capture(serial_port: Any) -> None:
    """Reset after opening the serial port so early ESP-IDF identity is captured.

    Keep DTR inactive (GPIO0 high) while pulsing RTS (EN low).  Opening the
    port only after esptool's reset can miss ``ELF file SHA256`` on fast boots,
    which previously caused a valid image to be treated as unproved.
    """
    serial_port.dtr = False
    serial_port.rts = True
    time.sleep(0.1)
    serial_port.rts = False


def capture_serial(port: str, seconds: int, output: Path, header: dict[str, str]) -> None:
    import serial

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for key, value in header.items():
            handle.write(f"# {key}={value}\n")
        handle.flush()

        serial_port = serial.Serial()
        serial_port.port = port
        serial_port.baudrate = 115200
        serial_port.timeout = 0.5
        serial_port.dtr = False
        serial_port.rts = False
        last_error: Exception | None = None
        for _ in range(10):
            try:
                serial_port.open()
                break
            except Exception as exc:
                last_error = exc
                time.sleep(1)
        if not serial_port.is_open:
            raise RuntimeError(f"serial port did not reopen: {last_error}")
        try:
            handle.write("# capture_reset=pulse_rts_after_open\n")
            handle.flush()
            reset_for_boot_capture(serial_port)
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                raw = serial_port.readline()
                if raw:
                    handle.write(raw.decode("utf-8", errors="replace").rstrip() + "\n")
                    handle.flush()
        finally:
            serial_port.close()


def serial_elf_hashes(text: str) -> list[str]:
    return [
        value.lower()
        for value in re.findall(
            r"(?:App identity:.*?ELF SHA256=|ELF file SHA256:\s*)([0-9a-fA-F]{8,64})",
            text,
        )
    ]


def serial_proves_candidate(path: Path, expected_elf_sha256: str) -> bool:
    hashes = serial_elf_hashes(path.read_text(encoding="utf-8", errors="replace"))
    return any(expected_elf_sha256.startswith(value) for value in hashes)


def write_device_consistency_report(
    output_dir: Path,
    candidate: dict[str, Any],
    serial_log: Path,
    backup_sha256: str,
    analysis: dict[str, Any],
) -> tuple[Path, Path, bool]:
    """Join archived build facts to device-originated serial evidence."""
    archived = candidate["consistency"]
    text = serial_log.read_text(encoding="utf-8", errors="replace")
    device_hashes = serial_elf_hashes(text)
    identity_matches = any(
        candidate["app_elf_sha256"].startswith(value) for value in device_hashes
    )
    runtime_evidence = [
        line.strip()
        for line in text.splitlines()
        if any(
            marker in line
            for marker in (
                "App identity:",
                "ELF file SHA256:",
                "Voice interaction mode:",
                "AFE Pipeline:",
                "Session ID:",
            )
        )
    ]
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "variant": candidate["variant"],
        "configuration": archived["configuration"],
        "models": archived["models"],
        "packaged_speech_assets": candidate["assets_index"],
        "firmware": {
            "file": str(candidate["app"]),
            "sha256": candidate["manifest"]["firmware_sha256"],
            "app_elf_sha256": candidate["app_elf_sha256"],
            "assets_sha256": candidate["assets_sha256"],
            "partition_table_sha256": candidate["partition_table_sha256"],
            "candidate_ota_slot": analysis["target_slot"],
            "preserved_original_ota_slot": analysis["active"]["slot"],
        },
        "serial": {
            "path": str(serial_log),
            "sha256": sha256(serial_log),
            "device_reported_elf_sha256": device_hashes,
            "runtime_evidence": runtime_evidence,
        },
        "rollback": {
            "preflash_full_backup_sha256": backup_sha256,
            "original_assets_sha256": analysis["original_assets_sha256"],
            "original_otadata_sha256": analysis["original_otadata_sha256"],
        },
        "gates": {
            "configuration_model_firmware_preflash": True,
            "device_boot_identity_matches_firmware": identity_matches,
            "configuration_model_firmware_serial_consistent": identity_matches,
        },
    }
    json_path = output_dir / "device-consistency.json"
    markdown_path = output_dir / "device-consistency.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archived_models = archived.get("models", {})
    # The modified A/B builds archive parsed build-log evidence under
    # ``models.build_log``.  A preserved upstream StackChan build already has
    # the authoritative packaged-model list, so it stores that list directly
    # under ``models``.  Both shapes prove the same build-time boundary.
    model_summary = json.dumps(
        archived_models.get("build_log", archived_models), ensure_ascii=False
    )
    markdown_path.write_text(
        "\n".join(
            [
                "# StackChan device consistency report",
                "",
                f"- Variant: `{candidate['variant']}`",
                f"- App file SHA256: `{candidate['manifest']['firmware_sha256']}`",
                f"- App ELF SHA256: `{candidate['app_elf_sha256']}`",
                f"- Device-reported ELF values: `{device_hashes}`",
                f"- Build model evidence: `{model_summary}`",
                f"- Preserved original OTA slot: `{analysis['active']['slot']}`",
                f"- Candidate OTA slot: `{analysis['target_slot']}`",
                f"- Full backup SHA256: `{backup_sha256}`",
                f"- Config-model-firmware-serial consistent: `{identity_matches}`",
                "",
                "The configuration and model facts are bound to the archived App ELF at build time; ",
                "the serial gate accepts only the ELF identity reported by the running device.",
                "A host-injected file-hash header is retained for traceability but is not boot proof.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return json_path, markdown_path, identity_matches


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--run-dir", type=Path, required=True)
    result.add_argument("--variant", required=True)
    result.add_argument("--port", default="COM7")
    result.add_argument("--backup", action="store_true", help="security check + read current 16 MiB")
    result.add_argument("--backup-file", type=Path)
    result.add_argument("--flash", action="store_true", help="write inactive OTA slot + assets + OTA selection")
    result.add_argument("--rollback", action="store_true", help="restore original assets + OTA metadata from backup")
    result.add_argument("--authorized-app-sha", default="")
    result.add_argument("--authorized-backup-sha", default="")
    result.add_argument("--monitor-seconds", type=int, default=45)
    result.add_argument(
        "--require-assets-match",
        action="store_true",
        help="require the live candidate-length assets prefix to match and never rewrite assets",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if sum((args.backup, args.flash, args.rollback)) > 1:
        print("[ERROR] --backup, --flash, and --rollback are separate phases", file=sys.stderr)
        return 2
    try:
        candidate = load_candidate(args.run_dir, args.variant)
    except (OSError, ValueError, json.JSONDecodeError, struct.error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    manifest = candidate["manifest"]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_dir = DEVICE_ROOT / run_id
    backup_file = args.backup_file or session_dir / "preflash-full-16mb.bin"
    print(f"Candidate: {args.variant}")
    print(f"App SHA256: {manifest['firmware_sha256']}")
    print(f"Embedded App ELF SHA256: {candidate['app_elf_sha256']}")
    print(f"Assets SHA256: {candidate['assets_sha256']}")
    multinet_route = candidate["assets_index"].get("multinet_model")
    print(
        "Packaged speech route: "
        + ("Chinese MultiNet custom wake command" if multinet_route else "WakeNet; no MultiNet runtime route")
    )
    print(f"Partition table SHA256: {candidate['partition_table_sha256']}")
    print(f"Port: {args.port}")
    print(f"Security command: {render_command(security_command(args.port))}")
    print(f"Backup command: {render_command(backup_command(args.port, backup_file))}")
    print("Flash target: determined from verified backup (inactive OTA slot only)")
    print("Merged image write: NEVER USED")

    if not args.backup and not args.flash and not args.rollback:
        print("Plan only. No serial port was opened.")
        return 0

    try:
        idf_version = require_idf55()
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    session_dir.mkdir(parents=True, exist_ok=True)

    try:
        security_output, security = run_security_check(args.port)
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] security preflight failed: {exc}", file=sys.stderr)
        return 2
    (session_dir / "security-info.txt").write_text(security_output, encoding="utf-8")
    if security["secure_boot_enabled"] or security["flash_encryption_enabled"]:
        print(
            "[ERROR] Secure Boot or Flash Encryption is enabled; this unsigned A/B path is forbidden",
            file=sys.stderr,
        )
        return 2

    if args.backup:
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(backup_command(args.port, backup_file), check=True)
        except subprocess.CalledProcessError as exc:
            print(f"[ERROR] full-flash backup failed: {exc}", file=sys.stderr)
            return 2

    try:
        backup_sha = validate_backup(backup_file)
        analysis = analyze_backup(backup_file, candidate)
    except (OSError, ValueError, struct.error) as exc:
        print(f"[ERROR] Refusing device write: {exc}", file=sys.stderr)
        return 2

    original_assets_file = session_dir / "rollback-assets.bin"
    original_otadata_file = session_dir / "rollback-otadata.bin"
    next_otadata_file = session_dir / f"{args.variant}-next-otadata.bin"
    original_assets_file.write_bytes(analysis["original_assets"])
    original_otadata_file.write_bytes(analysis["original_otadata"])
    next_otadata_file.write_bytes(analysis["next_otadata"])
    backup_manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "idf_version": idf_version,
        "port": args.port,
        "path": str(backup_file),
        "size": backup_file.stat().st_size,
        "sha256": backup_sha,
        "security": security,
        "partition_table_sha256": candidate["partition_table_sha256"],
        "active_ota_slot": analysis["active"]["slot"],
        "active_ota_copy": analysis["active"]["copy"],
        "active_ota_sequence": analysis["active"]["sequence"],
        "inactive_target_slot": analysis["target_slot"],
        "inactive_target_offset": int(analysis["target_partition"]["offset"]),
        "current_app_partition_sha256": analysis["current_app_partition_sha256"],
        "current_app_identity": analysis["current_app_identity"],
        "original_assets_sha256": analysis["original_assets_sha256"],
        "original_otadata_sha256": analysis["original_otadata_sha256"],
        "rollback_assets": str(original_assets_file),
        "rollback_otadata": str(original_otadata_file),
    }
    (session_dir / "backup-manifest.json").write_text(
        json.dumps(backup_manifest, indent=2) + "\n", encoding="utf-8"
    )

    if args.backup:
        print(f"Backup verified: {backup_sha}")
        print(
            f"Current ota_{analysis['active']['slot']} preserved; candidate target is "
            f"ota_{analysis['target_slot']} at 0x{int(analysis['target_partition']['offset']):x}"
        )
        print("No flash requested. Review backup-manifest.json before authorizing phase 2.")
        return 0

    if args.authorized_backup_sha != backup_sha:
        print(
            "[ERROR] --authorized-backup-sha must exactly match the verified full backup",
            file=sys.stderr,
        )
        return 2

    if args.rollback:
        commands = rollback_region_commands(
            args.port, candidate, original_assets_file, original_otadata_file
        )
        errors = execute_rollback(commands)
        if errors:
            print("[ERROR] rollback was incomplete: " + "; ".join(errors), file=sys.stderr)
            return 4
        print(f"Rollback restored original assets and OTA selection: {session_dir}")
        return 0

    if args.authorized_app_sha != manifest["firmware_sha256"]:
        print(
            "[ERROR] --authorized-app-sha must exactly match the candidate app SHA",
            file=sys.stderr,
        )
        return 2

    app_offset = int(analysis["target_partition"]["offset"])
    assets_offset = int(candidate["assets_partition"]["offset"])
    otadata_offset = int(candidate["otadata"]["offset"])
    prewrite_dir = session_dir / "prewrite-live"
    prewrite_dir.mkdir(parents=True, exist_ok=True)
    live_partition_table = prewrite_dir / "partition-table.bin"
    live_otadata = prewrite_dir / "otadata.bin"
    live_active_app = prewrite_dir / "active-app.bin"
    live_assets = prewrite_dir / "assets.bin"
    live_reads = [
        read_region_command(
            args.port,
            int(candidate["partition_table_offset"]),
            len(candidate["partition_table_bytes"]),
            live_partition_table,
        ),
        read_region_command(
            args.port,
            otadata_offset,
            int(candidate["otadata"]["size"]),
            live_otadata,
        ),
        read_region_command(
            args.port,
            int(analysis["active_partition"]["offset"]),
            int(analysis["current_app_identity"]["image_size"]),
            live_active_app,
        ),
        read_region_command(
            args.port,
            assets_offset,
            (
                candidate["assets"].stat().st_size
                if args.require_assets_match
                else int(candidate["assets_partition"]["size"])
            ),
            live_assets,
        ),
    ]
    print("Read-only live-state guard before any write:")
    try:
        for command in live_reads:
            print(f"- {render_command(command)}")
            run_read_with_retries(command)
        prewrite_evidence = validate_live_prewrite(
            candidate,
            analysis,
            live_partition_table,
            live_otadata,
            live_active_app,
            live_assets,
            require_assets_match=args.require_assets_match,
        )
    except (OSError, ValueError, struct.error, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] Refusing device write: live-state guard failed: {exc}", file=sys.stderr)
        return 2

    assets_already_installed = asset_image_matches_partition(
        candidate["assets"], live_assets
    )
    if args.require_assets_match and not assets_already_installed:
        print("[ERROR] Refusing device write: candidate assets mismatch", file=sys.stderr)
        return 2
    commands = [write_region_command(args.port, app_offset, candidate["app"])]
    if assets_already_installed:
        print("Candidate assets already match the live partition; skipping assets write")
    else:
        commands.append(
            write_region_command(
                args.port, assets_offset, candidate["assets"], after="no_reset"
            )
        )
    commands.append(write_region_command(args.port, otadata_offset, next_otadata_file))
    print("Guarded write sequence:")
    for command in commands:
        print(f"- {render_command(command)}")
    failed_write_index: int | None = None
    try:
        for index, command in enumerate(commands):
            failed_write_index = index
            subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        if failed_write_index == 0:
            print(
                f"[ERROR] inactive App write failed before shared state was touched: {exc}",
                file=sys.stderr,
            )
            return 2
        rollback_errors = execute_rollback(
            rollback_region_commands(
                args.port, candidate, original_assets_file, original_otadata_file
            )
        )
        if rollback_errors:
            print(
                "[ERROR] write failed and automatic rollback was incomplete: "
                + "; ".join(rollback_errors),
                file=sys.stderr,
            )
            return 4
        print(
            f"[ERROR] write failed; original assets and OTA selection were restored: {exc}",
            file=sys.stderr,
        )
        return 2

    serial_log = session_dir / f"{args.variant}-serial.log"
    try:
        capture_serial(
            args.port,
            args.monitor_seconds,
            serial_log,
            {
                "variant": args.variant,
                "firmware_file_sha256": manifest["firmware_sha256"],
                "expected_app_elf_sha256": candidate["app_elf_sha256"],
                "assets_sha256": candidate["assets_sha256"],
                "preflash_backup_sha256": backup_sha,
                "preserved_original_ota_slot": str(analysis["active"]["slot"]),
                "candidate_ota_slot": str(analysis["target_slot"]),
            },
        )
    except Exception as exc:
        rollback_errors = execute_rollback(
            rollback_region_commands(
                args.port, candidate, original_assets_file, original_otadata_file
            )
        )
        if rollback_errors:
            print(
                f"[ERROR] serial capture failed ({exc}) and automatic rollback was incomplete: "
                + "; ".join(rollback_errors),
                file=sys.stderr,
            )
            return 4
        print(
            f"[ERROR] serial capture failed; original assets and OTA selection were restored: {exc}",
            file=sys.stderr,
        )
        return 3
    consistency_json, consistency_markdown, boot_identity_verified = (
        write_device_consistency_report(
            session_dir, candidate, serial_log, backup_sha, analysis
        )
    )
    rollback_after_identity_failure: dict[str, Any] | None = None
    if not boot_identity_verified:
        rollback_errors = execute_rollback(
            rollback_region_commands(
                args.port, candidate, original_assets_file, original_otadata_file
            )
        )
        rollback_after_identity_failure = {
            "attempted": True,
            "complete": not rollback_errors,
            "errors": rollback_errors,
        }
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "variant": args.variant,
        "port": args.port,
        "firmware_sha256": manifest["firmware_sha256"],
        "app_elf_sha256": candidate["app_elf_sha256"],
        "assets_sha256": candidate["assets_sha256"],
        "preflash_backup_sha256": backup_sha,
        "prewrite_live_evidence": prewrite_evidence,
        "assets_write_skipped": assets_already_installed,
        "preserved_original_ota_slot": analysis["active"]["slot"],
        "candidate_ota_slot": analysis["target_slot"],
        "serial_boot_identity_verified": boot_identity_verified,
        "rollback_after_identity_failure": rollback_after_identity_failure,
        "serial_log": str(serial_log),
        "device_consistency_json": str(consistency_json),
        "device_consistency_markdown": str(consistency_markdown),
    }
    (session_dir / "flash-result.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Device evidence: {session_dir}")
    if not boot_identity_verified:
        if rollback_after_identity_failure and not rollback_after_identity_failure["complete"]:
            print(
                "[ERROR] serial identity was unproved and automatic rollback was incomplete",
                file=sys.stderr,
            )
            return 4
        print(
            "[ERROR] serial log did not prove the candidate ELF identity; original assets and "
            "OTA selection were restored, so do not score this run",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
