"""Verify discovery is enabled and present in the distributed StackChan app."""

import json
import sys
import zipfile
from pathlib import Path

project = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
config = set((project / "sdkconfig").read_text().splitlines())

for key in (
    "BOARD_TYPE_STACKCHAN",
    "STACKCHAN_MDNS_DISCOVERY",
    "STACKCHAN_SERVO_FEETECH",
    "STACKCHAN_SERVO_SCSCL",
    "STACKCHAN_SERVO_DELEGATED_MOTION",
    "STACKCHAN_AUTO_TORQUE_RELEASE_ENABLED",
    "USE_AFE_WAKE_WORD",
    "USE_AUDIO_PROCESSOR",
    "USE_HOTSPOT_WIFI_PROVISIONING",
    "SPIRAM_MODE_QUAD",
    "CAMERA_GC0308",
    "FLASH_DEFAULT_ASSETS",
    "USE_DEFAULT_MESSAGE_STYLE",
    "LV_USE_FREETYPE",
    "LV_USE_LIBJPEG_TURBO",
    "LV_USE_LIBPNG",
    "LV_USE_LZ4",
):
    print(f"CONFIG_{key}={'y' if f'CONFIG_{key}=y' in config else 'n'}")

for key in ("BOARD_TYPE_STACKCHAN", "STACKCHAN_MDNS_DISCOVERY"):
    if f"CONFIG_{key}=y" not in config:
        sys.exit(f"CONFIG_{key} must be enabled in distributed StackChan firmware")

build = project / "build"
flash = json.loads((build / "flasher_args.json").read_text())["app"]
app = (build / flash["file"]).read_bytes()
for marker in (b"_stackchan-mcp\0", b"mDNS discovery unavailable: mdns_init failed: %s"):
    if marker not in app:
        sys.exit(f"Discovery marker missing from app: {marker!r}")

offset = int(flash["offset"], 0)
merged = (build / "merged-binary.bin").read_bytes()
if merged[offset:offset + len(app)] != app:
    sys.exit("Merged firmware does not contain the checked app")

version = json.loads((build / "project_description.json").read_text())["project_version"]
archive = project / "releases" / f"v{version}_stackchan.zip"
with zipfile.ZipFile(archive) as release:
    if release.read("merged-binary.bin") != merged:
        sys.exit("Release zip does not contain the checked merged firmware")

print("mDNS artifact check passed: discovery present in app, merged firmware and release zip")
