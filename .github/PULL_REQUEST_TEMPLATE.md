## Summary

<!-- What changed and why? Keep this focused on the public technical context. -->

## Test plan

### Code-level

<!-- Check what you ran locally. It is OK to leave boxes unchecked if CI will cover them or they do not apply. -->

- [ ] gateway: `uv run pytest`
- [ ] gateway: `uv run ruff check .`
- [ ] firmware: `python ./scripts/release.py stackchan`
- [ ] Not applicable / not run: <!-- why -->

### Hardware

<!-- For firmware changes, describe any real-device testing. If you do not have hardware, say so clearly so a maintainer can help verify before merge. -->

- [ ] Device boots without crash
- [ ] Existing MCP tools still work where affected: `move_head`, `take_photo`, `set_volume`, `get_head_angles`
- [ ] Existing touch/servo behavior still works where affected: tap, stroke, wobble
- [ ] New behavior verified on real hardware: <!-- details -->
- [ ] Not applicable / hardware not available: <!-- why -->

## Breaking changes

<!-- MCP tool API changes, NVS schema changes, build flag changes, or "None". -->

## Related issues

<!-- Closes #N / Refs #N -->
