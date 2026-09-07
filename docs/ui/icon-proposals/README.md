# Icon exploration

Status: reference-aligned SVG approved by the user and applied to the UI and Tauri PNG/ICO resources.

- Initial flat A/B/C directions rejected as too simple.
- `modern-concepts.png`: built-in image_gen exploration; user preferred B, requested flat style.
- `theme-flat-b.png`: built-in image_gen refinement to theme gray-purple and a three-face cube. This raster contains a checkerboard, so it is reference-only, not a valid transparent shipping asset.
- `refined-b.svg`: rejected manual reinterpretation; its proportions differed from the reference.
- `reference-aligned-b.svg`: current review candidate, traced from the selected reference with transparent outer margins and a lighter cube divider.
- `reference-review.html`: original, SVG and 50% overlay comparison, plus small-size previews. Browser inspection found no obvious doubled silhouette at the displayed comparison scale.
- `trace_reference.py`: reproducible contour extraction; run using `./scripts/codexhub-python.sh docs/ui/icon-proposals/trace_reference.py`.

Generation prompts requested a model-connection hub with a central core and symmetric enclosure, then removed glass/metal/light effects, changed the palette to #8971A7 / #BCA2D8 / gray-purple, and added three cube faces. The current SVG uses traced vector contours rather than an embedded raster. Cube face colors are flat; the original's subtle raster texture is omitted.
