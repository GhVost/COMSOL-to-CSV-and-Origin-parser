# Changelog

## v1.9.0 — 2026-07-02

Deformation preview and large-model memory reliability release.

### Fixed
- **Large-file memory usage.** `parse_comsol_export`/`load_dataset_csv` used
  to read the whole export/CSV as one string just to find the leading `%`
  comment lines, peaking at several times the file size; both now stream
  only the header block. Measured on an 18.5 MB synthetic export: peak
  memory dropped from ~64 MB to ~32 MB.
- Bulk extraction no longer retains every selected item's full DataFrame
  for the whole run - only when pushing to OriginLab (where it's needed),
  and `push_to_origin` now frees each dataset immediately after Origin has
  taken a copy, instead of holding the entire batch until the project saves.

### Added
- **`--low-memory` / "Low memory (parse as float32)"** - parses tables and
  plots into float32 instead of float64, halving the memory each extracted
  dataset holds onto (precision loss below COMSOL's own exported digits).
  Off by default; available on the CLI, the window, and threaded through
  previews, bulk extraction, and the `--origin`-only CSV reload path.
- Preview plots now cap what's actually drawn at ~50,000 points
  (`subsample_for_plot`, a systematic every-Nth-row sample) for large 2D/3D
  surfaces and line series - matplotlib chokes on far fewer points than
  pandas does, independent of parsing memory. The Data tab and the exported
  CSV are unaffected; only the plotted preview is thinned.
- The deformation-exaggeration reference pass (see below) is skipped for
  exports over 150 MB, so it can't double an already-large plot's memory use.
- **Deformation exaggeration** — 2D/3D plot groups with an active
  Deformation sub-feature now get a second, temp-only COMSOL export with
  the feature switched off, recovering the undeformed reference geometry.
  Previews of such plots gain a 0-500% exaggeration slider (default 100% =
  COMSOL's own configured scale) that reshapes the geometry live; the
  feature's scale factor is also recorded as a `%` comment in the export.
- **Mask hostnames** checkbox in the license-usage tab, obscuring
  workstation names in the displayed report (the host filter still matches
  the real name). The setting is saved to
  `%APPDATA%\COMSOLExtractor\settings.json` and restored on the next start.
- The COMSOL status LED now turns green as soon as the COMSOL engine has
  started, rather than waiting for the model to finish loading.

## v1.8.0 — 2026-07-02

COMSOL-fidelity release for line/legend/3D previews and Origin plots.

### Added
- **Adjustable 3D surface opacity** — a slider (15–100%) on 3D previews
  live-updates the surface's transparency, so interior detail behind the
  outer surface is no longer hidden.
- **`tests/`** — pytest suite covering the CSV round-trip, header/legend
  parsing, and line-sweep splitting; `pyproject.toml` adds pytest/ruff
  config.
- `requirements-origin.txt` / `requirements-dev.txt` split out of
  `requirements.txt` (Origin and dev/build tooling installed as optional
  sets by `install_requirements.py`).

### Changed
- **Legend replication** — curve legends in both the preview and the
  Origin worksheet now come from COMSOL's own legend text where available
  (`get_plot_legend_labels`), falling back to the curve-label suffix
  encoded in COMSOL's column headers (e.g. `Iout (mA), V_dc=1 V`).
- **Line-only Origin import** — imported line series (tables/1D plots) are
  added to the Origin graph as **line only** (no symbols), matching
  COMSOL's own line plots instead of Origin's default line+symbol style;
  the legend is rebuilt from the corrected series names.
- Parametric line sweeps exported as one stitched x/y pair are split into
  separate series (preview and Origin alike) using COMSOL's legend text
  for naming instead of generic `y1`/`y2`.

## v1.7.0 — 2026-07-02

Workflow and license-monitoring release.

### Added
- **Open button** — the app starts straight into the combined window; pick
  (or re-pick) the `.mph` model from there instead of a blocking startup
  file dialog. A CLI model path still loads immediately.
- **Host filter** for the license-usage report (default `*-*`, fnmatch
  patterns like `impt-*`), applied live without re-querying the server.
- **Date stamp** `Extracted: YYYYMMDD` recorded with every dataset (CSV `%`
  comments, `manifest.json`, Origin worksheet comments).
- After an OriginLab export, the saved `comsol_results.opju` is opened in a
  fresh Origin instance automatically.

### Changed
- Previews now open on a **single click** of an item (the separate Preview
  button is gone); buttons are arranged in work-sequence order
  Open >> Select/Deselect >> Extract.
- `lmstat` output parsing handles uncounted/node-locked features and strips
  `PID:` suffixes; the license finder accepts `lmutil.exe` (COMSOL 6.x
  ships no standalone `lmstat.exe`).
- Code split into focused modules: `extraction.py`, `origin_push.py`,
  `license_check.py`, `gui.py`, with `COMSOLExtractor.py` as the entry
  point.

## v1.6.0 — 2026-07-02

### Added
- **Preview tabs** in an MDI area: each table/plot group opens a tab with a
  matplotlib **Plot** view (line chart for tables/1D, value-colored scatter
  for 2D/3D; colorblind-safe palette) and a read-only **Data** grid.
- **License usage** button: runs FlexNet's lmstat from the local COMSOL
  installation and reports which users hold seats of each COMSOL module
  (FNL licenses).
- `matplotlib` dependency for the preview plots.

## v1.5.0 — 2026-07-02

### Changed
- GUI ported from tkinter to **PySide6** (Qt): same combined status/items
  window, native file dialogs, background loading via Qt signals instead of
  a polling loop.

### Fixed
- A COMSOL server started in the background is now shut down when the
  window is closed while the model is still loading (previously the process
  and its license seat could leak).
