#!/usr/bin/env python3
"""Convert StackChan avatar PNGs into LVGL RGB565 C arrays for the firmware.

Source (faces, Phase 1):
    ~/.stackchan/avatar/{idle,happy,thinking,sad,surprised,embarrassed}.png
Source (eyes/mouths, Phase 2):
    ~/.stackchan/avatar/{eyes_open,eyes_half,eyes_closed}.png
    ~/.stackchan/avatar/{mouth_closed,mouth_half,mouth_open,mouth_e,mouth_u}.png

Default output: main/boards/stackchan/avatar_images.local.{h,cc}

Why RGB565 fixed binary instead of PNG:
  - The StackChan sdkconfig has CONFIG_LV_USE_PNG / BMP / GIF all unset, so
    LVGL cannot decode PNGs at runtime. We must ship raw lv_image_dsc_t arrays.

Why downscale to 160x120:
  - Original 320x240 RGB565 = 153,600 bytes/frame * 14 = ~2.1 MB.
  - The active OTA partition is 0x3F0000 (~3.9 MB), shared with the application
    binary. 2.1 MB of constant data would overwhelm the OTA slot.
  - 160x120 RGB565 = 38,400 bytes/frame * 14 = ~525 KB. Acceptable, and the
    image is later upscaled by LVGL via lv_image_set_scale() so it fills most
    of the 320x240 LCD without per-pixel resampling cost on flash.

Phase 2 design (full-frame swap for parts):
  - Eyes/mouths are stored as 320x240 full-frame PNGs (designed by the artist
    so each part image is the complete avatar with only that part changed).
  - StackChan firmware swaps the lv_image src directly when blinking or
    speaking, then restores the last-applied face image. This keeps the
    runtime simple (no LVGL alpha-blending of partial sprites) at the cost
    of extra flash for whole-frame variants. See report Phase 2 for trade-off.

Layout written into the C source matches LVGL 9.x lv_image_dsc_t with
LV_COLOR_FORMAT_RGB565 (= 0x12). stride = w * 2 (no row padding).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

EMOTIONS = ["idle", "happy", "thinking", "sad", "surprised", "embarrassed"]
# Phase 2 part assets. Variable names in the generated C use the *full*
# source name (e.g. "avatar_eyes_open", "avatar_mouth_e") so the firmware
# look-up keys stay 1:1 with the PNG filenames.
EYES = ["eyes_open", "eyes_half", "eyes_closed"]
MOUTHS = ["mouth_closed", "mouth_half", "mouth_open", "mouth_e", "mouth_u"]
DEFAULT_SRC = Path.home() / ".stackchan" / "avatar"
# Default output: <repo root>/main/boards/stackchan/, resolved relative to
# this script's location. Works for both the upstream xiaozhi-esp32 layout
# (scripts/avatar_convert/...) and the public stackchan-mcp monorepo layout
# (firmware/scripts/avatar_convert/...) as long as the firmware tree under
# main/ is intact.
DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent.parent / "main" / "boards" / "stackchan"
)
TARGET_W = 160
TARGET_H = 120


def rgb888_to_rgb565_bytes(im: Image.Image) -> bytes:
    """Pack RGB888 PIL image into little-endian RGB565 bytes (LVGL native)."""
    if im.mode != "RGB":
        im = im.convert("RGB")
    pixels = im.tobytes()
    out = bytearray(len(pixels) // 3 * 2)
    j = 0
    for i in range(0, len(pixels), 3):
        r = pixels[i]
        g = pixels[i + 1]
        b = pixels[i + 2]
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        # Little-endian (low byte first), matches LV_COLOR_FORMAT_RGB565
        out[j] = rgb565 & 0xFF
        out[j + 1] = (rgb565 >> 8) & 0xFF
        j += 2
    return bytes(out)


def emit_array(name: str, data: bytes) -> str:
    lines = [f"static const LV_ATTRIBUTE_LARGE_CONST uint8_t {name}_map[] = {{"]
    BPL = 16
    for i in range(0, len(data), BPL):
        chunk = data[i : i + BPL]
        lines.append("    " + ",".join(f"0x{b:02x}" for b in chunk) + ",")
    lines.append("};")
    return "\n".join(lines)


def emit_dsc(name: str, w: int, h: int, data_size: int) -> str:
    # cf = 0x12 = LV_COLOR_FORMAT_RGB565 (LVGL 9). magic = 0x19.
    return (
        f"const lv_image_dsc_t {name} = {{\n"
        f"    .header = {{\n"
        f"        .magic = LV_IMAGE_HEADER_MAGIC,\n"
        f"        .cf = LV_COLOR_FORMAT_RGB565,\n"
        f"        .flags = 0,\n"
        f"        .w = {w},\n"
        f"        .h = {h},\n"
        f"        .stride = {w * 2},\n"
        f"        .reserved_2 = 0,\n"
        f"    }},\n"
        f"    .data_size = {data_size},\n"
        f"    .data = {name}_map,\n"
        f"    .reserved = NULL,\n"
        f"    .reserved_2 = NULL,\n"
        f"}};\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--output-stem",
        choices=("avatar_images.local", "avatar_images"),
        default="avatar_images.local",
        help="Output filename stem. Defaults to gitignored local override files.",
    )
    ap.add_argument(
        "--tracked",
        action="store_const",
        const="avatar_images",
        dest="output_stem",
        help="Maintainer-only: overwrite the tracked public placeholder files.",
    )
    ap.add_argument("--width", type=int, default=TARGET_W)
    ap.add_argument("--height", type=int, default=TARGET_H)
    args = ap.parse_args()

    try:
        from PIL import Image
    except ImportError:
        sys.exit("Need Pillow for conversion: pip3 install Pillow")

    src_dir = args.src
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    cc_path = out_dir / f"{args.output_stem}.cc"
    h_path = out_dir / f"{args.output_stem}.h"

    cc_parts = [
        "// Auto-generated by scripts/avatar_convert/convert_avatars.py",
        "// Do not edit by hand. Regenerate after replacing PNG sources.",
        f'#include "{args.output_stem}.h"',
        "",
        '#ifdef __cplusplus',
        'extern "C" {',
        '#endif',
        "",
    ]

    total_bytes = 0

    def convert_one(stem: str) -> None:
        nonlocal total_bytes
        p = src_dir / f"{stem}.png"
        if not p.exists():
            sys.exit(f"missing source: {p}")
        im = Image.open(p)
        # Use LANCZOS for crisp downscale; preserve aspect (320:240 == 4:3 == target).
        im = im.resize((args.width, args.height), Image.LANCZOS)
        data = rgb888_to_rgb565_bytes(im)
        var = f"avatar_{stem}"
        cc_parts.append(emit_array(var, data))
        cc_parts.append("")
        cc_parts.append(emit_dsc(var, args.width, args.height, len(data)))
        cc_parts.append("")
        total_bytes += len(data)
        print(f"  {stem:14s} -> {len(data)} bytes ({args.width}x{args.height})")

    print("Faces:")
    for name in EMOTIONS:
        convert_one(name)
    print("Eyes:")
    for name in EYES:
        convert_one(name)
    print("Mouths:")
    for name in MOUTHS:
        convert_one(name)

    cc_parts.append("#ifdef __cplusplus")
    cc_parts.append("}  // extern \"C\"")
    cc_parts.append("#endif")
    cc_parts.append("")
    cc_path.write_text("\n".join(cc_parts))

    h_parts = [
        f"// Auto-generated header. See {args.output_stem}.cc.",
        "#pragma once",
        "",
        "#include <lvgl.h>",
        "",
        "#ifdef __cplusplus",
        'extern "C" {',
        "#endif",
        "",
    ]
    h_parts.append("// Phase 1: full-face expressions")
    for name in EMOTIONS:
        h_parts.append(f"extern const lv_image_dsc_t avatar_{name};")
    h_parts.append("")
    h_parts.append("// Phase 2: eye states (full-frame swap)")
    for name in EYES:
        h_parts.append(f"extern const lv_image_dsc_t avatar_{name};")
    h_parts.append("")
    h_parts.append("// Phase 2: mouth states (full-frame swap)")
    for name in MOUTHS:
        h_parts.append(f"extern const lv_image_dsc_t avatar_{name};")
    h_parts += [
        "",
        "#ifdef __cplusplus",
        "}",
        "#endif",
        "",
    ]
    h_path.write_text("\n".join(h_parts))

    print(f"\nTotal data: {total_bytes} bytes ({total_bytes/1024:.1f} KB)")
    print(f"Wrote: {cc_path}")
    print(f"Wrote: {h_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
