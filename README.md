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
# Open a file picker dialog to choose the .mph file
.venv\Scripts\python.exe ComsolExtractor.py

# Extract a specific model
.venv\Scripts\python.exe ComsolExtractor.py model.mph

# Choose a custom output directory
.venv\Scripts\python.exe ComsolExtractor.py model.mph --output ./out

# Also build an OriginLab project (.opju) from the extracted data
.venv\Scripts\python.exe ComsolExtractor.py model.mph --origin --origin-template my_template.otpu
```

## Output

Results are written to `<model_name>_results/` next to the `.mph` file:

- One CSV per result table and per plot group (1D/2D/3D), named after the
  COMSOL tag and label
- `manifest.json` — summary of everything extracted (tags, labels, files,
  row/column counts)
- `comsol_results.opju` (only with `--origin`) — an OriginLab project with
  one worksheet per dataset; tables and 1D plots additionally get a line
  graph

## How it works

- `MPh` starts a COMSOL session and loads the model.
- Result tables are read directly via COMSOL's table API.
- Plot group data (1D/2D/3D) is pulled using COMSOL's built-in "Plot" data
  export, which is more reliable across COMSOL versions than the
  feature-level data API.
- If `--origin` is given, the in-memory data is pushed straight into Origin
  via `originpro` (COM automation) — no CSV round-trip needed.

## Notes

- If a COMSOL process is already running, the script warns that starting its
  own session will use an additional engine instance and license seat.
- `--origin` requires OriginLab to be installed and running on the same
  machine.
