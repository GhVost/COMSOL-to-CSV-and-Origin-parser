"""
COMSOL .mph Result Extractor
=============================
Extracts all result tables and plot groups (1D/2D/3D) from a COMSOL model
and saves them as CSV files ready for OriginLab import.

Each CSV starts with any model/description metadata or user "Comments" as
leading '%' comment lines (also recorded in manifest.json), followed by two
header rows - column names and units, split from COMSOL's combined
'Name (unit)' / 'Name [unit]' column headers - and then the data. Units that
COMSOL stores as a separate plot-feature property (not in the header text),
or that belong to spatial-coordinate columns (R, X, Y, Z, ...) and come from
the geometry's length unit, are filled in from the model, and mis-encoded
unit symbols (e.g. 'Âµm') are repaired. With --origin, those names/units are
applied to the Origin worksheet's long name and units label rows, and the
comments to the sheet's comments.

Requirements:
    - COMSOL Multiphysics installed (any version 5.x / 6.x)
    - Python 3.10+ (uses 'X | Y' union type hints evaluated at runtime)
    - pip install MPh pandas numpy
    - pip install originpro and OriginLab installed (only for --origin)

Usage:
    python ComsolExtractor.py --origin

Opens a file dialog to pick the .mph model, then a checklist window listing
every table and plot group found in the model - pick which ones to extract,
and optionally build an OriginLab project from them. A model path, --output
<dir> and --origin-template <file> can also be given; see --help for details.

Output is saved to a folder named <model_name>_results/ next to the .mph file.

Module layout (in order):
    Helpers              - filename sanitizing, header/unit splitting,
                            mojibake repair, CSV writing, process checks,
                            and result-feature type classification.
    Extraction routines  - extract_table() for result tables, and a group of
                            functions around extract_via_export() for plot
                            groups (1D/2D/3D), which export via COMSOL's
                            built-in "Plot" exporter and then parse/repair
                            the resulting text file's headers and units.
    OriginLab integration - push_to_origin(), optional, builds an .opju
                            project directly from the extracted DataFrames.
    Main                 - CLI argument parsing, file/result picker dialogs,
                            and the overall extraction loop that ties
                            everything together and writes manifest.json.
"""

__version__ = '1.1.0'

import argparse
import sys
import re
import csv
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
    # Replace characters that Windows/Unix filesystems reject or treat
    # specially (path separators, wildcards, drive-letter colon, ...).
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Collapse any run of whitespace (COMSOL labels often contain spaces)
    # into a single underscore.
    name = re.sub(r'\s+', '_', name)
    # Trim stray leading/trailing underscores and cap the length so the
    # final '<name>_export.txt' / '<name>.csv' path stays reasonable.
    return name.strip('_')[:120]


def split_label_unit(label: str) -> tuple[str, str]:
    """Split a column header into (name, unit).

    Most COMSOL headers end in '(unit)'/'[unit]', e.g. 'Displacement
    magnitude (µm)'. Multi-curve 'Table graph' exports instead append a
    per-curve label after the unit, e.g. 'Kinetic energy density (J/m^3),
    ring' - if no unit is found at the end, fall back to pulling the first
    '(unit)'/'[unit]' out of the middle of the string.
    """
    label = str(label).strip()
    # Primary case: the whole label ends in '(unit)' or '[unit]', e.g.
    # 'Displacement magnitude (um)' -> name='Displacement magnitude', unit='um'.
    m = re.match(r'^(.*?)\s*[(\[]([^()\[\]]*)[)\]]\s*$', label)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Fallback case: the '(unit)'/'[unit]' group is somewhere in the middle,
    # with extra text trailing after it (multi-curve table-graph exports
    # append ', <curve label>' after the unit), e.g.
    # 'Kinetic energy density (J/m^3), ring' -> name='Kinetic energy density, ring', unit='J/m^3'.
    m = re.search(r'[(\[]([^()\[\]]*)[)\]]', label)
    if not m:
        # No bracketed unit anywhere - treat the entire label as the name.
        return label, ''
    # Cut the '(unit)'/'[unit]' substring out of the label, then tidy up
    # the leftover whitespace/comma so e.g. 'foo  , bar' -> 'foo, bar'.
    name = label[:m.start()] + label[m.end():]
    name = re.sub(r'\s{2,}', ' ', name)
    name = re.sub(r'\s*,\s*', ', ', name)
    return name.strip(' ,'), m.group(1).strip()


def repair_mojibake(text: str) -> str:
    """Fix text COMSOL's export wrote as UTF-8 bytes re-interpreted as
    Windows-1252 (e.g. the unit 'µm' coming out as 'Âµm')."""
    try:
        # Re-encoding the (incorrectly decoded) text as cp1252 reproduces the
        # original UTF-8 byte sequence, which can then be decoded properly.
        return text.encode('cp1252').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        # Round-trip failed -> the text wasn't mojibake in the first place;
        # return it unchanged.
        return text


