"""
COMSOL .mph Result Extractor
=============================
Extracts all result tables and plot groups (1D/2D/3D) from a COMSOL model
and saves them as CSV files ready for OriginLab import.

Requirements:
    - COMSOL Multiphysics installed (any version 5.x / 6.x)
    - Python 3.8+
    - pip install MPh pandas numpy
    - pip install originpro and OriginLab installed (only for --origin)

Usage:
    python comsol_extractor.py                          # opens file dialog
    python comsol_extractor.py model.mph                # CLI path
    python comsol_extractor.py model.mph --output ./out # custom output dir
    python comsol_extractor.py model.mph --origin       # also build .opju in OriginLab

Output is saved to a folder named <model_name>_results/ next to the .mph file.
"""

import argparse
import sys
import re
import json
import shutil
import tempfile
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

try:
    import psutil
except ImportError:
    psutil = None

try:
    import mph
except ImportError:
    sys.exit(
        "ERROR: MPh not installed.\n"
        "  pip install MPh\n"
        "Also ensure COMSOL Multiphysics is installed and licensed."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Turn a COMSOL tag/label into a safe filename."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', '_', name)
    return name.strip('_')[:120]


def comsol_already_running() -> bool:
    """Check whether a COMSOL Desktop/server process is already running."""
    if psutil is None:
        return False
    for proc in psutil.process_iter(['name']):
        name = (proc.info.get('name') or '').lower()
        if name.startswith('comsol'):
            return True
    return False


def get_plot_type(java_plot) -> str:
    """Classify a result feature as table / 1d / 2d / 3d / other.

    MPh wraps every result node in a generic 'ResultFeatureClient' proxy, so
    the Java class name is useless for classification. Instead, ask COMSOL
    for the feature's type string (e.g. 'PlotGroup1D', 'PlotGroup3D').
    """
    try:
        ftype = str(java_plot.getType()).lower() if hasattr(java_plot, 'getType') else ''
    except Exception:
        ftype = ''

    if 'table' in ftype:
        return 'table'
    if 'plotgroup1d' in ftype:
        return '1d'
    if 'plotgroup2d' in ftype:
        return '2d'
    if 'plotgroup3d' in ftype:
        return '3d'

    # Fallback: inspect the 'plotdim' property of plot-group features.
    try:
        dim = int(java_plot.getInt('plotdim'))
        return f'{dim}d'
    except Exception:
        pass
    return 'unknown'


# ---------------------------------------------------------------------------
# Extraction routines
# ---------------------------------------------------------------------------

def extract_table(model, tag: str) -> pd.DataFrame | None:
    """
    Extract a COMSOL result table into a DataFrame.
    Tables are stored under model.result().table(tag).
    """
    try:
        tbl = model.java.result().table(tag)
        # Get column headers
        ncols = tbl.getTableData().getNumColumns()
        headers = [str(tbl.getTableData().getColumnHeader(i)) for i in range(ncols)]
        # Get data row by row
        nrows = tbl.getTableData().getNumRows()
        data = np.zeros((nrows, ncols))
        for r in range(nrows):
            for c in range(ncols):
                data[r, c] = tbl.getTableData().getDoubleValue(r, c)

        df = pd.DataFrame(data, columns=headers)
        return df

    except Exception as e:
        print(f"  [!] Could not extract table '{tag}': {e}")
        return None


# ---------------------------------------------------------------------------
# Plot extraction via COMSOL's built-in data export
# ---------------------------------------------------------------------------
#
# The COMSOL Java client API (com.comsol.clientapi.impl.ResultFeatureClient)
# does not expose the older Feature.getData() -> FeatureData API
# (getXValues/getYValues/getNumGroups/...). The reliable, version-independent
# way to pull plot data is COMSOL's built-in "Plot" data export, which writes
# a text file with '%'-commented headers followed by numeric columns.

def export_via_comsol(model, pg_tag: str, output_dir: Path) -> Path | None:
    """Use COMSOL's native 'Plot' export to dump a plot group to a text file.

    COMSOL can sometimes refuse to write directly into the model's output
    folder (locking, permissions, ...), so fall back to the system temp
    directory and move the result into place if needed.
    """
    fname = sanitize_filename(pg_tag) + '_export.txt'
    export_tag = f'exp_{pg_tag}'

    for target_dir in (output_dir, Path(tempfile.gettempdir())):
        export_path = target_dir / fname
        try:
            exp = model.java.result().export().create(export_tag, 'Plot')
            exp.set('plotgroup', pg_tag)
            exp.set('filename', str(export_path))
            exp.run()
        except Exception as e:
            print(f"  [!] COMSOL export to '{target_dir}' failed for '{pg_tag}': {e}")
            continue
        finally:
            try:
                model.java.result().export().remove(export_tag)
            except Exception:
                pass

        if export_path.exists():
            if target_dir != output_dir:
                output_dir.mkdir(parents=True, exist_ok=True)
                final_path = output_dir / fname
                shutil.move(str(export_path), str(final_path))
                return final_path
            return export_path

    return None


def extract_via_export(model, pg_tag: str, output_dir: Path) -> pd.DataFrame | None:
    """Extract a plot group's data via COMSOL's native text export into a DataFrame."""
    export_path = export_via_comsol(model, pg_tag, output_dir)
    if export_path is None:
        return None

    try:
        df = pd.read_csv(export_path, comment='%', sep=r'\s+', header=None)
    except Exception as e:
        print(f"  [!] Could not parse export for '{pg_tag}': {e}")
        return None

    if df.empty:
        return None

    ncols = len(df.columns)
    if ncols == 2:
        df.columns = ['x', 'y']
    elif ncols == 3:
        df.columns = ['x', 'y', 'z']
    elif ncols == 4:
        df.columns = ['x', 'y', 'z', 'value']
    else:
        df.columns = [f'col{i}' for i in range(ncols)]
    return df


# ---------------------------------------------------------------------------
# OriginLab integration (optional)
# ---------------------------------------------------------------------------

def push_to_origin(datasets: list, output_dir: Path, template: str = ''):
    """
    Build an OriginLab project directly from extracted DataFrames.
    Requires: pip install originpro
    Must be run with Origin installed (originpro drives it via COM).
    """
    try:
        import originpro as op
    except ImportError:
        print("\n[!] originpro not installed. Install with: pip install originpro")
        print("    Then run this from Origin's Script Window or with Origin running.")
        return

    if not datasets:
        print("No data to import into Origin.")
        return

    for entry in datasets:
        name, kind, df = entry['name'], entry['kind'], entry['df']

        # Multiple curves sharing one x-axis come back as long-format (x, y, group);
        # pivot to wide format (one y column per group) for proper multi-curve plotting.
        if 'group' in df.columns and {'x', 'y'}.issubset(df.columns):
            df = df.pivot_table(index='x', columns='group', values='y', sort=False).reset_index()
            df.columns = ['x'] + [str(c) for c in df.columns[1:]]

        wb = op.new_book('w', name)
        sheet = wb[0]
        sheet.from_df(df)
        if len(df.columns) >= 2:
            sheet.cols_axis('xy', repeat=True)

        if kind in ('table', '1d') and len(df.columns) >= 2:
            graph = op.new_graph(template=template) if template else op.new_graph()
            layer = graph[0]
            for i in range(1, len(df.columns)):
                layer.add_plot(sheet, coly=i, colx=0)
            layer.rescale()
            graph.lname = name

        print(f"  -> Imported: {name}")

    opju_path = output_dir / 'comsol_results.opju'
    op.save(str(opju_path))
    print(f"\nOrigin project saved: {opju_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def pick_file_dialog() -> Path | None:
    """Open a native file-picker dialog and return the selected .mph path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None

    root = tk.Tk()
    root.withdraw()          # hide the root window
    root.attributes('-topmost', True)  # dialog on top
    file_path = filedialog.askopenfilename(
        title='Select a COMSOL model file',
        filetypes=[
            ('COMSOL models', '*.mph'),
            ('All files', '*.*'),
        ],
    )
    root.destroy()
    return Path(file_path) if file_path else None


def main():
    parser = argparse.ArgumentParser(
        description='Extract results from a COMSOL .mph file'
    )
    parser.add_argument('model', nargs='?', default=None,
                        help='Path to .mph file (opens file dialog if omitted)')
    parser.add_argument('--output', '-o', default=None,
                        help='Output directory (default: folder next to .mph named <model>_results/)')
    parser.add_argument('--origin', action='store_true',
                        help='Also push CSVs into OriginLab (requires originpro)')
    parser.add_argument('--origin-template', default='',
                        help='Origin graph template (.otpu) to use')
    args = parser.parse_args()

    # -- Resolve model path: CLI arg or file dialog --
    if args.model:
        model_path = Path(args.model).resolve()
    else:
        print("No file specified — opening file dialog...")
        model_path = pick_file_dialog()
        if model_path is None:
            sys.exit("No file selected. Exiting.")
        model_path = model_path.resolve()

    if not model_path.exists():
        sys.exit(f"File not found: {model_path}")

    # -- Output folder: same location as .mph, named <stem>_results --
    if args.output:
        output_dir = Path(args.output).resolve()
    else:
        output_dir = model_path.parent / f"{model_path.stem}_results"

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output folder: {output_dir}")

    # -- Start COMSOL server and load model --
    if comsol_already_running():
        print("WARNING: A COMSOL process is already running. Starting this "
              "stand-alone session will launch an additional COMSOL engine "
              "instance, using extra memory and a separate license seat.")
    print(f"Starting COMSOL server...")
    client = mph.start()
    print(f"Loading model: {model_path.name}")
    model = client.load(str(model_path))
    print(f"Model loaded.\n")

    manifest = {
        'model': model_path.name,
        'extracted_at': datetime.now().isoformat(),
        'tables': [],
        '1d_plots': [],
        '2d_plots': [],
        '3d_plots': [],
        'other': [],
    }
    datasets = []  # collected for direct OriginLab export: {'name', 'kind', 'df'}

    # -- Discover result nodes --
    java_result = model.java.result()

    # ---- Tables ----
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        tbl_tags_obj = java_result.table().tags()
        tbl_tags = [str(t) for t in tbl_tags_obj]
    except Exception as e:
        print(f"  [!] Could not list result tables: {e}")
        tbl_tags = []

    if tbl_tags:
        print(f"Found {len(tbl_tags)} result table(s):")
    for tag in tbl_tags:
        try:
            label = str(java_result.table(tag).label())
        except Exception:
            label = tag
        print(f"  - {tag} ({label})")

        df = extract_table(model, tag)
        if df is not None and not df.empty:
            name = sanitize_filename(f"table_{tag}_{label}")
            fname = name + '.csv'
            df.to_csv(output_dir / fname, index=False)
            manifest['tables'].append({'tag': tag, 'label': label, 'file': fname,
                                       'rows': len(df), 'cols': list(df.columns)})
            datasets.append({'name': name, 'kind': 'table', 'df': df})
            print(f"    -> Saved {fname}  ({len(df)} rows x {len(df.columns)} cols)")

    # ---- Plot groups ----
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        pg_tags_obj = java_result.tags()
        pg_tags = [str(t) for t in pg_tags_obj]
    except Exception as e:
        print(f"  [!] Could not list plot groups: {e}")
        pg_tags = []

    for tag in pg_tags:
        try:
            pg = model.java.result(tag)
            label = str(pg.label()) if hasattr(pg, 'label') else tag
            class_name = str(pg.getClass().getSimpleName())
        except Exception as e:
            print(f"  [!] Could not access plot group '{tag}': {e}")
            continue

        ptype = get_plot_type(pg)
        print(f"\n  [{ptype.upper():>7}] {tag} ({label})  [{class_name}]")

        df = extract_via_export(model, tag, output_dir)
        if df is not None and not df.empty:
            name = sanitize_filename(f"plot{ptype}_{tag}_{label}")
            fname = name + '.csv'
            df.to_csv(output_dir / fname, index=False)
            entry = {'tag': tag, 'label': label, 'file': fname,
                     'rows': len(df), 'cols': list(df.columns)}
            if ptype in ('1d', '2d', '3d'):
                manifest[f'{ptype}_plots'].append(entry)
            else:
                entry['type'] = class_name
                manifest['other'].append(entry)
            datasets.append({'name': name, 'kind': ptype, 'df': df})
            print(f"    -> Saved {fname}  ({len(df)} rows x {len(df.columns)} cols)")
        else:
            entry = {'tag': tag, 'label': label}
            if ptype not in ('1d', '2d', '3d'):
                entry['type'] = class_name
                manifest['other'].append(entry)
            print(f"    [!] No data extracted for '{tag}'")

    # -- Write manifest --
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / 'manifest.json'
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest written: {manifest_path}")

    # -- Summary --
    n_tables = len(manifest['tables'])
    n_1d = len(manifest['1d_plots'])
    n_2d = len(manifest['2d_plots'])
    n_3d = len(manifest['3d_plots'])
    n_other = len(manifest['other'])
    print(f"\n{'='*50}")
    print(f"Extraction complete!")
    print(f"  Tables:    {n_tables}")
    print(f"  1D plots:  {n_1d}")
    print(f"  2D plots:  {n_2d}")
    print(f"  3D plots:  {n_3d}")
    print(f"  Other:     {n_other}")
    print(f"  Output:    {output_dir}")
    print(f"{'='*50}")

    # -- Optional: push to OriginLab --
    if args.origin:
        print("\nPushing results to OriginLab...")
        push_to_origin(datasets, output_dir, template=args.origin_template)

    # -- Clean up --
    client.clear()
    print("Done.")


if __name__ == '__main__':
    main()