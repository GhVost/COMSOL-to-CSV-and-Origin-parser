# COMSOL to .csv and/or Origin

Extract result tables and plot groups (1D/2D/3D) from a COMSOL `.mph` model
and save them as CSV files, optionally importing everything directly into an
OriginLab project (`.opju`) - or import a previously extracted folder into
OriginLab without COMSOL at all.

## Requirements

- COMSOL Multiphysics (5.x or 6.x), installed and licensed — only needed for
  `--comsol`
- Python 3.10+
- Python packages: `MPh`, `pandas`, `numpy`, `psutil`, `PySide6` (GUI),
  `matplotlib` (preview plots)
- Optional, for `--origin`: OriginLab installed + `originpro`

Install everything into a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\python install_requirements.py
```

`install_requirements.py` installs the required packages from
`requirements.txt` and then attempts the optional Origin/build tooling from
`requirements-origin.txt` and `requirements-dev.txt`. A COMSOL-only setup can
also be installed directly with `pip install -r requirements.txt`.

## Usage

```powershell
.venv\Scripts\python.exe COMSOLExtractor.py
```

With no `--comsol`/`--origin` flags, the script opens a single combined
window. The buttons follow the work sequence **Open >> Select/Deselect >>
Extract**: click **Open...** to pick the `.mph` model (a COMSOL server is
started and the model loaded in the background), tick the items to extract,
then click **Extract**. The window contains:

- **Status** - a green LED that lights up as soon as the COMSOL engine
  itself has started (before the model finishes loading), plus an
  **OriginLab** row showing whether Origin/OriginPro is currently running
  (LED + "running"/"not running"). A **Start OriginPro** button launches
  OriginPro (via `originpro`/COM) and re-checks its status afterwards,
  updating the LED once it comes up. A checkbox ("Import extracted results
  into OriginLab") controls whether the extraction is pushed into Origin
  afterwards - ticked automatically once Origin is detected as running.
  If a COMSOL process was already running before this session started, a
  warning is shown here too (an extra engine instance/license seat will be
  used).
- **Items to extract** - a checklist of tables/plot groups, grouped by
  category (Probe Tables / Tables / 1D / 2D / 3D Plots), all checked by
  default. Each bold group heading is itself a checkbox that selects or
  deselects its whole category at once. Items are listed by their
  human-readable COMSOL label ("Probe Table 1", "S-parameter"); hover an
  item to see the internal COMSOL tag (`tbl1`, `pg66`, ...). **Clicking** an
  item extracts it once and opens a preview tab in the MDI area on the
  right. Tables open as a **Data** grid only; 1D plots open as separate
  line series (no markers on the line itself) with legend entries taken
  from COMSOL's own curve labels where available, plus a peak marker per
  series; 2D/3D plots open as triangulated surfaces using the exported,
  already-deformed COMSOL coordinates when deformation is active - 3D
  previews add a **surface opacity** slider so interior detail hidden
  behind the outer surface can be seen. If the plot has an active
  **Deformation** sub-feature, both 2D and 3D previews also get a
  **deformation exaggeration** slider (0-500%, default 100% = COMSOL's own
  configured scale) that reshapes the geometry live against the
  undeformed reference captured during export.
- **License usage** - a button in the status section runs FlexNet's
  `lmstat` (or `lmutil lmstat`, COMSOL 6.x) from the local COMSOL
  installation and opens a tab reporting which users currently hold seats
  of each COMSOL module (FNL licenses). A **Host filter** field (default
  `*-*`, an fnmatch pattern like `impt-*`; `*` shows all) narrows the
  report to matching workstations without re-querying the server. A
  **Mask hostnames** checkbox obscures workstation names in the displayed
  report (filtering still matches the real hostname); its state is saved
  to `%APPDATA%\COMSOLExtractor\settings.json` and restored the next time
  the app starts.

Click **Extract** to write the selected items to `<model_name>_results/`
and, if the OriginLab checkbox is ticked, build `comsol_results.opju` in the
same folder. The window stays open while the extraction runs, showing a
progress bar (weighted by each item's expected share of the work, so it
keeps moving through one long item) and a status line with the current
item, the total elapsed time, and an estimated time remaining. The estimate
is pace-based - elapsed time extrapolated over the work fraction still
left - with each item's actual duration and row count recorded to
`.extract_timing.json` in the results folder, so a re-run of the same model
weights every item by how long it took last time (one huge plot among small
tables is predicted as such); a first-ever run shows "estimating..." until
the first item completes. The saved project is then opened in a fresh
Origin instance and the results folder in File Explorer. Every dataset
carries an `Extracted: YYYYMMDD` date stamp in its CSV `%` comments,
`manifest.json`, and Origin worksheet comments.

For the **OriginLab-only** workflow - re-importing a previously extracted
`<model_name>_results/` folder without COMSOL (e.g. COMSOL isn't installed
here, or its license is busy elsewhere) - use `--origin` without `--comsol`
on the command line (see below); this skips the model/COMSOL steps entirely
and instead prompts for the results folder to pack into a new OriginLab
project.

The same choices are available non-interactively via `--comsol` and
`--origin` (either or both); `--comsol` (with or without `--origin`) still
shows the combined window above so you can pick which items to extract and
whether to push to OriginLab.

```powershell
.venv\Scripts\python.exe COMSOLExtractor.py --comsol --origin
```

This opens the combined status/items window with the OriginLab checkbox
pre-ticked; clicking **Extract** writes the CSVs and builds an OriginLab
project (`.opju`).

If `--origin` is given without `--comsol`, OriginLab not running yet is the
only thing checked beforehand with a console prompt (waits for Enter to
continue, or Ctrl+C to abort) - it's not part of the combined window since
that path skips COMSOL/the model entirely.

Other options:

- `COMSOLExtractor.py model.mph --comsol` — extract a specific model (loads
  it immediately, no Open click needed)
- `--output ./out` — custom output directory (COMSOL mode)
- `--origin-template my_template.otpu` — Origin graph template for line plots
- `--low-memory` — parse into float32 instead of float64, halving memory use
  on large tables/plots (see **Large models / low on memory** below);
  pre-ticks the same checkbox in the window
- `--version` — print the version and exit

## Large models / low on memory

Large 2D/3D exports (fine meshes, long parametric sweeps) can use enough
memory to crash the process on its own. A few things help, roughly in order
of impact:

- **Tick "Low memory (parse as float32)"** in the window (or pass
  `--low-memory`) - halves the memory each extracted dataset holds onto,
  for a precision loss well below what COMSOL's own text export already
  rounds to.
- **Extract fewer items at once** if you're selecting many large 2D/3D
  plots together, especially with "Import extracted results into OriginLab"
  ticked - each selected item's data is held until it's written (and, if
  pushing to Origin, until Origin has taken a copy), so a big batch adds up.
- Previews already cap what's actually plotted (not the underlying data) at
  ~50,000 points for large 2D/3D/line datasets, and a plot's deformation
  exaggeration reference pass (see below) is skipped for exports over
  150 MB - both automatic, no configuration needed.

## Building a standalone executable

A single-file `COMSOLExtractor.exe` can be built with
[PyInstaller](https://pyinstaller.org/), so it can run without a Python
install (COMSOL Multiphysics and/or OriginLab are still required separately
- see Disclaimer).

```powershell
.venv\Scripts\python install_requirements.py
.venv\Scripts\python build_exe.py
```

This runs PyInstaller against `COMSOLExtractor.spec` and writes
`dist\COMSOLExtractor.exe`. The version shown in its file properties
(and by `--version`) comes from `__version__` in `COMSOLExtractor.py` and
`version_info.txt` - keep both in sync when bumping the version.

## Output

With `--comsol`, results are written to `<model_name>_results/` next to the
`.mph` file:

- One CSV per result table and per plot group (1D/2D/3D), named after the
  COMSOL label (e.g. `Probe_Table_1.csv`); the internal COMSOL tag is
  appended only to disambiguate duplicate labels and is always recorded in
  `manifest.json`. Column headers include COMSOL's units (e.g.
  `Total displacement (m)`), and any model/description metadata or
  user-entered "Comments" are written as leading `%` comment lines.
- `manifest.json` — summary of everything extracted (tags, labels, files,
  row/column counts, and the same comments)
- `.extract_timing.json` — per-item durations/row counts from the last run,
  used to weight the next run's progress bar and remaining-time estimate
- `comsol_results.opju` (only with `--origin`) — an OriginLab project with
  one worksheet per dataset; tables and 1D plots additionally get a line
  graph

With `--origin` alone, `comsol_results.opju` is written into the
previously extracted folder you picked, built from its CSVs/`manifest.json`.

## Code layout

- `COMSOLExtractor.py` — command line and the extraction workflow
- `extraction.py` — COMSOL data extraction and the CSV format
- `origin_push.py` — OriginLab (`.opju`) integration via `originpro`
- `license_check.py` — FlexNet (FNL) license-usage query and report
- `gui.py` — PySide6 window, dialogs, and preview widgets

## How it works

- `MPh` starts a COMSOL session and loads the model.
- Result tables are read directly via COMSOL's table API: row data comes
  from `getTableData(True)`, and column names/units come from the table's
  `headers` property (a `[index, "Description (unit)"]` matrix). Older
  no-arg `getTableData()`/`getColumnHeader()`/`getDoubleValue()` calls were
  removed in COMSOL 6.4, so this is the version-independent approach.
- Plot group data (1D/2D/3D) is pulled using COMSOL's built-in "Plot" data
  export, which is more reliable across COMSOL versions than the
  feature-level data API. The exported `%`-commented header line is parsed
  to recover per-column names and units.
- Some plot types don't get a usable header line from the export at all -
  notably single-curve "Probe Table Graph" plots, which read columns
  straight from a result table. For these, column headers are instead taken
  from the source table's `headers` property, picked out via the plot
  feature's `xaxisdata` (x-axis column) and `plotcolumns` (y-axis column(s))
  properties.
- 2D/3D plot groups with an active **Deformation** sub-feature (COMSOL
  auto-tags these `defm1`, `defm2`, ...) get a second, temp-directory-only
  export with the feature switched off, recovering the undeformed reference
  geometry as extra `Undeformed <col>` columns alongside the (already
  COMSOL-scaled) deformed ones - restoring the feature's active state
  afterward either way. This is what drives the preview's deformation
  exaggeration slider; the feature's own scale factor is also recorded as a
  `%` comment (e.g. `Deformation: scale=87.4 (auto)`).
- If `--origin` is given together with `--comsol`, the in-memory data is
  pushed straight into Origin via `originpro` (COM automation) — no CSV
  round-trip needed. Column long names/units and the comments are applied to
  each worksheet: a curve column's long name carries the **full
  sweep-parameter list** from COMSOL's header (e.g.
  `gap=2 µm, external_ring=1.3 µm` for nested sweeps - dropping any
  parameter would make same-valued inner-sweep curves indistinguishable),
  and the complete original header, measured quantity included, is
  preserved in that column's Comments row. Parametric line sweeps exported
  as stitched x/y pairs are split into separate **line-only** Origin series
  (no symbols) with legend entries taken from COMSOL's own curve labels
  where available, so the graph matches COMSOL's plot instead of drawing
  connector zigzags between parameter values.
- If `--origin` is given without `--comsol`, the same `push_to_origin()` step
  runs on data read back from a `<model_name>_results/` folder's CSVs and
  `manifest.json` (via `load_dataset_csv()`/`load_datasets_from_folder()`),
  reconstructing the same `'Name (unit)'` columns as a fresh COMSOL
  extraction would produce.
- After `push_to_origin()` finishes (with or without `--comsol`), the script
  closes any Origin/OriginPro process that appeared while it ran (via
  `get_origin_pids()`/`close_new_origin_processes()`). `originpro` sometimes
  leaves its own hidden Origin instance running after `op.exit()`, which can
  otherwise keep the `.opju` file locked for further operations. An Origin
  session that was already running before the script started is left
  untouched.

## Disclaimer

This is an independent, unofficial tool and is not affiliated with,
endorsed by, or sponsored by COMSOL AB or OriginLab Corporation.

- COMSOL, COMSOL Multiphysics, and COMSOL Server are trademarks or
  registered trademarks of COMSOL AB.
- OriginLab, Origin, and OriginPro are trademarks or registered trademarks
  of OriginLab Corporation.

Using `--comsol` or `--origin` requires a valid, separately obtained
license for COMSOL Multiphysics and/or OriginLab, respectively - neither
is provided by or included with this project.