def write_csv_with_comments(df: pd.DataFrame, path: Path, comments: list[str] | None = None):
    """Write a DataFrame to CSV with COMSOL metadata as leading '%' comment lines,
    followed by a row of column names and a row of units (split from the
    'Name (unit)' headers), then the data."""
    # Split each column's 'Name (unit)' header into two parallel tuples so
    # they can be written as two separate header rows.
    names, units = zip(*(split_label_unit(col) for col in df.columns))
    with open(path, 'w', encoding='utf-8', newline='') as f:
        # Leading metadata/description/user-comment lines, COMSOL-style
        # ('%'-prefixed), written before any CSV header/data rows.
        for line in comments or []:
            f.write(f"% {line}\n")
        writer = csv.writer(f)
        # Row 1: bare column names (units stripped out).
        writer.writerow(names)
        # Row 2: matching units, blank for columns that have none.
        writer.writerow(units)
        # Remaining rows: the numeric data itself, one row per sample/point.
        for row in df.itertuples(index=False, name=None):
            writer.writerow(row)


def load_dataset_csv(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Read a CSV written by write_csv_with_comments() back into a
    (DataFrame, comments) pair.

    This is the reverse operation: leading '%' lines are read back as
    comments, and the following two header rows (column names, then units)
    are recombined into single 'Name (unit)' column headers - so the
    DataFrame looks the same as one freshly extracted from COMSOL. Used to
    re-import a previously extracted '<model>_results' folder into OriginLab
    without needing COMSOL running again.
    """
    with open(path, 'r', encoding='utf-8', newline='') as f:
        lines = f.readlines()

    # Leading '%' lines are metadata/comments, same convention as COMSOL's
    # own text exports.
    comments = []
    i = 0
    while i < len(lines) and lines[i].startswith('%'):
        comments.append(lines[i][1:].strip())
        i += 1

    # The next two rows are the column names and units written by
    # write_csv_with_comments(); zip them back into 'Name (unit)' headers
    # (or just 'Name' if there's no unit for that column).
    names = next(csv.reader([lines[i]]))
    units = next(csv.reader([lines[i + 1]]))
    headers = [f"{name} ({unit})" if unit else name for name, unit in zip(names, units)]

    df = pd.read_csv(path, skiprows=i + 2, header=None, names=headers)
    return df, comments


def comsol_already_running() -> bool:
    """Check whether a COMSOL Desktop/server process is already running."""
    if psutil is None:
        # psutil not installed - we can't enumerate processes, so assume
        # nothing is running (the caller will just skip the warning).
        return False
    # Scan all running processes for one whose name starts with 'comsol'
    # (covers comsol.exe, comsolmphserver.exe, COMSOL Multiphysics.exe, ...).
    for proc in psutil.process_iter(['name']):
        name = (proc.info.get('name') or '').lower()
        if name.startswith('comsol'):
            return True
    return False


def origin_already_running() -> bool:
    """Check whether OriginLab (Origin/OriginPro) is already running."""
    if psutil is None:
        return False
    # Same approach as comsol_already_running(), looking for an
    # Origin*.exe / OriginPro*.exe process instead.
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
    push_to_origin() was called.

    originpro launches its own hidden Origin instance via COM if one
    wasn't already running, and op.exit() doesn't always fully tear that
    instance down afterwards - leaving a process that keeps the .opju file
    locked for further operations. Compare against pids_before (captured
    before push_to_origin()) and only close processes that appeared since,
    leaving any Origin session the user already had open untouched.
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


def confirm_or_exit(message: str):
    """Print a message and wait for the user to press Enter (or Ctrl+C to abort)."""
    print(message)
    try:
        # Block until the user acknowledges the warning; Ctrl+C aborts the
        # whole script instead of barging ahead with an unwanted extra
        # COMSOL/Origin instance.
        input("Press Enter to continue, or Ctrl+C to abort... ")
    except KeyboardInterrupt:
        sys.exit("\nAborted by user.")


def get_plot_type(java_plot) -> str:
    """Classify a result feature as table / 1d / 2d / 3d / other.

    MPh wraps every result node in a generic 'ResultFeatureClient' proxy, so
    the Java class name is useless for classification. Instead, ask COMSOL
    for the feature's type string (e.g. 'PlotGroup1D', 'PlotGroup3D').
    """
    try:
        # getType() returns a short identifier like 'PlotGroup1D' or
        # 'Table'; lowercase it so the substring checks below are
        # case-insensitive.
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
    # This covers any plot-group subtype whose getType() string doesn't
    # match the patterns above but still exposes a numeric dimension.
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
    """
    Extract a COMSOL result table into a DataFrame, plus any user comments.

    Tables are stored under model.result().table(tag). Column headers
    (units already included, e.g. 'freq (GHz)') come from the table's
    'headers' property - a [index, description] matrix - while the data
    itself comes from getTableData(True) as strings (COMSOL 6.4 dropped the
    no-arg getTableData()/getColumnHeader()/getDoubleValue() API).
    """
    try:
        tbl = model.java.result().table(tag)

        # 'headers' is a String[][] property: each row is
        # [1-indexed column number as text, "Description (unit)"].
        # We only need the description/unit text (column index 1).
        headers = [str(row[1]) for row in tbl.getStringMatrix('headers')]

        # getTableData(True) returns the full data grid as String[][]
        # (one row per sample, one column per header). Each cell is a Java
        # String (e.g. "3.4065" or "inf"/"NaN"), so convert via str() first
        # before float() - Python's float() accepts "inf"/"NaN" natively.
        rows = tbl.getTableData(True)
        data = np.array([[float(str(cell)) for cell in row] for row in rows], dtype=float)

        df = pd.DataFrame(data, columns=headers)

        # Pick up any user-entered "Comments" text on the table node, if set.
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
    # Temporary export-node tag; created and removed within this function so
    # repeated calls don't accumulate leftover export nodes in the model.
    export_tag = f'exp_{pg_tag}'

    # Try the real output directory first, then fall back to the system
    # temp directory if COMSOL refuses to write there (e.g. the model's
    # folder is read-only or locked).
    for target_dir in (output_dir, Path(tempfile.gettempdir())):
        export_path = target_dir / fname
        try:
            # Create a one-off 'Plot' export node pointing at this plot
            # group, run it to write the text file, then...
            exp = model.java.result().export().create(export_tag, 'Plot')
            exp.set('plotgroup', pg_tag)
            exp.set('filename', str(export_path))
            exp.run()
        except Exception as e:
            print(f"  [!] COMSOL export to '{target_dir}' failed for '{pg_tag}': {e}")
            continue
        finally:
            # ...always remove the temporary export node again, whether or
            # not exp.run() succeeded, to avoid cluttering the model.
            try:
                model.java.result().export().remove(export_tag)
            except Exception:
                pass

        if export_path.exists():
            if target_dir != output_dir:
                # Exported to the temp dir as a fallback - move the file
                # into the real output directory so it ends up alongside
                # the CSV and manifest.
                output_dir.mkdir(parents=True, exist_ok=True)
                final_path = output_dir / fname
                shutil.move(str(export_path), str(final_path))
                return final_path
            return export_path

    # Both the output directory and the temp-dir fallback failed.
    return None


def get_feature_units(pg) -> dict[str, str]:
    """Map each axis description/expression to its COMSOL 'unit' property.

    A plot feature (e.g. Line Graph, Surface, or a nested Deformation
    sub-feature) can carry a unit as a property separate from its
    description/expression - COMSOL's text export omits such units from the
    column header entirely. COMSOL pairs these by name prefix, e.g.
    'unit'/'descr'/'expr' for a feature's main expression, or
    'xdataunit'/'xdatadescr'/'xdataexpr' for its x-axis. A 'unit' property
    can also be a string array (e.g. a Deformation sub-feature's per-axis
    units), paired positionally with a same-length 'expr' (or 'descr')
    array. Nested features are walked recursively.
    """
    units = {}

    def visit(feat):
        # List every property name on this feature (e.g. 'unit', 'descr',
        # 'expr', 'xdataunit', 'xdatadescr', ...). If the call fails (some
        # feature types don't support properties()), just skip it.
        try:
            names = {str(n) for n in feat.properties()}
        except Exception:
            names = set()

        for prop in names:
            # Only interested in properties whose name ends in 'unit'
            # (covers 'unit', 'xdataunit', 'ydataunit', 'zdataunit', ...).
            if not prop.lower().endswith('unit'):
                continue
            try:
                unit_type = str(feat.getValueType(prop))
            except Exception:
                continue

            # The matching label property shares the same prefix, e.g.
            # 'unit' pairs with 'descr'/'expr', 'xdataunit' pairs with
            # 'xdatadescr'/'xdataexpr'. Try description first, then
            # expression, and use whichever exists with a matching type.
            prefix = prop[:-len('unit')]
            for label_prop in (prefix + 'descr', prefix + 'expr'):
                if label_prop not in names:
                    continue
                try:
                    # Both the unit and its label must be the same Java
                    # value type (String vs StringArray) to pair them up.
                    if str(feat.getValueType(label_prop)) != unit_type:
                        continue
                    if unit_type == 'String':
                        # Single expression with a single unit, e.g.
                        # descr='Total displacement', unit='m'.
                        unit_val = str(feat.getString(prop)).strip()
                        label_val = str(feat.getString(label_prop)).strip()
                        if unit_val and label_val:
                            units[label_val] = unit_val
                            break
                    elif unit_type == 'StringArray':
                        # Multiple expressions with per-entry units (e.g. a
                        # Deformation sub-feature's x/y/z components) -
                        # pair them up positionally.
                        unit_vals = [str(v).strip() for v in feat.getStringArray(prop)]
                        label_vals = [str(v).strip() for v in feat.getStringArray(label_prop)]
                        if len(unit_vals) == len(label_vals):
                            for label_val, unit_val in zip(label_vals, unit_vals):
                                if label_val and unit_val:
                                    units[label_val] = unit_val
                            break
                except Exception:
                    continue

        # Recurse into any nested sub-features (e.g. a Surface plot's
        # Deformation/Height Expression sub-features), which can carry
        # their own unit properties independent of the parent.
        try:
            for ctag in feat.feature().tags():
                visit(feat.feature(str(ctag)))
        except Exception:
            pass

    # Walk every top-level feature of the plot group (Line Graph, Surface,
    # Contour, ... - whatever was added to this plot).
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

    Spatial-coordinate columns (R, Z, X, Y, ...) in COMSOL's plot exports are
    given in the model geometry's length unit (e.g. 'µm'), but - unlike
    expression columns - this isn't recorded as a feature property, so it
    has to be looked up via the plot group's dataset.
    """
    try:
        # Every plot group references a dataset (its 'data' property),
        # which in turn references the geometry it was meshed/solved on
        # ('geom'). The geometry node itself exposes the model's length
        # unit, e.g. 'um', 'mm', 'm'.
        ds_tag = str(pg.getString('data'))
        ds = model.java.result().dataset(ds_tag)
        geom_tag = str(ds.getString('geom'))
        return str(model.java.geom(geom_tag).lengthUnit())
    except Exception:
        # Any step can fail (e.g. dataset without a geometry) - just
        # return '' and let callers treat that as "no coordinate unit".
        return ''


def split_header_line(header_line: str, ncols: int) -> list[str] | None:
    """Split a COMSOL '%' header line into ncols column headers, or None if
    that isn't possible.

    Headers are normally separated by runs of 2+ spaces (a unit's own
    parentheses use single spaces). Multi-curve 'Table graph' exports
    instead repeat 'Description (unit), <curve label>' back-to-back with
    single spaces and no inter-header padding; detect that by splitting on
    repeats of the '<description> (<unit>), ' prefix.
    """
    # Common case: split on runs of 2+ spaces. If that already yields the
    # right number of columns, we're done.
    candidate = re.split(r'\s{2,}', header_line.strip())
    if len(candidate) == ncols:
        return candidate

    # Otherwise, some piece(s) likely contain multiple
    # 'Description (unit), <curve label>' headers glued together with only
    # single spaces. For each piece, find the '...(unit), ' prefix and split
    # the piece every time that exact prefix recurs.
    parts = []
    for piece in candidate:
        m = re.match(r'^(.*?[(\[][^()\[\]]*[)\]],\s*)', piece)
        if not m:
            # No '(unit), ' pattern found - keep the piece as a single header.
            parts.append(piece)
            continue
        prefix = m.group(1)
        # Split right before each repetition of 'prefix' (a zero-width
        # lookahead keeps the prefix attached to the following text), then
        # drop any empty fragments.
        parts.extend(s.strip() for s in re.split(f'(?={re.escape(prefix)})', piece) if s.strip())

    # Only trust the result if it produced exactly the expected number of
    # columns - otherwise the caller falls back to generic names.
    return parts if len(parts) == ncols else None


def get_table_graph_headers(model, pg) -> list[str] | None:
    """Return column headers for a 'Probe Table Graph' plot from its source table.

    A Table Graph feature (source='table') plots columns of a result table
    (table='tbl1') rather than computing its own expressions: 'xaxisdata' is
    the 1-indexed x-axis column and 'plotcolumns' the 1-indexed y-axis
    column(s). The table's own 'headers' property already has each column's
    'Description (unit)' text, which COMSOL's plot export sometimes fails to
    write into the export file's header line (e.g. single-curve probe
    plots) - use that as the authoritative source in [x, *ys] order.
    """
    try:
        # A plot group can have multiple features; look for the one that
        # plots straight from a table (a "Probe Table Graph").
        for ftag in pg.feature().tags():
            feat = pg.feature(str(ftag))
            names = {str(n) for n in feat.properties()}
            # All four properties must be present, and 'source' must be
            # 'table' - otherwise this feature computes its own
            # expressions and isn't table-backed.
            if not {'source', 'table', 'xaxisdata', 'plotcolumns'} <= names:
                continue
            if str(feat.getString('source')) != 'table':
                continue

            # Look up the referenced table and read its column
            # descriptions/units (same 'headers' property used by
            # extract_table()).
            tbl = model.java.result().table(str(feat.getString('table')))
            table_headers = [str(row[1]) for row in tbl.getStringMatrix('headers')]

            # 'xaxisdata' and 'plotcolumns' are 1-indexed column numbers
            # into the table; convert to 0-indexed positions into
            # table_headers. The x-axis column comes first, followed by
            # each plotted y column, matching the export's column order.
            x_idx = int(feat.getInt('xaxisdata')) - 1
            y_idx = [int(i) - 1 for i in feat.getIntArray('plotcolumns')]
            cols = [x_idx] + y_idx
            if all(0 <= i < len(table_headers) for i in cols):
                return [table_headers[i] for i in cols]
    except Exception:
        # Not a table-backed plot, or one of the properties/lookups above
        # failed - the caller will fall back to the export's own header
        # line (or generic x/y/z/value names).
        pass

    return None


def parse_comsol_export(path: Path, units_map: dict[str, str] | None = None,
                         coordinate_unit: str = '',
                         fallback_headers: list[str] | None = None) -> tuple[pd.DataFrame, list[str]] | None:
    """Parse a COMSOL text export into (DataFrame, metadata comment lines).

    COMSOL prefixes the file with '%' lines holding metadata (Model, Version,
    Date, Description, ...). The last '%' line normally holds the column
    headers, with each header (e.g. 'Total displacement (m)') separated from
    the next by a run of 2+ spaces, while the unit's parentheses use single
    spaces - so splitting on '\\s{2,}' recovers the per-column labels.

    Some columns get their unit from a separate plot-feature property rather
    than from '(unit)' in the header text (see get_feature_units); units_map
    fills those in by matching the column's name. Spatial-coordinate columns
    (see COORDINATE_NAMES) fall back to coordinate_unit (see
    get_geometry_length_unit), since they carry no feature property at all.

    If the export's own header line is missing or unusable, fallback_headers
    (see get_table_graph_headers) is used instead, when its length matches
    the data's column count.
    """
    # Fix any mojibake before splitting into lines, so unit symbols like
    # 'um' decode correctly regardless of which line they end up on.
    text = repair_mojibake(path.read_text(encoding='utf-8', errors='replace'))
    # Every '%'-prefixed line is metadata/header text; strip the '%' and
    # surrounding whitespace, keeping the original order.
    comment_lines = [line[1:].strip() for line in text.splitlines() if line.startswith('%')]

    try:
        # pandas skips '%' comment lines automatically; columns are
        # whitespace-separated with no header row of their own.
        df = pd.read_csv(path, comment='%', sep=r'\s+', header=None)
    except Exception:
        return None
    if df.empty:
        return None

    # Try to recover per-column headers from the last '%' line (COMSOL puts
    # the column header line immediately before the data, if it writes one
    # at all).
    headers = None
    meta = comment_lines
    if comment_lines:
        headers = split_header_line(comment_lines[-1], len(df.columns))
        if headers is not None:
            # The last comment line was consumed as the header line, so
            # don't also write it out as a '%' metadata line in the CSV.
            meta = comment_lines[:-1]

    # No usable header line in the export itself (e.g. a single-curve
    # Probe Table Graph) - use the table-derived headers instead, if they
    # match the column count.
    if headers is None and fallback_headers and len(fallback_headers) == len(df.columns):
        headers = fallback_headers

    if headers:
        if units_map or coordinate_unit:
            # Fill in units that COMSOL didn't embed in the header text
            # itself: first from per-feature unit properties (units_map),
            # then - for spatial coordinate columns only - from the
            # geometry's length unit (coordinate_unit).
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
        # No header information available at all - fall back to generic
        # names based on column count (no units).
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
        # Gather everything parse_comsol_export() needs to recover full
        # 'Name (unit)' headers: per-feature units, the geometry's length
        # unit (for coordinate columns), and - as a last resort - headers
        # read directly from a source table for table-backed plots.
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
    COMSOL extraction (e.g. when COMSOL isn't installed/licensed on this
    machine, or its license is busy elsewhere).

    Uses manifest.json to find each extracted file and which section
    (tables/1d_plots/2d_plots/3d_plots/other) it belongs to, then reads it
    back with load_dataset_csv().
    """
    manifest_path = folder / 'manifest.json'
    if not manifest_path.exists():
        sys.exit(f"No manifest.json found in {folder} - pick a '<model>_results' folder.")

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    # Map each manifest section to the 'kind' push_to_origin() expects:
    # 'table'/'1d' additionally get a line graph, the rest just a worksheet.
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
                # Entries without a 'file' key represent failed extractions
                # recorded for diagnostics only - nothing to load.
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

    for entry in datasets:
        name, kind, df = entry['name'], entry['kind'], entry['df']
        comments = entry.get('comments') or []

        # Multiple curves sharing one x-axis come back as long-format (x, y, group);
        # pivot to wide format (one y column per group) for proper multi-curve plotting.
        if 'group' in df.columns and {'x', 'y'}.issubset(df.columns):
            df = df.pivot_table(index='x', columns='group', values='y', sort=False).reset_index()
            df.columns = ['x'] + [str(c) for c in df.columns[1:]]

        # Create one new worksheet per extracted table/plot, named after
        # the COMSOL tag+label, and load the DataFrame into it.
        wb = op.new_book('w', name)
        sheet = wb[0]
        sheet.from_df(df)
        if len(df.columns) >= 2:
            # Mark the first column as X and the rest as Y, repeating the
            # X/Y pattern for any extra column groups.
            sheet.cols_axis('xy', repeat=True)

        # Carry column names and units (parsed from 'Name (unit)' headers)
        # over to Origin's long name / units label rows.
        for i, col in enumerate(df.columns):
            label, unit = split_label_unit(col)
            sheet.set_label(i, label, type='L')
            if unit:
                sheet.set_label(i, unit, type='U')

        # Carry over any COMSOL metadata/user comments as the sheet's
        # comments field, if Origin's API allows setting it.
        if comments:
            try:
                sheet.comments = '\n'.join(comments)
            except Exception:
                pass

        # For tables and 1D plots, also create a line graph plotting every
        # Y column against the first (X) column.
        if kind in ('table', '1d') and len(df.columns) >= 2:
            graph = op.new_graph(template=template) if template else op.new_graph()
            layer = graph[0]
            for i in range(1, len(df.columns)):
                layer.add_plot(sheet, coly=i, colx=0)
            layer.rescale()
            graph.lname = name

        print(f"  -> Imported: {name}")

    # Save everything as a single .opju project file in the output folder.
    opju_path = output_dir / 'comsol_results.opju'
    op.save(str(opju_path))
    print(f"\nOrigin project saved: {opju_path}")

    # Detach originpro from this Origin instance. If originpro launched its
    # own hidden Origin process, op.exit() asks it to shut down; if it
    # attached to an Origin the user already had open, this is a no-op.
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
    # askopenfilename() returns '' if the user cancelled.
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
    # askdirectory() returns '' if the user cancelled.
    return Path(folder) if folder else None


def pick_mode_dialog() -> dict | None:
    """Show a single dialog to choose which steps to run - extracting from a
    COMSOL model (--comsol) and/or importing results into OriginLab
    (--origin) - shown when neither option was given on the command line.

    This combines the old mode-picker window with the separate
    console-based pre-flight checks into one interactive window:

    - Each option has a status LED. OriginLab's LED reflects whether an
      Origin/OriginPro process is currently running (cheap process check).
    - COMSOL's LED instead reflects an actual attempt to start a COMSOL
      server (mph.start()) - a real availability check (server reachable,
      license seat free), not just "is comsol.exe running". This runs once
      the window has rendered its "checking..." status, then updates to
      green/"server available" or red/"unavailable: ...". JPype's JVM must
      be started on the main thread, so this check briefly blocks the
      window rather than running in a background thread.
    - If that COMSOL server check succeeds and the user keeps "Extract
      from COMSOL" ticked, the already-started client is handed back and
      reused for extraction (no second server is started). If COMSOL isn't
      selected, the test client is shut down again before the dialog
      closes.
    - Any warnings that used to be separate confirm_or_exit() prompts (e.g.
      "OriginLab isn't running yet") are shown inline here instead.

    Returns {'comsol': bool, 'origin': bool, 'comsol_client': client|None},
    or None if the user cancelled. At least one of the two must be selected
    to proceed.
    """
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Select extraction steps")
    root.attributes('-topmost', True)
    root.resizable(False, False)

    container = ttk.Frame(root, padding=15)
    container.pack(fill='both', expand=True)

    ttk.Label(container, text="Choose which steps to run:",
              font=('TkDefaultFont', 9, 'bold')).grid(
        row=0, column=0, columnspan=3, sticky='w', pady=(0, 10))

    comsol_var = tk.BooleanVar(value=True)
    origin_var = tk.BooleanVar(value=origin_already_running())

    # Filled in by run_comsol_check() below.
    state = {'comsol_status': 'checking', 'comsol_client': None, 'comsol_error': None}

    leds = {}
    status_labels = {}

    def add_row(row: int, text: str, var: tk.BooleanVar, key: str, initial_text: str):
        ttk.Checkbutton(container, text=text, variable=var).grid(
            row=row, column=0, sticky='w', pady=3)
        # A small filled circle, recoloured as each check completes.
        led = tk.Canvas(container, width=14, height=14, highlightthickness=0)
        oval = led.create_oval(2, 2, 12, 12, fill='#b0b0b0', outline='')
        led.grid(row=row, column=1, padx=(15, 5))
        lbl = ttk.Label(container, text=initial_text)
        lbl.grid(row=row, column=2, sticky='w')
        leds[key] = (led, oval)
        status_labels[key] = lbl

    add_row(1, "Extract from COMSOL (--comsol)", comsol_var, 'comsol', 'checking server availability...')
    add_row(2, "Import into OriginLab (--origin)", origin_var, 'origin',
            'running' if origin_already_running() else 'not running')
    # The OriginLab LED only reflects an instant process check, so set it now.
    leds['origin'][0].itemconfig(
        leds['origin'][1], fill=('#2ecc40' if origin_already_running() else '#b0b0b0'))

    warn_label = ttk.Label(container, text="", foreground='#cc6600',
                            wraplength=340, justify='left')
    warn_label.grid(row=3, column=0, columnspan=3, sticky='w', pady=(8, 0))

    hint = ttk.Label(container, text="Checking COMSOL server availability...",
                      foreground='#888888')
    hint.grid(row=4, column=0, columnspan=3, sticky='w', pady=(4, 0))

    result = {'mode': None}

    # -- Real availability check: actually start a COMSOL server --
    # This is a real availability check (license seat, server reachable),
    # not just a process-name scan. If it succeeds and the user keeps
    # COMSOL selected, the started client is reused for extraction.
    # JPype's JVM has thread affinity, so this must run on the main thread -
    # it briefly blocks the window after it renders its "checking..." state.
    def run_comsol_check():
        try:
            client = mph.start()
            state['comsol_client'] = client
            state['comsol_status'] = 'available'
        except Exception as e:
            state['comsol_status'] = 'unavailable'
            state['comsol_error'] = str(e)

    # Recorded before run_comsol_check() so the warning below can tell
    # whether the availability check itself used an extra instance/seat.
    comsol_was_running = comsol_already_running()

    def update_warning():
        msgs = []
        if comsol_var.get() and comsol_was_running:
            msgs.append("A COMSOL process was already running - the "
                         "availability check used an additional engine "
                         "instance and license seat.")
        if origin_var.get() and not origin_already_running():
            msgs.append("OriginLab does not appear to be running - originpro "
                         "will try to launch it automatically.")
        warn_label.configure(text='\n'.join(msgs))

    def refresh_comsol_status():
        if state['comsol_status'] == 'available':
            leds['comsol'][0].itemconfig(leds['comsol'][1], fill='#2ecc40')
            status_labels['comsol'].configure(text='server available')
        else:
            leds['comsol'][0].itemconfig(leds['comsol'][1], fill='#cc4040')
            err = (state['comsol_error'] or 'unknown error')
            if len(err) > 60:
                err = err[:57] + '...'
            status_labels['comsol'].configure(text=f'unavailable: {err}')

        update_warning()
        hint.configure(text="Select at least one option.", foreground='#888888')
        ok_btn.configure(state='normal')

    def discard_comsol_client():
        # If the availability check started a COMSOL server but it ends up
        # unused (COMSOL not selected, or the dialog is cancelled), shut it
        # down again rather than leaving an orphaned session running.
        client = state.get('comsol_client')
        if client is not None:
            try:
                client.clear()
            except Exception:
                pass
            state['comsol_client'] = None

    def on_ok():
        if not comsol_var.get() and not origin_var.get():
            # Refuse to close with nothing selected - flag it instead.
            hint.configure(text="Select at least one option.", foreground='#cc0000')
            return
        if not comsol_var.get():
            discard_comsol_client()
        result['mode'] = {
            'comsol': comsol_var.get(),
            'origin': origin_var.get(),
            'comsol_client': state['comsol_client'],
        }
        root.destroy()

    def on_cancel():
        discard_comsol_client()
        result['mode'] = None
        root.destroy()

    btn_frame = ttk.Frame(container)
    btn_frame.grid(row=5, column=0, columnspan=3, pady=(15, 0), sticky='e')
    ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side='right')
    ok_btn = ttk.Button(btn_frame, text="OK", command=on_ok, state='disabled')
    ok_btn.pack(side='right', padx=(0, 5))

    root.protocol('WM_DELETE_WINDOW', on_cancel)

    # Render the "checking server availability..." state before running the
    # (blocking) COMSOL availability check, so the user sees it immediately.
    root.update_idletasks()
    root.update()
    run_comsol_check()
    refresh_comsol_status()

    root.mainloop()
    return result['mode']


# Display names for the groups shown in the item-picker, in the order shown.
ITEM_GROUP_LABELS = {
    'table': 'Tables',
    '1d': '1D Plots',
    '2d': '2D Plots',
    '3d': '3D Plots',
}


def pick_items_dialog(items: list[dict]) -> list[dict] | None:
    """Show a checklist of extractable tables/plot groups, grouped by type.

    Every item is checked by default. Returns the selected items, or None
    if the user cancelled (closed the window or clicked Cancel).
    """
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Select results to extract")
    root.attributes('-topmost', True)
    root.geometry('480x480')

    container = ttk.Frame(root, padding=10)
    container.pack(fill='both', expand=True)

    ttk.Label(container, text="Select which tables/plots to extract:").pack(anchor='w')

    # The list of items can be long, so put it in a scrollable canvas:
    # a Frame ('inner') holding all the checkboxes is placed inside a
    # Canvas, with a Scrollbar driving the canvas's view.
    list_frame = ttk.Frame(container)
    list_frame.pack(fill='both', expand=True, pady=(5, 0))

    canvas = tk.Canvas(list_frame, borderwidth=0, highlightthickness=0)
    scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=canvas.yview)
    inner = ttk.Frame(canvas)
    # Whenever 'inner' is resized (e.g. more checkboxes added), update the
    # canvas's scrollable region to match its full size.
    inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.create_window((0, 0), window=inner, anchor='nw')
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side='left', fill='both', expand=True)
    scrollbar.pack(side='right', fill='y')

    # Build one checkbox per item, grouped under bold section headings
    # (Tables / 1D Plots / 2D Plots / 3D Plots / Other) in the order items
    # appear. A new heading is only inserted when the group changes.
    variables = []
    last_group = None
    for item in items:
        group = item['kind'] if item['kind'] in ITEM_GROUP_LABELS else 'other'
        if group != last_group:
            ttk.Label(inner, text=ITEM_GROUP_LABELS.get(group, 'Other'),
                      font=('TkDefaultFont', 9, 'bold')).pack(
                anchor='w', pady=(8 if last_group else 0, 2))
            last_group = group
        # Checked by default - the user deselects what they don't want.
        var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inner, text=f"{item['tag']}: {item['label']}", variable=var).pack(
            anchor='w', padx=(10, 0))
        variables.append(var)

    # Mutable holder for the dialog's result, since the button callbacks
    # below can't return a value directly - they just close the window.
    result = {'items': None}

    def select_all():
        for v in variables:
            v.set(True)

    def deselect_all():
        for v in variables:
            v.set(False)

    def on_extract():
        # Keep only the items whose checkbox is still ticked, preserving
        # the original (grouped) order.
        result['items'] = [item for item, v in zip(items, variables) if v.get()]
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

    # Treat closing the window (the 'X' button) the same as Cancel.
    root.protocol('WM_DELETE_WINDOW', on_cancel)
    root.mainloop()
    return result['items']


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

    # -- Decide which steps to run --
    # If neither --comsol nor --origin was given, ask via a single combined
    # GUI that also runs the COMSOL/OriginLab availability checks
    # interactively (e.g. so the user can pick Origin-only if COMSOL's
    # license is busy elsewhere). comsol_client is a COMSOL server already
    # started by that dialog's background check, ready to be reused -
    # or None if the dialog was skipped (CLI flags given) or the check
    # didn't succeed/wasn't needed.
    mode = None
    comsol_client = None
    if args.comsol or args.origin:
        do_comsol, do_origin = args.comsol, args.origin
    else:
        mode = pick_mode_dialog()
        if mode is None:
            sys.exit("Nothing selected. Exiting.")
        do_comsol, do_origin = mode['comsol'], mode['origin']
        comsol_client = mode.get('comsol_client')

    if not do_comsol:
        # -- OriginLab-only mode: import a previously extracted folder --
        # (do_origin is guaranteed True here - the dialog/CLI logic above
        # requires at least one of the two steps.)
        # When mode is set, the combined dialog already checked and showed
        # OriginLab's running status inline - only prompt here for the
        # CLI-flags path, which skips that dialog entirely.
        if mode is None and not origin_already_running():
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
        print("Done.")
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

    # -- Pre-flight checks --
    # When mode is set, the combined dialog already checked and showed these
    # statuses inline (and, for COMSOL, started a server to test it) - only
    # prompt here for the CLI-flags path, which skips that dialog entirely.
    if mode is None:
        if comsol_already_running():
            confirm_or_exit(
                "WARNING: A COMSOL process is already running. Starting this "
                "stand-alone session will launch an additional COMSOL engine "
                "instance, using extra memory and a separate license seat.\n"
                "Close the existing COMSOL session first if you want to avoid that."
            )

        if do_origin and not origin_already_running():
            confirm_or_exit(
                "NOTE: OriginLab does not appear to be running.\n"
                "Start OriginLab now so --origin can connect to it (originpro "
                "may otherwise fail to launch it automatically)."
            )

    # -- Start COMSOL server and load model --
    # Reuse the server the mode dialog already started for its availability
    # check, if any, rather than launching a second one.
    if comsol_client is not None:
        client = comsol_client
        print("Using COMSOL server started during the availability check.")
    else:
        print(f"Starting COMSOL server...")
        client = mph.start()
    print(f"Loading model: {model_path.name}")
    model = client.load(str(model_path))
    print(f"Model loaded.\n")

    # Top-level summary written to manifest.json at the end. Each list
    # holds one dict per successfully extracted item (tag, label, output
    # filename, row/column counts, and any comments).
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

    # List every result table tag (e.g. 'tbl1', 'tbl2', ...).
    try:
        tbl_tags = [str(t) for t in java_result.table().tags()]
    except Exception as e:
        print(f"  [!] Could not list result tables: {e}")
        tbl_tags = []

    # List every top-level result node tag - this includes plot groups
    # ('pg1', 'pg2', ...) as well as other result-tree entries.
    try:
        pg_tags = [str(t) for t in java_result.tags()]
    except Exception as e:
        print(f"  [!] Could not list plot groups: {e}")
        pg_tags = []

    # Build a flat list of everything the user can choose to extract,
    # tagged with its 'kind' (table/1d/2d/3d/unknown) for grouping in the
    # checklist dialog and for picking the right extraction routine later.
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

    # -- Let the user pick which results to extract --
    print(f"Found {len(items)} extractable item(s). Opening selection window...")
    selected = pick_items_dialog(items)
    if not selected:
        print("Nothing selected. Exiting.")
        client.clear()
        return

    # ---- Extract selected items ----
    output_dir.mkdir(parents=True, exist_ok=True)
    for item in selected:
        tag, label, kind = item['tag'], item['label'], item['kind']

        if kind == 'table':
            # Result tables: extract via the table API and write
            # 'table_<tag>_<label>.csv'.
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

            # Prepend any user-entered "Comments" on the plot group node.
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
                # Unrecognized plot dimension - still record it, but under
                # 'other' with its Java class name for diagnostics.
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

    # -- Clean up --
    client.clear()
    print("Done.")


if __name__ == '__main__':
    main()