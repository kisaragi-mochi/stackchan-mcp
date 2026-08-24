#!/usr/bin/env python3
"""Build a no-flash StackChan voice/audio A/B firmware matrix.

The default mode only prints the plan. ``--execute`` must be launched from an
ESP-IDF 5.5 PowerShell so the inherited ``idf.py`` environment is authoritative.
It never invokes esptool or accesses a serial port. The user's original
sdkconfig.defaults.local is restored even if a build fails.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import build_consistency_report
import configure_stackchan


SCRIPT_DIR = Path(__file__).resolve().parent
FIRMWARE_ROOT = SCRIPT_DIR.parent
EVALUATION_ROOT = FIRMWARE_ROOT.parent.parent
DEFAULT_ARTIFACT_ROOT = EVALUATION_ROOT / "artifacts" / "firmware-ab"
REQUIRED_IDF_SERIES = "v5.5"

VARIANTS = {
    "a-wakenet": ("xiaozhi-conversational", "wakenet", "xiaozhi-plus-action"),
    # This intentionally selects MN6 while keeping AFE WakeNet active. Current
    # asset packaging skips MN6 in this mode, so B is a blinded negative
    # control for the historical "checkbox-only" observation.  The matrix
    # comparison below proves whether A and B still have equal behavior bytes.
    "b-wakenet-mn6-flag": ("xiaozhi-conversational", "wakenet-mn6-flag", "xiaozhi-plus-action"),
    "c-custom-multinet": ("xiaozhi-conversational", "custom-multinet", "xiaozhi-plus-action"),
    "d-wakenet-device-aec": ("xiaozhi-conversational", "wakenet-device-aec", "xiaozhi-plus-action"),
    "e-wakenet-server-aec": ("xiaozhi-conversational", "wakenet-server-aec", "xiaozhi-plus-action"),
    "f-mcp-single-shot": ("mcp-single-shot", "wakenet", "local-mcp"),
}

FLASH_BUNDLE_FILES = (
    "bootloader/bootloader.bin",
    "partition_table/partition-table.bin",
    "ota_data_initial.bin",
    "generated_assets.bin",
    "xiaozhi.bin",
    "merged-binary.bin",
    "flasher_args.json",
    "flash_args",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def byte_diff_ranges(left: bytes, right: bytes) -> list[dict[str, int]]:
    """Return inclusive contiguous byte ranges that differ in equal-size blobs."""
    if len(left) != len(right):
        raise ValueError("byte_diff_ranges requires equal-size inputs")
    ranges: list[dict[str, int]] = []
    for offset, (left_byte, right_byte) in enumerate(zip(left, right)):
        if left_byte == right_byte:
            continue
        if not ranges or offset != ranges[-1]["end"] + 1:
            ranges.append({"start": offset, "end": offset, "length": 1})
        else:
            ranges[-1]["end"] = offset
            ranges[-1]["length"] += 1
    return ranges


def esp_image_metadata_spans(image: bytes) -> list[dict[str, int | str]]:
    """Locate non-behavioral ESP app metadata allowed to vary per clean build."""
    app_desc = image.find(build_consistency_report.APP_DESC_MAGIC, 0, 1024)
    if app_desc < 0 or len(image) < 33:
        return []
    # esp_app_desc_t: time/date are build labels and app_elf_sha256 is the
    # identity of the linked ELF. ESP-IDF appends one checksum byte plus the
    # image SHA256 at the end of a hash-appended image.
    return [
        {"name": "build_time", "start": app_desc + 80, "end": app_desc + 96},
        {"name": "build_date", "start": app_desc + 96, "end": app_desc + 112},
        {"name": "app_elf_sha256", "start": app_desc + 144, "end": app_desc + 176},
        {"name": "image_checksum_and_sha256", "start": len(image) - 33, "end": len(image)},
    ]


def compare_variants(run_dir: Path, left_name: str, right_name: str) -> dict[str, object]:
    """Prove whether two candidates differ only in build identity metadata."""
    left_app_path = run_dir / left_name / "xiaozhi.bin"
    right_app_path = run_dir / right_name / "xiaozhi.bin"
    left_assets_path = run_dir / left_name / "flash-bundle" / "generated_assets.bin"
    right_assets_path = run_dir / right_name / "flash-bundle" / "generated_assets.bin"
    left_app = left_app_path.read_bytes()
    right_app = right_app_path.read_bytes()
    same_size = len(left_app) == len(right_app)
    diff_ranges = byte_diff_ranges(left_app, right_app) if same_size else []
    metadata_spans = esp_image_metadata_spans(left_app) if same_size else []
    differing_offsets = (
        [offset for item in diff_ranges for offset in range(item["start"], item["end"] + 1)]
        if same_size
        else []
    )
    metadata_only = bool(metadata_spans) and all(
        any(span["start"] <= offset < span["end"] for span in metadata_spans)
        for offset in differing_offsets
    )
    assets_equal = (
        left_assets_path.stat().st_size == right_assets_path.stat().st_size
        and sha256(left_assets_path) == sha256(right_assets_path)
    )
    behavior_payload_equal = same_size and metadata_only and assets_equal
    return {
        "left": left_name,
        "right": right_name,
        "purpose": "hidden-mn6-checkbox-negative-control",
        "app_size_equal": same_size,
        "app_diff_byte_count": sum(item["length"] for item in diff_ranges),
        "app_diff_ranges": diff_ranges,
        "allowed_metadata_spans": metadata_spans,
        "app_differences_metadata_only": metadata_only,
        "assets_equal": assets_equal,
        "left_assets_sha256": sha256(left_assets_path),
        "right_assets_sha256": sha256(right_assets_path),
        "behavior_payload_equal": behavior_payload_equal,
        "interpretation": (
            "A/B is a negative control: any measured recognition difference is not caused "
            "by different executable behavior or packaged speech models."
            if behavior_payload_equal
            else "A/B contains a runtime-relevant difference and must be reviewed before scoring."
        ),
    }


def selected_variants(names: list[str] | None) -> list[tuple[str, str, str, str]]:
    requested = names or list(VARIANTS)
    unknown = [name for name in requested if name not in VARIANTS]
    if unknown:
        raise ValueError("Unknown variants: " + ", ".join(unknown))
    return [(name, *VARIANTS[name]) for name in requested]


def build_command() -> list[str]:
    return [sys.executable, "./scripts/release.py", "stackchan"]


def idf_version() -> str:
    idf_path = os.environ.get("IDF_PATH")
    if not idf_path:
        raise RuntimeError(
            "IDF_PATH is not set; launch this command from ESP-IDF 5.5 PowerShell"
        )
    idf_script = Path(idf_path) / "tools" / "idf.py"
    if not idf_script.exists():
        raise RuntimeError(f"idf.py not found under IDF_PATH: {idf_script}")
    result = subprocess.run(
        [sys.executable, str(idf_script), "--version"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    version = result.stdout.strip() or result.stderr.strip()
    if REQUIRED_IDF_SERIES not in version:
        raise RuntimeError(
            f"Expected ESP-IDF {REQUIRED_IDF_SERIES}.x PowerShell, got: {version}"
        )
    return version


def validate_build_dir(build_dir: Path, firmware_root: Path) -> None:
    resolved_root = firmware_root.resolve()
    resolved_build = build_dir.resolve()
    if resolved_build.parent != resolved_root or resolved_build.name != "build":
        raise RuntimeError(f"Refusing to clean unexpected build directory: {resolved_build}")


def preserve_preexisting_build(firmware_root: Path, run_dir: Path) -> None:
    binary = firmware_root / "build" / "xiaozhi.bin"
    if not binary.exists():
        return
    target = run_dir / "_preexisting"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, target / "xiaozhi.bin")
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "source": str(binary),
                "sha256": sha256(binary),
                "mtime": datetime.fromtimestamp(
                    binary.stat().st_mtime, timezone.utc
                ).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def clean_build_dir(firmware_root: Path) -> None:
    build_dir = firmware_root / "build"
    validate_build_dir(build_dir, firmware_root)
    if build_dir.exists():
        shutil.rmtree(build_dir)


def apply_profile(local_file: Path, voice: str, audio: str, transport: str) -> None:
    result = configure_stackchan.main(
        [
            "--voice-mode",
            voice,
            "--audio-profile",
            audio,
            "--transport-profile",
            transport,
            "--local-file",
            str(local_file),
        ]
    )
    if result != 0:
        raise RuntimeError(f"Failed to apply profile {voice}/{audio}")


def archive_variant(
    firmware_root: Path,
    variant_dir: Path,
    name: str,
    voice: str,
    audio: str,
    transport: str,
) -> dict[str, object]:
    binary = firmware_root / "build" / "xiaozhi.bin"
    if not binary.exists():
        raise RuntimeError(f"Build finished without {binary}")
    variant_dir.mkdir(parents=True, exist_ok=True)
    target_binary = variant_dir / "xiaozhi.bin"
    shutil.copy2(binary, target_binary)

    bundle_dir = variant_dir / "flash-bundle"
    for relative_name in FLASH_BUNDLE_FILES:
        source = firmware_root / "build" / relative_name
        if not source.is_file():
            raise RuntimeError(f"Complete flash bundle is missing {source}")
        target = bundle_dir / relative_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    report = build_consistency_report.build_report(firmware_root, None)
    (variant_dir / "consistency.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (variant_dir / "consistency.md").write_text(
        build_consistency_report.markdown_report(report), encoding="utf-8"
    )
    if not report["gates"]["technical_ready_for_flash_review"]:
        error_codes = [
            item["code"] for item in report["findings"] if item["severity"] == "error"
        ]
        raise RuntimeError(
            f"Built variant {name} failed consistency gates: "
            + ", ".join(error_codes)
        )
    manifest = {
        "variant": name,
        "voice_profile": voice,
        "audio_profile": audio,
        "transport_profile": transport,
        "firmware_sha256": sha256(target_binary),
        "firmware_size": target_binary.stat().st_size,
        "app_elf_sha256": build_consistency_report.app_elf_sha256(target_binary),
        "full_flash_sha256": sha256(bundle_dir / "merged-binary.bin"),
        "full_flash_size": (bundle_dir / "merged-binary.bin").stat().st_size,
        "flash_bundle": "flash-bundle",
        "technical_ready_for_flash_review": report["gates"][
            "technical_ready_for_flash_review"
        ],
        "flash_authorized_by_user": False,
    }
    (variant_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def print_plan(variants: list[tuple[str, str, str, str]], artifact_root: Path) -> None:
    print(f"Build environment: ESP-IDF {REQUIRED_IDF_SERIES}.x PowerShell")
    print(f"Artifact root: {artifact_root}")
    print("Flash command: NEVER CALLED")
    for name, voice, audio, transport in variants:
        print(f"- {name}: voice={voice}, audio={audio}, transport={transport}")


def execute(
    variants: list[tuple[str, str, str, str]],
    firmware_root: Path,
    artifact_root: Path,
) -> Path:
    active_idf_version = idf_version()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = artifact_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    preserve_preexisting_build(firmware_root, run_dir)

    local_file = firmware_root / "sdkconfig.defaults.local"
    local_existed = local_file.exists()
    original_local = local_file.read_bytes() if local_existed else b""
    manifests: list[dict[str, object]] = []
    try:
        for name, voice, audio, transport in variants:
            print(f"=== Building {name} ===", flush=True)
            apply_profile(local_file, voice, audio, transport)
            clean_build_dir(firmware_root)
            subprocess.run(build_command(), cwd=firmware_root, check=True)
            manifests.append(
                archive_variant(
                    firmware_root, run_dir / name, name, voice, audio, transport
                )
            )
    finally:
        if local_existed:
            local_file.write_bytes(original_local)
        elif local_file.exists():
            local_file.unlink()

    comparisons: list[dict[str, object]] = []
    built_names = {str(manifest["variant"]) for manifest in manifests}
    if {"a-wakenet", "b-wakenet-mn6-flag"}.issubset(built_names):
        comparisons.append(
            compare_variants(run_dir, "a-wakenet", "b-wakenet-mn6-flag")
        )

    (run_dir / "matrix.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "idf_version": active_idf_version,
                "flash_invoked": False,
                "variants": manifests,
                "comparisons": comparisons,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--variant", action="append", choices=list(VARIANTS))
    parser.add_argument("--firmware-root", type=Path, default=FIRMWARE_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    args = parser.parse_args(argv)

    try:
        variants = selected_variants(args.variant)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print_plan(variants, args.artifact_root)
    if not args.execute:
        print("Plan only. Pass --execute from ESP-IDF 5.5 PowerShell.")
        return 0
    try:
        run_dir = execute(
            variants,
            args.firmware_root.resolve(),
            args.artifact_root.resolve(),
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] A/B build failed: {exc}", file=sys.stderr)
        return 1
    print(f"A/B artifacts: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
