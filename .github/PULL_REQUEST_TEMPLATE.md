## Summary

<!-- What changed and why? Keep this focused on the public technical context. -->

## Test plan

### Code-level

<!-- CI runs these where applicable. Check the ones you ran locally, or explain why not. -->

- [ ] gateway: `uv run pytest`
- [ ] gateway: `uv run ruff check .`
- [ ] firmware: `python ./scripts/release.py stackchan`

### Hardware

<!-- Required for firmware changes. Write "N/A - gateway/docs only" when hardware is not affected. -->

- [ ] Device boots without crash
- [ ] Existing MCP tools still work where affected: `move_head`, `take_photo`, `set_volume`, `get_head_angles`
- [ ] Existing touch/servo behavior still works where affected: tap, stroke, wobble
- [ ] New behavior verified on real hardware: <!-- details -->

## Breaking changes

<!-- MCP tool API changes, NVS schema changes, build flag changes, or "None". -->

## Related issues

<!-- Closes #N / Refs #N -->
