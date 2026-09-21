#!/usr/bin/env python3
"""Draw a classic Stack-chan matrix (6×3×5) and pack RGB565 for load_avatar_set."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

W, H = 160, 120
KIMG = W * H * 2
OUT_DIR = Path(__file__).resolve().parent
PNG_DIR = OUT_DIR / "png"
BIN_PATH = OUT_DIR / "classic-matrix.rgb565"

FACES = ["idle", "happy", "thinking", "sad", "surprised", "embarrassed"]
EYES = ["open", "half", "closed"]
MOUTHS = ["closed", "half", "open", "e", "u"]

BG = (0, 0, 0)
WHITE = (255, 255, 255)
BLUSH = (255, 128, 160)


def rgb565_le(im: Image.Image) -> bytes:
    if im.mode != "RGB":
        im = im.convert("RGB")
    src = im.tobytes()
    out = bytearray(len(src) // 3 * 2)
    j = 0
    for i in range(0, len(src), 3):
        r, g, b = src[i], src[i + 1], src[i + 2]
        packed = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[j] = packed & 0xFF
        out[j + 1] = packed >> 8
        j += 2
    return bytes(out)


def ellipse(draw: ImageDraw.ImageDraw, cx: float, cy: float, rx: float, ry: float, fill) -> None:
    draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=fill)


def draw_face(face: str, eyes: str, mouth: str) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    cx, cy = 80.0, 50.0
    spread = 28.0
    left, right = cx - spread, cx + spread

    eye_rx, eye_ry = 20.0, 22.0
    pupil_r = 8.5
    pupil_dx = pupil_dy = 0.0

    if face == "happy":
        eye_ry = 16.0
        pupil_dy = -1.0
    elif face == "thinking":
        pupil_dx, pupil_dy = 4.0, -5.0
    elif face == "sad":
        eye_ry = 18.0
        pupil_dy = 5.0
    elif face == "surprised":
        eye_rx, eye_ry = 23.0, 26.0
        pupil_r = 10.0
    elif face == "embarrassed":
        eye_rx, eye_ry = 17.0, 16.0
        pupil_dy = 2.0

    def one_eye(ex: float) -> None:
        if eyes == "closed":
            d.line((ex - eye_rx + 2, cy, ex + eye_rx - 2, cy), fill=WHITE, width=3)
            return
        ry = eye_ry * (0.42 if eyes == "half" else 1.0)
        ellipse(d, ex, cy, eye_rx, ry, WHITE)
        if eyes == "half":
            d.rectangle((ex - eye_rx - 1, cy - eye_ry - 1, ex + eye_rx + 1, cy - 1), fill=BG)
            ellipse(d, ex, cy, eye_rx, ry, WHITE)
        px = ex + pupil_dx
        py = cy + pupil_dy
        if eyes == "half":
            py = cy + 2
        ellipse(d, px, py, pupil_r, pupil_r, BG)
        ellipse(d, px - 2.5, py - 2.5, 2.2, 2.2, WHITE)

    one_eye(left)
    one_eye(right)

    if face == "embarrassed":
        ellipse(d, left - 6, 78, 8, 4, BLUSH)
        ellipse(d, right + 6, 78, 8, 4, BLUSH)

    mx, my = 80.0, 92.0
    if face == "happy" and mouth == "closed":
        d.arc((mx - 12, my - 8, mx + 12, my + 8), 20, 160, fill=WHITE, width=3)
    elif face == "sad" and mouth == "closed":
        d.arc((mx - 10, my - 2, mx + 10, my + 12), 200, 340, fill=WHITE, width=3)
    elif face == "thinking" and mouth == "closed":
        d.line((mx - 6, my, mx + 6, my), fill=WHITE, width=3)
    elif mouth == "closed":
        ellipse(d, mx, my, 4, 2, WHITE)
    elif mouth == "half":
        ellipse(d, mx, my, 7, 5, WHITE)
    elif mouth == "open":
        ellipse(d, mx, my + 1, 9, 8, WHITE)
    elif mouth == "e":
        ellipse(d, mx, my, 12, 5, WHITE)
    elif mouth == "u":
        d.arc((mx - 7, my - 6, mx + 7, my + 8), 20, 160, fill=WHITE, width=3)

    if face == "surprised" and mouth == "closed":
        ellipse(d, mx, my, 6, 7, WHITE)

    return im


def main() -> None:
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    frames: list[bytes] = []
    for face in FACES:
        for eyes in EYES:
            for mouth in MOUTHS:
                im = draw_face(face, eyes, mouth)
                frames.append(rgb565_le(im))
                if eyes == "open" and mouth == "closed":
                    im.resize((320, 240), Image.Resampling.NEAREST).save(
                        PNG_DIR / f"{face}.png"
                    )
                if face == "idle" and mouth == "closed":
                    im.save(PNG_DIR / f"eyes_{eyes}.png")
                if face == "idle" and eyes == "open":
                    im.save(PNG_DIR / f"mouth_{mouth}.png")

    payload = b"".join(frames)
    expected = 90 * KIMG
    if len(payload) != expected:
        raise SystemExit(f"size {len(payload)} != {expected}")
    BIN_PATH.write_bytes(payload)
    print(f"wrote {BIN_PATH} ({len(payload)} bytes)")
    print(f"previews in {PNG_DIR}")


if __name__ == "__main__":
    main()
