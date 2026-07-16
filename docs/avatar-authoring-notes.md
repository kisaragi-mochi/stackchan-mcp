# Avatar authoring notes

Field notes from building custom avatar sets for the `load_avatar_set`
pipeline. The wire format is documented on the tool itself (layered mode =
14 frames, face 6 + eyes 3 + mouth 5, 537,600 bytes; matrix mode = 90
pre-composited frames, 6 × 3 × 5, 3,456,000 bytes; both raw RGB565).
These notes cover the authoring side: what makes a set read well on the
device, and the pitfalls we hit so you do not have to.

They are advice, not requirements — nothing here is enforced by the
gateway or the firmware.

## Keep every frame in one geometry

A matrix set is the cartesian product of face × eyes × mouth variants.
The device cuts between those frames constantly (blink, lip-sync,
expression changes), so all 90 frames must share one geometry: same
canvas, same head position, same feature anchor points. A variant that
is redrawn even slightly off — a face a few pixels larger, an eye line
a few pixels higher — reads as a visible jump every time that frame is
selected, not as a subtle difference.

The practical rule: derive every variant from one master image, and
never mix frames from sources drawn at different proportions. If a
variant needs redrawing, redraw it on top of the master's geometry
rather than importing from elsewhere. Scaling a near-miss frame to fit
does not rescue it; the anchors drift even when the canvas matches.

## Moving parts need edit-grade sources

Eyes and mouths are compositing parts: the same part is combined with
several faces (explicitly in layered mode, at bake time for a matrix
set). A part cropped out of an already-composited picture carries its
old background along its edges — anti-aliased pixels, compression
artifacts, a halo of the previous face's shading. Composited onto a
different face variant, that edge shows up as a faint outline that
appears and disappears as frames cycle.

Author the moving parts as separate layers with clean alpha from the
start (or redraw them as such), and composite at export time. If all
you have is a flat reference image, treat it as a reference to redraw
from, not as a source to crop from.

## Mind the fetch window

A matrix set is a ~3.3 MB transfer. Over a healthy link it is quick,
but with Wi-Fi power save active (the modem idles between beacons) we
have measured a full fetch taking two to three minutes. During the
fetch the device is busy downloading; queued commands wait, and a slow
dispatch such as `say` issued right after `load_avatar_set` is likely
to hit the MCP client's timeout before it ever reaches the device.

Practical sequencing: stage the set, wait for the fetch to complete
(the gateway logs the device's GET, and the `load_avatar_set` call
itself blocks until the device confirms), and only then resume normal
traffic. If your host automates a set load at connect time, give the
commands that follow it generous timeouts.

## Blink cadence is a per-face aesthetic

The firmware blinks autonomously on a randomized gap (3–6 s by
default, constants in `firmware/main/boards/stackchan/stackchan.cc`).
How that cadence reads depends on the face: small, stylized faces tend
to read better with quicker blinks, while realistic proportions stay
calm on the stock gap. When a new set feels subtly wrong — too static,
or too nervous — the blink gap is worth checking before touching the
art. Changing the bounds currently means editing the constants and
reflashing, so it is a bake-time decision per avatar set rather than a
runtime knob.
