# COMSOL-data-parser

Extract result tables and plot groups (1D/2D/3D) from a COMSOL `.mph` model
and save them as CSV files, optionally importing everything directly into an
OriginLab project (`.opju`).

## Requirements

- COMSOL Multiphysics (5.x or 6.x), installed and licensed
- Python 3.10+
- Python packages: `MPh`, `pandas`, `numpy`, `psutil`
- Optional, for `--origin`: OriginLab installed + `originpro`

Install everything into a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\pip install MPh pandas numpy psutil originpro
```

## Usage

```powershell
.venv\Scripts\python.exe ComsolExtractor.py --origin
```

This opens a file picker to choose the `.mph` model, extracts everything,
and builds an OriginLab project (`.opju`).

Other options:

- `ComsolExtractor.py model.mph` — extract a specific model (skips the file
  dialog), without `--origin` if only CSVs are needed
- `--output ./out` — custom output directory
- `--origin-template my_template.otpu` — Origin graph template for line plots

## Output

Results are written to `<model_name>_results/` next to the `.mph` file:

- One CSV per result table and per plot group (1D/2D/3D), named after the
  COMSOL tag and label. Column headers include COMSOL's units (e.g.
  `Total displacement (m)`), and any model/description metadata or
  user-entered "Comments" are written as leading `%` comment lines.
- `manifest.json` — summary of everything extracted (tags, labels, files,
  row/column counts, and the same comments)
- `comsol_results.opju` (only with `--origin`) — an OriginLab project with
  one worksheet per dataset; tables and 1D plots additionally get a line
  graph

## How it works

- `MPh` starts a COMSOL session and loads the model.
- Result tables are read directly via COMSOL's table API, which already
  includes column headers with units.
- Plot group data (1D/2D/3D) is pulled using COMSOL's built-in "Plot" data
  export, which is more reliable across COMSOL versions than the
  feature-level data API. The exported `%`-commented header line is parsed
  to recover per-column names and units.
- If `--origin` is given, the in-memory data is pushed straight into Origin
  via `originpro` (COM automation) — no CSV round-trip needed. Column long
  names/units and the comments are applied to each worksheet.

## Notes

- Before starting, the script checks whether COMSOL or (with `--origin`)
  OriginLab are already running and prints a prompt if action may be needed:
  - If a COMSOL process is already running, starting this script's own
    session will use an additional engine instance and license seat —
    close the existing session first if you want to avoid that.
  - With `--origin`, if OriginLab isn't running yet, start it so `originpro`
    can connect to it.
  - Each prompt waits for Enter to continue, or Ctrl+C to abort.
- `--origin` requires OriginLab to be installed on the same machine.
