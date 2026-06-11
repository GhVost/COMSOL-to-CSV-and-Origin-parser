"""
COMSOL .mph Result Extractor
=============================
Extracts all result tables, 1D plots, and 2D plots from a COMSOL model
and saves them as CSV files ready for OriginLab import.

Requirements:
    - COMSOL Multiphysics installed (any version 5.x / 6.x)
    - Python 3.8+
    - pip install MPh pandas numpy

Usage:
    python comsol_extractor.py                          # opens file dialog
    python comsol_extractor.py model.mph                # CLI path
    python comsol_extractor.py model.mph --output ./out # custom output dir
    python comsol_extractor.py model.mph --origin       # also push to OriginLab

Output is saved to a folder named <model_name>_results/ next to the .mph file.
"""

import argparse
import sys
import re
import json
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


def discover_nodes(model, root: str) -> list[str]:
    """Return all child tags under a results node (e.g. 'results')."""
    try:
        tags = model.java.result().tags()
        return [str(t) for t in tags]
    except Exception:
        return []


def get_plot_type(java_plot) -> str:
    """Heuristic to classify a result feature as table / 1d / 2d / other."""
    type_str = str(type(java_plot)).lower()
    class_name = java_plot.getClass().getSimpleName() if hasattr(java_plot, 'getClass') else ''
    class_lower = class_name.lower()

    if 'table' in class_lower or 'table' in type_str:
        return 'table'
    if 'plot1d' in class_lower or 'pg1d' in type_str or '1d' in class_lower:
        return '1d'
    if 'plot2d' in class_lower or 'pg2d' in type_str or '2d' in class_lower:
        return '2d'
    if 'plotgroup' in class_lower:
        # Inspect dimension hint from node properties
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


def extract_1d_plot(model, pg_tag: str) -> dict[str, pd.DataFrame]:
    """
    Extract all line-graphs inside a 1D plot group.

    For each child feature (line graph, point graph, global, etc.)
    we call getData() or use model.evaluate() to pull x/y arrays.

    Returns dict  { child_tag: DataFrame }.
    """
    results = {}
    try:
        pg = model.java.result(pg_tag)
        child_tags_obj = pg.feature().tags()
        child_tags = [str(t) for t in child_tags_obj]

        for ctag in child_tags:
            try:
                feat = pg.feature(ctag)
                # Try to use getData — works on evaluated features
                feat_data = feat.getData()
                if feat_data is None:
                    pg.run()  # force evaluation
                    feat_data = feat.getData()

                if feat_data is not None:
                    nsets = feat_data.getNumGroups()
                    frames = []
                    for g in range(nsets):
                        x = np.array(feat_data.getXValues(g))
                        y = np.array(feat_data.getYValues(g))
                        group_label = str(feat_data.getLegend(g)) if nsets > 1 else ''
                        df = pd.DataFrame({'x': x, 'y': y})
                        if group_label:
                            df['group'] = group_label
                        frames.append(df)

                    if frames:
                        results[ctag] = pd.concat(frames, ignore_index=True)

            except Exception as e:
                print(f"    [!] Skipping child '{ctag}' in '{pg_tag}': {e}")

    except Exception as e:
        print(f"  [!] Could not process 1D plot group '{pg_tag}': {e}")

    return results


