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
.venv\Scripts\python.exe ComsolExtractor.py
```

With no `--comsol`/`--origin` flags, a single combined dialog appears to
pick which steps to run - **Extract from COMSOL** and/or **Import into
OriginLab** - and to check their availability interactively:

- **OriginLab**'s LED shows green/grey depending on whether an
  Origin/OriginPro process is currently running.
- **COMSOL**'s LED instead reflects a real availability check: as soon as
  the dialog renders showing "checking server availability...", it tries to
  start a COMSOL server (`mph.start()`), then updates to green/"server
  available" or red/"unavailable: ...". This briefly blocks the window (the
  "OK" button is disabled until it finishes), since starting COMSOL's JVM
  must happen on the main thread.
- If that check succeeds and **Extract from COMSOL** stays checked, the
  already-started server is reused for extraction - no second server is
  launched. If COMSOL is unchecked (or the dialog is cancelled), that test
  server is shut down again.
- Warnings that used to be separate "press Enter to continue" prompts (e.g.
  "OriginLab isn't running yet") are now shown inline in this same window.

Pick whichever combination matches your situation:

- **COMSOL only** — parse the `.mph` model and write CSVs/`manifest.json`,
  without touching OriginLab (e.g. OriginLab isn't installed here, or you
  just want the CSVs).
- **OriginLab only** — skip COMSOL entirely (useful if COMSOL isn't
  installed, or its license is busy elsewhere) and instead pick an existing
  `<model_name>_results/` folder to pack into a new OriginLab project.
- **Both** — parse the `.mph` model, write CSVs/`manifest.json`, *and* push
  the same in-memory data straight into OriginLab in one go.

The same choices are available non-interactively via `--comsol` and
`--origin` (either or both); when at least one is given on the command line,
the dialog is skipped.

```powershell
.venv\Scripts\python.exe ComsolExtractor.py --comsol --origin
```

This opens a file picker to choose the `.mph` model, extracts everything,
and builds an OriginLab project (`.opju`).

When `--comsol`/`--origin` are given directly, this skips the combined
dialog above, but the script still checks beforehand whether COMSOL/OriginLab
are already running and prompts if action may be needed:

- If `--comsol` is selected and a COMSOL process is already running,
  starting this script's own session will use an additional engine instance
  and license seat — close the existing session first if you want to avoid
  that.
- If `--origin` is selected and OriginLab isn't running yet, start it so
  `originpro` can connect to it.
- Each prompt waits for Enter to continue, or Ctrl+C to abort.

Other options:

- `ComsolExtractor.py model.mph --comsol` — extract a specific model (skips
  the file dialog)
- `--output ./out` — custom output directory (COMSOL mode)
- `--origin-template my_template.otpu` — Origin graph template for line plots

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
