"""
COMSOL .mph Result Extractor
=============================
Extracts result tables and plot groups (1D/2D/3D) from a COMSOL model and
saves them as CSV files for OriginLab import, optionally building an .opju
project directly from them.

Each CSV starts with leading '%' comment lines (model metadata / user
comments, also recorded in manifest.json), then two header rows - column
names and units, split from COMSOL's 'Name (unit)'/'Name [unit]' headers -
then the data. Units not present in the header text (separate plot-feature
properties, or spatial-coordinate columns using the geometry's length unit)
are filled in, and mojibake unit symbols (e.g. 'Âµm') are repaired. With
--origin, those names/units/comments are applied to the Origin worksheet.

Requirements:
    - COMSOL Multiphysics installed (5.x / 6.x)
    - Python 3.10+ (uses 'X | Y' union type hints evaluated at runtime)
    - pip install MPh pandas numpy
    - pip install originpro and OriginLab installed (only for --origin)

Usage:
    python COMSOLExtractor.py --origin

Opens a file dialog to pick the .mph model, then a combined window listing
every table/plot group to extract and OriginLab's status. A model path,
--output <dir> and --origin-template <file> can also be given; see --help.

Output is saved to a folder named <model_name>_results/ next to the .mph file.
"""

__version__ = '1.4.0'

import argparse
import os
import sys
import re
import csv
import json
import shutil
import tempfile
import traceback
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


def split_label_unit(label: str) -> tuple[str, str]:
    """Split a column header into (name, unit).

    Most headers end in '(unit)'/'[unit]' (e.g. 'Displacement magnitude
    (um)'). Multi-curve table-graph exports instead append ', <curve label>'
    after the unit - if no unit is found at the end, pull the first
    '(unit)'/'[unit]' out of the middle of the string instead.
    """
    label = str(label).strip()
    m = re.match(r'^(.*?)\s*[(\[]([^()\[\]]*)[)\]]\s*$', label)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    m = re.search(r'[(\[]([^()\[\]]*)[)\]]', label)
    if not m:
        return label, ''
    name = label[:m.start()] + label[m.end():]
    name = re.sub(r'\s{2,}', ' ', name)
    name = re.sub(r'\s*,\s*', ', ', name)
    return name.strip(' ,'), m.group(1).strip()


def repair_mojibake(text: str) -> str:
    """Fix text COMSOL's export wrote as UTF-8 bytes re-interpreted as
    Windows-1252 (e.g. the unit 'µm' coming out as 'Âµm')."""
    try:
        return text.encode('cp1252').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def write_csv_with_comments(df: pd.DataFrame, path: Path, comments: list[str] | None = None):
    """Write a DataFrame to CSV with COMSOL metadata as leading '%' comment lines,
    followed by a row of column names and a row of units (split from the
    'Name (unit)' headers), then the data."""
    names, units = zip(*(split_label_unit(col) for col in df.columns))
    with open(path, 'w', encoding='utf-8', newline='') as f:
        for line in comments or []:
            f.write(f"% {line}\n")
        writer = csv.writer(f)
        writer.writerow(names)
        writer.writerow(units)
        for row in df.itertuples(index=False, name=None):
            writer.writerow(row)