def extract_2d_plot(model, pg_tag: str, output_dir: Path) -> dict[str, pd.DataFrame]:
    """
    Extract data from a 2D plot group.

    2D surface / contour data is trickier. Two strategies:
      1) Export the plot as a text table via COMSOL's built-in export.
      2) Use getData() for contour / surface children.

    Returns dict { child_tag: DataFrame }.
    """
    results = {}
    try:
        pg = model.java.result(pg_tag)
        child_tags_obj = pg.feature().tags()
        child_tags = [str(t) for t in child_tags_obj]

        for ctag in child_tags:
            try:
                feat = pg.feature(ctag)
                feat_data = feat.getData()
                if feat_data is None:
                    pg.run()
                    feat_data = feat.getData()

                if feat_data is not None:
                    nsets = feat_data.getNumGroups()
                    frames = []
                    for g in range(nsets):
                        # 2D data typically provides (x, y, z) triplets
                        try:
                            x = np.array(feat_data.getXValues(g))
                            y = np.array(feat_data.getYValues(g))
                            z = np.array(feat_data.getZValues(g))
                            df = pd.DataFrame({'x': x, 'y': y, 'z': z})
                        except Exception:
                            # Fall back to x/y only
                            x = np.array(feat_data.getXValues(g))
                            y = np.array(feat_data.getYValues(g))
                            df = pd.DataFrame({'x': x, 'y': y})

                        group_label = str(feat_data.getLegend(g)) if nsets > 1 else ''
                        if group_label:
                            df['group'] = group_label
                        frames.append(df)

                    if frames:
                        results[ctag] = pd.concat(frames, ignore_index=True)

            except Exception as e:
                print(f"    [!] Skipping child '{ctag}' in '{pg_tag}': {e}")

        # Fallback: use COMSOL's built-in export if we got nothing
        if not results:
            try:
                export_path = str(output_dir / f"{sanitize_filename(pg_tag)}_export.txt")
                export = model.java.result().export().create(
                    f"tmp_export_{pg_tag}", "Plot"
                )
                export.set("plotgroup", pg_tag)
                export.set("filename", export_path)
                export.run()
                model.java.result().export().remove(f"tmp_export_{pg_tag}")

                df = pd.read_csv(export_path, comment='%', delim_whitespace=True,
                                 header=None)
                results[pg_tag + '_exported'] = df
            except Exception:
                pass

    except Exception as e:
        print(f"  [!] Could not process 2D plot group '{pg_tag}': {e}")

    return results


# ---------------------------------------------------------------------------
# Alternative: export ALL via COMSOL's built-in data export
# ---------------------------------------------------------------------------

def export_via_comsol(model, pg_tag: str, output_dir: Path) -> Path | None:
    """
    Use COMSOL's native export to dump a plot group to a text file.
    Works as a universal fallback for any plot type.
    """
    try:
        fname = sanitize_filename(pg_tag) + '.txt'
        export_path = output_dir / fname

        # Create a temporary data export node
        export_tag = f'exp_{pg_tag}'
        exp = model.java.result().export().create(export_tag, 'Data')
        exp.set('plotgroup', pg_tag)
        exp.set('filename', str(export_path))
        exp.set('header', 'on')
        exp.run()
        model.java.result().export().remove(export_tag)

        if export_path.exists():
            return export_path

    except Exception as e:
        print(f"  [!] COMSOL export fallback failed for '{pg_tag}': {e}")
    return None


# ---------------------------------------------------------------------------
# OriginLab integration (optional)
# ---------------------------------------------------------------------------

