# Changelog

## v1.11.0 — 2026-07-08

Category selection, human-readable naming, complete sweep-parameter legends,
and an honest remaining-time estimate.

### Fixed
- **Remaining-time estimate no longer sticks at 0:00:00.** v1.10.0 predicted
  each item's time once at its start and counted down, so any item running
  past its prediction pinned the display to zero for the rest of that item.
  The estimate is now pace-based (elapsed time extrapolated over the
  work-weighted fraction completed, recomputed every half-second): it can
  never show zero while items remain and rises honestly when an item
  overruns its history.
- **Origin long names lost all but the innermost sweep parameter.** For
  nested sweeps COMSOL headers like `R (Ω), gap=2 µm, ring=1.3 µm` were
  reduced to `ring=1.3 µm`, making same-valued inner-sweep columns
  indistinguishable. The long name now keeps the full parameter list, and
  the complete original header (measured quantity included) is preserved in
  each column's Comments row.
- Item durations are measured with `perf_counter` - `monotonic` ticks in
  15.6 ms steps on Windows and recorded fast items as 0-second history.

### Changed
- The status line during extraction shows the total elapsed time instead of
  the confusing per-item timer, and the progress bar advances by each
  item's expected share of the work (so it keeps moving through one long
  item) rather than by item count.
- **Human-readable names everywhere:** the checklist, preview tabs, CSV
  filenames, and Origin books use the COMSOL label ("Probe Table 1"), not
  internal tags like `pg66`/`tbl1`. The tag is appended only to break a
  duplicate-label collision, remains in `manifest.json`, and shows in the
  item's tooltip.
- The window is no longer always-on-top.

### Added
- **Category selection** - the checklist group headings (Probe Tables /
  Tables / 1D / 2D / 3D Plots) are themselves checkboxes that select or
  deselect the whole category at once. Probe tables are split out of
  Tables by their COMSOL label (extraction-wise they stay ordinary tables).

## v1.10.0 — 2026-07-08

In-window extraction progress with size-aware time estimates.

### Changed
- **The window now stays open during extraction** instead of closing the
  moment Extract is clicked: controls grey out, a progress bar counts the
  items, and the status line shows the current item with its ticking
  elapsed time and an estimated time remaining. Closing the window
  mid-extraction is ignored (the underlying COMSOL call cannot be
  cancelled); if extraction fails, the window stays open with the error in
  the status line. Previously the window vanished and all progress went to
  the console only - which on a huge model looked like a crash while a
  single silent COMSOL call was still running.
- The extraction loop moved out of `main()` into `extract_selected()`,
  driven by the window through a `progress(done, total, label, eta)`
  callback; console output is unchanged.

### Added
- **Size-aware remaining-time estimate.** Every run records each item's
  actual duration and row count to `.extract_timing.json` in the results
  folder. A re-run of the same model predicts each remaining item's time
  from its own history - so one half-million-row plot among small probe
  tables is weighted as such, not averaged away - and continuously rescales
  all predictions by the ratio of this run's actual vs predicted durations
  (e.g. after a finer re-solve everything runs ~2x longer and the estimate
  stretches accordingly). Items with no history use the current run's
  average; a first-ever run shows elapsed time only until the first item
  completes.
- `tests/test_extract_selected.py` - progress/ETA callback sequence,
  CSV/manifest output, timing-history save and reuse.

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