def load_dataset_csv(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Read a CSV written by write_csv_with_comments() back into a
    (DataFrame, comments) pair, recombining the two header rows into
    'Name (unit)' columns - used to re-import a '<model>_results' folder into
    OriginLab without COMSOL running again.
    """
    with open(path, 'r', encoding='utf-8', newline='') as f:
        lines = f.readlines()

    comments = []
    i = 0
    while i < len(lines) and lines[i].startswith('%'):
        comments.append(lines[i][1:].strip())
        i += 1

    names = next(csv.reader([lines[i]]))
    units = next(csv.reader([lines[i + 1]]))
    headers = [f"{name} ({unit})" if unit else name for name, unit in zip(names, units)]

    df = pd.read_csv(path, skiprows=i + 2, header=None, names=headers)
    return df, comments


def comsol_already_running() -> bool:
    """Check whether a COMSOL Desktop/server process is already running."""
    if psutil is None:
        return False
    for proc in psutil.process_iter(['name']):
        name = (proc.info.get('name') or '').lower()
        if name.startswith('comsol'):
            return True
    return False


def origin_already_running() -> bool:
    """Check whether OriginLab (Origin/OriginPro) is already running."""
    if psutil is None:
        return False
    for proc in psutil.process_iter(['name']):
        name = (proc.info.get('name') or '').lower()
        if name.startswith('origin'):
            return True
    return False


def get_origin_pids() -> set[int]:
    """Return the PIDs of all currently running Origin/OriginPro processes."""
    if psutil is None:
        return set()
    return {
        proc.info['pid']
        for proc in psutil.process_iter(['pid', 'name'])
        if (proc.info.get('name') or '').lower().startswith('origin')
    }


def close_new_origin_processes(pids_before: set[int]):
    """Terminate any Origin/OriginPro process that wasn't running before
    push_to_origin() was called - originpro's hidden Origin instance can
    otherwise outlive op.exit() and keep the .opju file locked. An Origin
    session the user already had open (in pids_before) is left untouched.
    """
    if psutil is None:
        return
    for proc in psutil.process_iter(['pid', 'name']):
        name = (proc.info.get('name') or '').lower()
        if name.startswith('origin') and proc.info['pid'] not in pids_before:
            try:
                proc.terminate()
            except Exception:
                pass


def start_originpro() -> tuple[bool, str]:
    """Launch OriginPro (or just show it, if a hidden instance is already
    attached) via originpro's COM connection - op.set_show(True) both
    triggers the lazy connection and makes the window visible.

    Returns (success, error_message) - error_message is '' on success.
    """
    try:
        import originpro as op
    except ImportError:
        return False, "originpro not installed"
    try:
        op.set_show(True)
        return True, ''
    except Exception as e:
        return False, str(e)


def confirm_or_exit(message: str):
    """Print a message and wait for the user to press Enter (or Ctrl+C to abort)."""
    print(message)
    try:
        input("Press Enter to continue, or Ctrl+C to abort... ")
    except KeyboardInterrupt:
        sys.exit("\nAborted by user.")


def pause_if_frozen():
    """When running as a bundled .exe (no console window of its own), wait
    for Enter before the window closes so any final messages - including
    OriginLab push errors - stay visible."""
    if getattr(sys, 'frozen', False):
        input("\nPress Enter to exit... ")


def get_plot_type(java_plot) -> str:
    """Classify a result feature as table / 1d / 2d / 3d / other.

    MPh wraps every result node in a generic proxy, so the Java class name is
    useless for classification - ask COMSOL for the feature's type string
    instead (e.g. 'PlotGroup1D', 'PlotGroup3D').
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

    # Fallback for plot-group subtypes whose getType() doesn't match above.
    try:
        dim = int(java_plot.getInt('plotdim'))
        return f'{dim}d'
    except Exception:
        pass
    return 'unknown'


# ---------------------------------------------------------------------------
# Extraction routines
# ---------------------------------------------------------------------------

def extract_table(model, tag: str) -> tuple[pd.DataFrame, list[str]] | None:
    """Extract a COMSOL result table into a DataFrame, plus any user comments.

    Column headers (with units, e.g. 'freq (GHz)') come from the table's
    'headers' property - a [index, description] matrix - while the data
    comes from getTableData(True) as strings (COMSOL 6.4 dropped the no-arg
    getTableData()/getColumnHeader()/getDoubleValue() API).
    """
    try:
        tbl = model.java.result().table(tag)
        headers = [str(row[1]) for row in tbl.getStringMatrix('headers')]

        # Each cell is a Java String (e.g. "3.4065" or "inf"/"NaN"); convert
        # via str() first - Python's float() accepts "inf"/"NaN" natively.
        rows = tbl.getTableData(True)
        data = np.array([[float(str(cell)) for cell in row] for row in rows], dtype=float)

        df = pd.DataFrame(data, columns=headers)

        comments = []
        try:
            note = str(tbl.comments())
            if note:
                comments.append(note)
        except Exception:
            pass

        return df, comments

    except Exception as e:
        print(f"  [!] Could not extract table '{tag}': {e}")
        return None


# ---------------------------------------------------------------------------
# Plot extraction via COMSOL's built-in "Plot" data export - more reliable
# across COMSOL versions than the feature-level data API.
# ---------------------------------------------------------------------------

def export_via_comsol(model, pg_tag: str, output_dir: Path) -> Path | None:
    """Use COMSOL's native 'Plot' export to dump a plot group to a text file,
    falling back to the system temp directory if COMSOL refuses to write
    into output_dir (locking, permissions, ...)."""
    fname = sanitize_filename(pg_tag) + '_export.txt'
    # Temporary export-node tag; created and removed within this function so
    # repeated calls don't accumulate leftover export nodes in the model.
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
                # Exported to the temp dir as a fallback - move it alongside
                # the CSV and manifest in the real output directory.
                output_dir.mkdir(parents=True, exist_ok=True)
                final_path = output_dir / fname
                shutil.move(str(export_path), str(final_path))
                return final_path
            return export_path

    return None


def get_feature_units(pg) -> dict[str, str]:
    """Map each axis description/expression to its COMSOL 'unit' property.

    A plot feature (e.g. Line Graph, Surface, or a nested Deformation
    sub-feature) can carry a unit as a property separate from its
    description/expression - COMSOL's text export omits such units from the
    column header entirely. Units pair with their label by name prefix, e.g.
    'unit'/'descr'/'expr' or 'xdataunit'/'xdatadescr'/'xdataexpr'. A 'unit'
    can also be a string array (e.g. a Deformation sub-feature's per-axis
    units), paired positionally with a same-length 'expr'/'descr' array.
    Nested features are walked recursively.
    """
    units = {}

    def visit(feat):
        try:
            names = {str(n) for n in feat.properties()}
        except Exception:
            names = set()

        for prop in names:
            if not prop.lower().endswith('unit'):
                continue
            try:
                unit_type = str(feat.getValueType(prop))
            except Exception:
                continue

            prefix = prop[:-len('unit')]
            for label_prop in (prefix + 'descr', prefix + 'expr'):
                if label_prop not in names:
                    continue
                try:
                    # Unit and label must share a value type to pair them up.
                    if str(feat.getValueType(label_prop)) != unit_type:
                        continue
                    if unit_type == 'String':
                        unit_val = str(feat.getString(prop)).strip()
                        label_val = str(feat.getString(label_prop)).strip()
                        if unit_val and label_val:
                            units[label_val] = unit_val
                            break
                    elif unit_type == 'StringArray':
                        unit_vals = [str(v).strip() for v in feat.getStringArray(prop)]
                        label_vals = [str(v).strip() for v in feat.getStringArray(label_prop)]
                        if len(unit_vals) == len(label_vals):
                            for label_val, unit_val in zip(label_vals, unit_vals):
                                if label_val and unit_val:
                                    units[label_val] = unit_val
                            break
                except Exception:
                    continue

        # Nested sub-features (e.g. a Surface plot's Deformation/Height
        # Expression) can carry their own unit properties too.
        try:
            for ctag in feat.feature().tags():
                visit(feat.feature(str(ctag)))
        except Exception:
            pass

    try:
        for ftag in pg.feature().tags():
            visit(pg.feature(str(ftag)))
    except Exception:
        pass

    return units


# Spatial-coordinate column names COMSOL uses in plot exports (case-insensitive).
COORDINATE_NAMES = {'r', 'x', 'y', 'z', 'phi'}


def get_geometry_length_unit(model, pg) -> str:
    """Return the length unit of the geometry behind a plot group's dataset.

    Spatial-coordinate columns (R, Z, X, Y, ...) in COMSOL's plot exports use
    the model geometry's length unit (e.g. 'µm'), but - unlike expression
    columns - this isn't a feature property, so it's looked up via the plot
    group's dataset -> geometry.
    """
    try:
        ds_tag = str(pg.getString('data'))
        ds = model.java.result().dataset(ds_tag)
        geom_tag = str(ds.getString('geom'))
        return str(model.java.geom(geom_tag).lengthUnit())
    except Exception:
        return ''


def split_header_line(header_line: str, ncols: int) -> list[str] | None:
    """Split a COMSOL '%' header line into ncols column headers, or None if
    that isn't possible.

    Headers are normally separated by runs of 2+ spaces (a unit's own
    parentheses use single spaces). Multi-curve table-graph exports instead
    repeat 'Description (unit), <curve label>' back-to-back with single
    spaces; detect that by splitting on repeats of the '...(unit), ' prefix.
    """
    candidate = re.split(r'\s{2,}', header_line.strip())
    if len(candidate) == ncols:
        return candidate

    parts = []
    for piece in candidate:
        m = re.match(r'^(.*?[(\[][^()\[\]]*[)\]],\s*)', piece)
        if not m:
            parts.append(piece)
            continue
        prefix = m.group(1)
        parts.extend(s.strip() for s in re.split(f'(?={re.escape(prefix)})', piece) if s.strip())

    # Only trust the result if it produced the expected number of columns.
    return parts if len(parts) == ncols else None


def get_table_graph_headers(model, pg) -> list[str] | None:
    """Return column headers for a 'Probe Table Graph' plot from its source table.

    A Table Graph feature (source='table') plots columns of a result table
    rather than computing its own expressions: 'xaxisdata'/'plotcolumns' are
    1-indexed columns into the table's 'headers' property, which COMSOL's
    plot export sometimes fails to write into the export file's own header
    line (e.g. single-curve probe plots).
    """
    try:
        for ftag in pg.feature().tags():
            feat = pg.feature(str(ftag))
            names = {str(n) for n in feat.properties()}
            if not {'source', 'table', 'xaxisdata', 'plotcolumns'} <= names:
                continue
            if str(feat.getString('source')) != 'table':
                continue

            tbl = model.java.result().table(str(feat.getString('table')))
            table_headers = [str(row[1]) for row in tbl.getStringMatrix('headers')]

            # x-axis column first, then each plotted y column, matching the
            # export's column order.
            x_idx = int(feat.getInt('xaxisdata')) - 1
            y_idx = [int(i) - 1 for i in feat.getIntArray('plotcolumns')]
            cols = [x_idx] + y_idx
            if all(0 <= i < len(table_headers) for i in cols):
                return [table_headers[i] for i in cols]
    except Exception:
        pass

    return None


def parse_comsol_export(path: Path, units_map: dict[str, str] | None = None,
                         coordinate_unit: str = '',
                         fallback_headers: list[str] | None = None) -> tuple[pd.DataFrame, list[str]] | None:
    """Parse a COMSOL text export into (DataFrame, metadata comment lines).

    COMSOL prefixes the file with '%' lines holding metadata (Model, Version,
    Date, Description, ...). The last '%' line normally holds the column
    headers, separated by runs of 2+ spaces (split_header_line).

    units_map (see get_feature_units) fills in units that aren't embedded in
    the header text; coordinate_unit (see get_geometry_length_unit) fills in
    spatial-coordinate columns (COORDINATE_NAMES). If the export has no
    usable header line, fallback_headers (see get_table_graph_headers) is
    used if its length matches the data's column count.
    """
    text = repair_mojibake(path.read_text(encoding='utf-8', errors='replace'))
    comment_lines = [line[1:].strip() for line in text.splitlines() if line.startswith('%')]

    try:
        df = pd.read_csv(path, comment='%', sep=r'\s+', header=None)
    except Exception:
        return None
    if df.empty:
        return None

    headers = None
    meta = comment_lines
    if comment_lines:
        headers = split_header_line(comment_lines[-1], len(df.columns))
        if headers is not None:
            meta = comment_lines[:-1]

    if headers is None and fallback_headers and len(fallback_headers) == len(df.columns):
        headers = fallback_headers

    if headers:
        if units_map or coordinate_unit:
            merged = []
            for header in headers:
                name, unit = split_label_unit(header)
                if not unit and units_map:
                    unit = units_map.get(name, '')
                if not unit and coordinate_unit and name.lower() in COORDINATE_NAMES:
                    unit = coordinate_unit
                merged.append(f"{name} ({unit})" if unit else name)
            headers = merged
        df.columns = headers
    else:
        # No header info at all - fall back to generic names by column count.
        ncols = len(df.columns)
        if ncols == 2:
            df.columns = ['x', 'y']
        elif ncols == 3:
            df.columns = ['x', 'y', 'z']
        elif ncols == 4:
            df.columns = ['x', 'y', 'z', 'value']
        else:
            df.columns = [f'col{i}' for i in range(ncols)]

    return df, meta


def extract_via_export(model, pg, pg_tag: str, output_dir: Path) -> tuple[pd.DataFrame, list[str]] | None:
    """Extract a plot group's data via COMSOL's native text export into a DataFrame."""
    export_path = export_via_comsol(model, pg_tag, output_dir)
    if export_path is None:
        return None

    try:
        return parse_comsol_export(export_path, get_feature_units(pg), get_geometry_length_unit(model, pg),
                                    get_table_graph_headers(model, pg))
    except Exception as e:
        print(f"  [!] Could not parse export for '{pg_tag}': {e}")
        return None


# ---------------------------------------------------------------------------
# OriginLab integration (optional)
# ---------------------------------------------------------------------------

def load_datasets_from_folder(folder: Path) -> list[dict]:
    """Load CSVs from a previously written '<model>_results' folder for
    direct OriginLab import via push_to_origin(), without re-running the
    COMSOL extraction (e.g. when COMSOL isn't installed/licensed here, or its
    license is busy elsewhere).

    Uses manifest.json to find each extracted file and which section it
    belongs to, then reads it back with load_dataset_csv().
    """
    manifest_path = folder / 'manifest.json'
    if not manifest_path.exists():
        sys.exit(f"No manifest.json found in {folder} - pick a '<model>_results' folder.")

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    # 'table'/'1d' additionally get a line graph in push_to_origin().
    section_kinds = {
        'tables': 'table',
        '1d_plots': '1d',
        '2d_plots': '2d',
        '3d_plots': '3d',
        'other': 'other',
    }

    datasets = []
    for section, kind in section_kinds.items():
        for entry in manifest.get(section, []):
            fname = entry.get('file')
            if not fname:
                # Failed extraction recorded for diagnostics only - skip.
                continue
            path = folder / fname
            if not path.exists():
                print(f"  [!] Missing file referenced in manifest: {fname}")
                continue
            df, comments = load_dataset_csv(path)
            datasets.append({'name': Path(fname).stem, 'kind': kind, 'df': df,
                              'comments': comments or entry.get('comments', [])})
            print(f"  - Loaded {fname}  ({len(df)} rows x {len(df.columns)} cols)")

    return datasets


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

    try:
        for entry in datasets:
            name, kind, df = entry['name'], entry['kind'], entry['df']
            comments = entry.get('comments') or []

            # Long-format (x, y, group) multi-curve data -> wide format (one
            # y column per group) for proper multi-curve plotting.
            if 'group' in df.columns and {'x', 'y'}.issubset(df.columns):
                df = df.pivot_table(index='x', columns='group', values='y', sort=False).reset_index()
                df.columns = ['x'] + [str(c) for c in df.columns[1:]]

            wb = op.new_book('w', name)
            sheet = wb[0]
            sheet.from_df(df)
            if len(df.columns) >= 2:
                sheet.cols_axis('xy', repeat=True)

            # Carry column names/units (from 'Name (unit)' headers) over to
            # Origin's long name / units label rows.
            for i, col in enumerate(df.columns):
                label, unit = split_label_unit(col)
                sheet.set_label(i, label, type='L')
                if unit:
                    sheet.set_label(i, unit, type='U')

            if comments:
                try:
                    sheet.comments = '\n'.join(comments)
                except Exception:
                    pass

            # Tables and 1D plots also get a line graph of every Y vs X.
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
    except Exception:
        # Don't let a COM/Origin-side failure take down the whole script
        # after extraction has already succeeded.
        print("\n[!] Failed to push results to OriginLab:")
        traceback.print_exc()
    finally:
        # Detach originpro - if it launched its own hidden Origin, this asks
        # it to shut down; if it attached to one already open, it's a no-op.
        try:
            op.exit()
        except Exception:
            pass


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
    root.withdraw()
    root.attributes('-topmost', True)
    file_path = filedialog.askopenfilename(
        title='Select a COMSOL model file',
        filetypes=[
            ('COMSOL models', '*.mph'),
            ('All files', '*.*'),
        ],
    )
    root.destroy()
    return Path(file_path) if file_path else None


def pick_folder_dialog(title: str) -> Path | None:
    """Open a native folder-picker dialog and return the selected directory."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None

    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    folder = filedialog.askdirectory(title=title)
    root.destroy()
    return Path(folder) if folder else None


# Display names for the groups shown in the item-picker, in the order shown.
ITEM_GROUP_LABELS = {
    'table': 'Tables',
    '1d': '1D Plots',
    '2d': '2D Plots',
    '3d': '3D Plots',
}


def pick_extraction_dialog(items: list[dict], model_name: str, comsol_warning: str | None,
                            push_to_origin_default: bool) -> dict | None:
    """Combined window: COMSOL/OriginLab status (with a "Start OriginPro"
    button) and the checklist of tables/plot groups to extract, grouped by type.

    Every item is checked by default. Returns
    {'items': [...selected items...], 'push_to_origin': bool}, or None if the
    user cancelled (closed the window or clicked Cancel).
    """
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("COMSOL Extractor")
    root.attributes('-topmost', True)
    root.geometry('480x560')

    container = ttk.Frame(root, padding=10)
    container.pack(fill='both', expand=True)

    # -- Status section --
    status = ttk.LabelFrame(container, text="Status", padding=8)
    status.pack(fill='x', pady=(0, 8))

    comsol_row = ttk.Frame(status)
    comsol_row.pack(fill='x')
    comsol_led = tk.Canvas(comsol_row, width=14, height=14, highlightthickness=0)
    comsol_led.create_oval(2, 2, 12, 12, fill='#2ecc40', outline='')
    comsol_led.pack(side='left', padx=(0, 6))
    ttk.Label(comsol_row, text=f"COMSOL: model '{model_name}' loaded").pack(side='left')

    if comsol_warning:
        ttk.Label(status, text=comsol_warning, foreground='#cc6600',
                  wraplength=420, justify='left').pack(fill='x', anchor='w', pady=(4, 0))

    origin_row = ttk.Frame(status)
    origin_row.pack(fill='x', pady=(6, 0))
    origin_led = tk.Canvas(origin_row, width=14, height=14, highlightthickness=0)
    origin_oval = origin_led.create_oval(2, 2, 12, 12, fill='#b0b0b0', outline='')
    origin_led.pack(side='left', padx=(0, 6))
    origin_status_lbl = ttk.Label(origin_row, text="OriginLab: ...")
    origin_status_lbl.pack(side='left')

    push_var = tk.BooleanVar(value=push_to_origin_default)

    def refresh_origin_led() -> bool:
        running = origin_already_running()
        origin_led.itemconfig(origin_oval, fill='#2ecc40' if running else '#b0b0b0')
        origin_status_lbl.configure(text=f"OriginLab: {'running' if running else 'not running'}")
        return running

    start_btn = ttk.Button(origin_row, text="Start OriginPro")
    start_btn.pack(side='left', padx=(10, 0))

    def poll_origin_status(attempt: int):
        # Poll a few times after "Start OriginPro" is clicked, since the
        # process can take a couple of seconds to appear.
        if refresh_origin_led():
            push_var.set(True)
            start_btn.configure(state='normal', text="Start OriginPro")
            return
        if attempt >= 20:
            origin_status_lbl.configure(text="OriginLab: starting... (taking a while)")
            start_btn.configure(state='normal', text="Start OriginPro")
            return
        root.after(500, poll_origin_status, attempt + 1)

    def on_start_origin():
        start_btn.configure(state='disabled', text="Starting...")
        root.update_idletasks()
        ok, err = start_originpro()
        if not ok:
            origin_status_lbl.configure(text=f"OriginLab: failed to start ({err[:60]})")
            start_btn.configure(state='normal', text="Start OriginPro")
            return
        poll_origin_status(0)

    start_btn.configure(command=on_start_origin)
    refresh_origin_led()

    ttk.Checkbutton(status, text="Import extracted results into OriginLab",
                    variable=push_var).pack(anchor='w', pady=(6, 0))

    # -- Items section --
    ttk.Label(container, text="Select which tables/plots to extract:").pack(anchor='w')

    # The list of items can be long, so put it in a scrollable canvas.
    list_frame = ttk.Frame(container)
    list_frame.pack(fill='both', expand=True, pady=(5, 0))

    canvas = tk.Canvas(list_frame, borderwidth=0, highlightthickness=0)
    scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.create_window((0, 0), window=inner, anchor='nw')
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side='left', fill='both', expand=True)
    scrollbar.pack(side='right', fill='y')

    # One checkbox per item, grouped under bold headings by kind; a new
    # heading is only inserted when the group changes.
    variables = []
    last_group = None
    for item in items:
        group = item['kind'] if item['kind'] in ITEM_GROUP_LABELS else 'other'
        if group != last_group:
            ttk.Label(inner, text=ITEM_GROUP_LABELS.get(group, 'Other'),
                      font=('TkDefaultFont', 9, 'bold')).pack(
                anchor='w', pady=(8 if last_group else 0, 2))
            last_group = group
        var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inner, text=f"{item['tag']}: {item['label']}", variable=var).pack(
            anchor='w', padx=(10, 0))
        variables.append(var)

    result = {'items': None, 'push_to_origin': False}

    def select_all():
        for v in variables:
            v.set(True)

    def deselect_all():
        for v in variables:
            v.set(False)

    def on_extract():
        result['items'] = [item for item, v in zip(items, variables) if v.get()]
        result['push_to_origin'] = push_var.get()
        root.destroy()

    def on_cancel():
        result['items'] = None
        root.destroy()

    btn_frame = ttk.Frame(container)
    btn_frame.pack(fill='x', pady=(10, 0))
    ttk.Button(btn_frame, text="Select All", command=select_all).pack(side='left')
    ttk.Button(btn_frame, text="Deselect All", command=deselect_all).pack(side='left', padx=5)
    ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side='right')
    ttk.Button(btn_frame, text="Extract", command=on_extract).pack(side='right', padx=5)

    root.protocol('WM_DELETE_WINDOW', on_cancel)
    root.mainloop()
    return result if result['items'] is not None else None


def main():
    parser = argparse.ArgumentParser(
        description='Extract results from a COMSOL .mph file and/or import them into OriginLab'
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('model', nargs='?', default=None,
                        help='Path to .mph file (opens file dialog if omitted)')
    parser.add_argument('--output', '-o', default=None,
                        help='Output directory (default: folder next to .mph named <model>_results/)')
    parser.add_argument('--comsol', action='store_true',
                        help='Extract results from a COMSOL .mph model')
    parser.add_argument('--origin', action='store_true',
                        help='Import results into OriginLab (requires originpro)')
    parser.add_argument('--origin-template', default='',
                        help='Origin graph template (.otpu) to use')
    args = parser.parse_args()

    # If neither --comsol nor --origin was given, default to extracting from
    # COMSOL; whether to also push to OriginLab is decided in the combined
    # status/items dialog below.
    if args.comsol or args.origin:
        do_comsol, do_origin = args.comsol, args.origin
    else:
        do_comsol, do_origin = True, False

    if not do_comsol:
        # -- OriginLab-only mode: import a previously extracted folder --
        # (do_origin is guaranteed True - reached only via `--origin` without
        # `--comsol` on the command line.)
        if not origin_already_running():
            confirm_or_exit(
                "NOTE: OriginLab does not appear to be running.\n"
                "Start OriginLab now so --origin can connect to it (originpro "
                "may otherwise fail to launch it automatically)."
            )

        print("Select the '<model>_results' folder to import into OriginLab...")
        folder = pick_folder_dialog("Select results folder to import into OriginLab")
        if folder is None:
            sys.exit("No folder selected. Exiting.")
        folder = folder.resolve()

        print(f"Loading datasets from: {folder}")
        datasets = load_datasets_from_folder(folder)
        if not datasets:
            sys.exit("No datasets found to import.")

        print("\nPushing results to OriginLab...")
        origin_pids_before = get_origin_pids()
        push_to_origin(datasets, folder, template=args.origin_template)
        close_new_origin_processes(origin_pids_before)

        os.startfile(folder)  # open the results folder in File Explorer
        print("Done.")
        pause_if_frozen()
        return

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

    # COMSOL already-running warning is shown inline in the combined dialog
    # below, rather than a blocking console prompt.
    comsol_warning = None
    if comsol_already_running():
        comsol_warning = (
            "A COMSOL process is already running - this session will launch "
            "an additional engine instance, using extra memory and a "
            "separate license seat."
        )

    # -- Start COMSOL server and load model --
    print("Starting COMSOL server...")
    client = mph.start()
    print(f"Loading model: {model_path.name}")
    model = client.load(str(model_path))
    print(f"Model loaded.\n")

    # Top-level summary written to manifest.json at the end. Each list holds
    # one dict per successfully extracted item (tag, label, output filename,
    # row/column counts, and any comments).
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

    try:
        tbl_tags = [str(t) for t in java_result.table().tags()]
    except Exception as e:
        print(f"  [!] Could not list result tables: {e}")
        tbl_tags = []

    # Top-level result node tags - includes plot groups ('pg1', 'pg2', ...)
    # as well as other result-tree entries.
    try:
        pg_tags = [str(t) for t in java_result.tags()]
    except Exception as e:
        print(f"  [!] Could not list plot groups: {e}")
        pg_tags = []

    # Flat list of everything the user can choose to extract, tagged with its
    # 'kind' (table/1d/2d/3d/unknown) for grouping in the checklist and for
    # picking the right extraction routine later.
    items = []
    for tag in tbl_tags:
        try:
            label = str(java_result.table(tag).label())
        except Exception:
            label = tag
        items.append({'tag': tag, 'label': label, 'kind': 'table'})

    for tag in pg_tags:
        try:
            pg = model.java.result(tag)
            label = str(pg.label()) if hasattr(pg, 'label') else tag
            class_name = str(pg.getClass().getSimpleName())
        except Exception as e:
            print(f"  [!] Could not access plot group '{tag}': {e}")
            continue
        items.append({'tag': tag, 'label': label, 'kind': get_plot_type(pg),
                       'class_name': class_name, 'pg': pg})

    if not items:
        client.clear()
        sys.exit("No extractable tables or plot groups found in this model.")

    # -- Combined status + item-selection window --
    print(f"Found {len(items)} extractable item(s). Opening selection window...")
    choice = pick_extraction_dialog(items, model_path.name, comsol_warning, do_origin)
    if choice is None or not choice['items']:
        print("Nothing selected. Exiting.")
        client.clear()
        return
    selected = choice['items']
    do_origin = choice['push_to_origin']

    # ---- Extract selected items ----
    output_dir.mkdir(parents=True, exist_ok=True)
    for item in selected:
        tag, label, kind = item['tag'], item['label'], item['kind']

        if kind == 'table':
            print(f"  - {tag} ({label})")
            result = extract_table(model, tag)
            if result is not None and not result[0].empty:
                df, comments = result
                name = sanitize_filename(f"table_{tag}_{label}")
                fname = name + '.csv'
                write_csv_with_comments(df, output_dir / fname, comments)
                manifest['tables'].append({'tag': tag, 'label': label, 'file': fname,
                                           'rows': len(df), 'cols': list(df.columns),
                                           'comments': comments})
                datasets.append({'name': name, 'kind': 'table', 'df': df, 'comments': comments})
                print(f"    -> Saved {fname}  ({len(df)} rows x {len(df.columns)} cols)")
            continue

        # Everything else (1D/2D/3D plot groups, or anything else
        # get_plot_type() couldn't classify) goes through COMSOL's "Plot"
        # export and is written as 'plot<kind>_<tag>_<label>.csv'.
        pg, class_name, ptype = item['pg'], item['class_name'], kind
        print(f"\n  [{ptype.upper():>7}] {tag} ({label})  [{class_name}]")

        result = extract_via_export(model, pg, tag, output_dir)
        if result is not None and not result[0].empty:
            df, comments = result

            try:
                note = str(pg.comments())
                if note:
                    comments = [note] + comments
            except Exception:
                pass

            name = sanitize_filename(f"plot{ptype}_{tag}_{label}")
            fname = name + '.csv'
            write_csv_with_comments(df, output_dir / fname, comments)
            entry = {'tag': tag, 'label': label, 'file': fname,
                     'rows': len(df), 'cols': list(df.columns), 'comments': comments}
            if ptype in ('1d', '2d', '3d'):
                manifest[f'{ptype}_plots'].append(entry)
            else:
                # Unrecognized plot dimension - record under 'other' with its
                # Java class name for diagnostics.
                entry['type'] = class_name
                manifest['other'].append(entry)
            datasets.append({'name': name, 'kind': ptype, 'df': df, 'comments': comments})
            print(f"    -> Saved {fname}  ({len(df)} rows x {len(df.columns)} cols)")
        else:
            # Export/parse failed (e.g. empty plot, unsupported export) -
            # record it without a file so it's still visible in the manifest.
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
    if do_origin:
        print("\nPushing results to OriginLab...")
        origin_pids_before = get_origin_pids()
        push_to_origin(datasets, output_dir, template=args.origin_template)
        close_new_origin_processes(origin_pids_before)

    # -- Open the output folder and clean up --
    os.startfile(output_dir)  # open the results folder in File Explorer
    client.clear()
    print("Done.")
    pause_if_frozen()


if __name__ == '__main__':
    main()