def push_to_origin(csv_dir: Path, template: str = ''):
    """
    Import all extracted CSVs into OriginLab.
    Requires: pip install originpro
    Must be run with Origin running or from Origin's embedded Python.
    """
    try:
        import originpro as op
    except ImportError:
        print("\n[!] originpro not installed. Install with: pip install originpro")
        print("    Then run this from Origin's Script Window or with Origin running.")
        return

    csv_files = sorted(csv_dir.glob('*.csv'))
    if not csv_files:
        print("No CSV files found to import.")
        return

    for csv_path in csv_files:
        df = pd.read_csv(csv_path)
        wb = op.new_book('w', csv_path.stem)
        sheet = wb[0]

        for i, col in enumerate(df.columns):
            sheet.from_list(i, df[col].tolist(), col)

        # Set column designations
        if len(df.columns) >= 1:
            sheet.set_label(0, 'X')
        for i in range(1, len(df.columns)):
            sheet.set_label(i, 'Y')

        # Create a graph
        if template:
            graph = op.new_graph(template=template)
        else:
            graph = op.new_graph()

        layer = graph[0]
        for i in range(1, len(df.columns)):
            layer.add_plot(sheet, coly=i, colx=0)
        layer.rescale()
        graph.set_str('LongName', csv_path.stem)
        print(f"  -> Imported: {csv_path.stem}")

    opju_path = csv_dir / 'comsol_results.opju'
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
    parser.add_argument('--fallback-export', action='store_true',
                        help='Use COMSOL native export as fallback for all plots')
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
        'other': [],
    }

    # -- Discover result nodes --
    java_result = model.java.result()

    # ---- Tables ----
    try:
        tbl_tags_obj = java_result.table().tags()
        tbl_tags = [str(t) for t in tbl_tags_obj]
    except Exception:
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
            fname = sanitize_filename(f"table_{tag}_{label}") + '.csv'
            df.to_csv(output_dir / fname, index=False)
            manifest['tables'].append({'tag': tag, 'label': label, 'file': fname,
                                       'rows': len(df), 'cols': list(df.columns)})
            print(f"    -> Saved {fname}  ({len(df)} rows x {len(df.columns)} cols)")

    # ---- Plot groups ----
    try:
        pg_tags_obj = java_result.tags()
        pg_tags = [str(t) for t in pg_tags_obj]
    except Exception:
        pg_tags = []

    for tag in pg_tags:
        try:
            pg = java_result(tag)
            label = str(pg.label()) if hasattr(pg, 'label') else tag
            class_name = pg.getClass().getSimpleName()
        except Exception:
            continue

        ptype = get_plot_type(pg)
        print(f"\n  [{ptype.upper():>7}] {tag} ({label})  [{class_name}]")

        if ptype == '1d':
            data = extract_1d_plot(model, tag)
            for child_tag, df in data.items():
                fname = sanitize_filename(f"plot1d_{tag}_{child_tag}") + '.csv'
                df.to_csv(output_dir / fname, index=False)
                manifest['1d_plots'].append({
                    'tag': tag, 'child': child_tag, 'label': label,
                    'file': fname, 'rows': len(df), 'cols': list(df.columns)
                })
                print(f"    -> Saved {fname}  ({len(df)} rows)")

            # Fallback if nothing extracted
            if not data and args.fallback_export:
                p = export_via_comsol(model, tag, output_dir)
                if p:
                    print(f"    -> Fallback export: {p.name}")

        elif ptype == '2d':
            data = extract_2d_plot(model, tag, output_dir)
            for child_tag, df in data.items():
                fname = sanitize_filename(f"plot2d_{tag}_{child_tag}") + '.csv'
                df.to_csv(output_dir / fname, index=False)
                manifest['2d_plots'].append({
                    'tag': tag, 'child': child_tag, 'label': label,
                    'file': fname, 'rows': len(df), 'cols': list(df.columns)
                })
                print(f"    -> Saved {fname}  ({len(df)} rows)")

            if not data and args.fallback_export:
                p = export_via_comsol(model, tag, output_dir)
                if p:
                    print(f"    -> Fallback export: {p.name}")

        else:
            manifest['other'].append({'tag': tag, 'label': label, 'type': class_name})
            if args.fallback_export:
                p = export_via_comsol(model, tag, output_dir)
                if p:
                    print(f"    -> Fallback export: {p.name}")

    # -- Write manifest --
    manifest_path = output_dir / 'manifest.json'
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest written: {manifest_path}")

    # -- Summary --
    n_tables = len(manifest['tables'])
    n_1d = len(manifest['1d_plots'])
    n_2d = len(manifest['2d_plots'])
    n_other = len(manifest['other'])
    print(f"\n{'='*50}")
    print(f"Extraction complete!")
    print(f"  Tables:    {n_tables}")
    print(f"  1D plots:  {n_1d}")
    print(f"  2D plots:  {n_2d}")
    print(f"  Other:     {n_other}")
    print(f"  Output:    {output_dir}")
    print(f"{'='*50}")

    # -- Optional: push to OriginLab --
    if args.origin:
        print("\nPushing results to OriginLab...")
        push_to_origin(output_dir, template=args.origin_template)

    # -- Clean up --
    client.clear()
    print("Done.")


if __name__ == '__main__':
    main()