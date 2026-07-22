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

Eyes and mouths recombine with several faces — but only in your
authoring tool, at export time. On the device there is no compositor:
the firmware always shows exactly one full-frame RGB565 image at a
time, and RGB565 carries no alpha. In layered mode a blink or mouth
change swaps the whole screen to the eyes or mouth frame; a matrix
set bakes every face × eyes × mouth combination ahead of time. Either
way, every exported frame must be a complete full-frame image — a
part exported with transparency renders as a broken face the first
time its frame is selected.

Inside the authoring file, still build the moving parts as separate
layers with clean alpha: a part cropped out of an already-composited
picture carries its old background along its edges — anti-aliased
pixels, compression artifacts, a halo of the previous face's shading —
and that edge shows up as a faint outline that appears and disappears
as frames cycle. Composite the layers onto the face and flatten at
export time. If all you have is a flat reference image, treat it as a
reference to redraw from, not as a source to crop from.

## Mind the fetch window

A matrix set is a ~3.3 MB transfer. Over a healthy link it is quick,
but with Wi-Fi power save active (the modem idles between beacons) we
have measured a full fetch taking two to three minutes.

The fetch runs on its own task on the device: the WebSocket loop keeps
processing commands, so `say` and other non-avatar traffic go through
normally during the download. What is held back are the avatar-facing
updates — face, mouth and blink changes are quiesced until the new set
is adopted — so sequencing matters for avatar-affecting calls, not for
commands in general.

The wait itself belongs to `load_avatar_set`: the call blocks until
the device confirms adoption, bounded by its own `timeout` argument
(default 60 s, maximum 300 s). A multi-minute power-save fetch
therefore needs that per-call `timeout` raised to the 180–300 s range,
or the call returns `device_timeout` while the device is still
downloading. Raising timeouts on subsequent commands does not extend
the fetch wait. The gateway logs the device's GET, which is the
easiest way to watch a slow fetch make progress.

## Blink cadence is a per-face aesthetic

The firmware blinks autonomously on a randomized gap (3–6 s by
default, constants in `firmware/main/boards/stackchan/stackchan.cc`).
How that cadence reads depends on the face: small, stylized faces tend
to read better with quicker blinks, while realistic proportions stay
calm on the stock gap. When a new set feels subtly wrong — too static,
or too nervous — the blink gap is worth checking before touching the
art. The bounds are build-time constants that apply to every avatar
set on the device: changing them means editing the constants and
reflashing, and the new cadence applies device-wide, not per set. Tune
them for the set the device actually runs day to day.
