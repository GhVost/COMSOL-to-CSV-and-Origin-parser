# COMSOL to .csv and/or Origin

Extract result tables and plot groups (1D/2D/3D) from a COMSOL `.mph` model
and save them as CSV files, optionally importing everything directly into an
OriginLab project (`.opju`) - or import a previously extracted folder into
OriginLab without COMSOL at all.

## Requirements

- COMSOL Multiphysics (5.x or 6.x), installed and licensed — only needed for
  `--comsol`
- Python 3.10+
- Python packages: `MPh`, `pandas`, `numpy`, `psutil`
- Optional, for `--origin`: OriginLab installed + `originpro`

Install everything into a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\python install_requirements.py
```

`install_requirements.py` installs everything listed in `requirements.txt`
(`MPh`, `pandas`, `numpy`, `psutil`, and the optional `originpro`). It can
also be run directly with `pip install -r requirements.txt` if preferred.

## Usage

```powershell
.venv\Scripts\python.exe COMSOLExtractor.py
```

With no `--comsol`/`--origin` flags, the script opens a file picker for the
`.mph` model, starts a COMSOL server and loads it, then shows a single
combined window with:

- **Status** - a green LED confirming the model loaded, plus an
  **OriginLab** row showing whether Origin/OriginPro is currently running
  (LED + "running"/"not running"). A **Start OriginPro** button launches
  OriginPro (via `originpro`/COM) and re-checks its status afterwards,
  updating the LED once it comes up. A checkbox ("Import extracted results
  into OriginLab") controls whether the extraction is pushed into Origin
  afterwards - ticked automatically once Origin is detected as running.
  If a COMSOL process was already running before this session started, a
  warning is shown here too (an extra engine instance/license seat will be
  used).
- **Items to extract** - the same checklist of tables/plot groups as
  before, grouped by type (Tables / 1D / 2D / 3D Plots), all checked by
  default.

Click **Extract** to write the selected items to `<model_name>_results/`
and, if the OriginLab checkbox is ticked, build `comsol_results.opju` in the
same folder - which is then opened in File Explorer.

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

This opens a file picker to choose the `.mph` model, then the combined
status/items window with the OriginLab checkbox pre-ticked; clicking
**Extract** writes the CSVs and builds an OriginLab project (`.opju`).

If `--origin` is given without `--comsol`, OriginLab not running yet is the
only thing checked beforehand with a console prompt (waits for Enter to
continue, or Ctrl+C to abort) - it's not part of the combined window since
that path skips COMSOL/the model entirely.

Other options:

- `COMSOLExtractor.py model.mph --comsol` — extract a specific model (skips
  the file dialog)
- `--output ./out` — custom output directory (COMSOL mode)
- `--origin-template my_template.otpu` — Origin graph template for line plots
- `--version` — print the version and exit

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
  COMSOL tag and label. Column headers include COMSOL's units (e.g.
  `Total displacement (m)`), and any model/description metadata or
  user-entered "Comments" are written as leading `%` comment lines.
- `manifest.json` — summary of everything extracted (tags, labels, files,
  row/column counts, and the same comments)
- `comsol_results.opju` (only with `--origin`) — an OriginLab project with
  one worksheet per dataset; tables and 1D plots additionally get a line
  graph

With `--origin` alone, `comsol_results.opju` is written into the
previously extracted folder you picked, built from its CSVs/`manifest.json`.

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
- If `--origin` is given together with `--comsol`, the in-memory data is
  pushed straight into Origin via `originpro` (COM automation) — no CSV
  round-trip needed. Column long names/units and the comments are applied to
  each worksheet.
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
