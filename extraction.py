"""
COMSOL-side data extraction: result tables via the table API, plot groups
via COMSOL's built-in "Plot" text export, plus the CSV format used to save
and re-load extracted datasets.
"""

import re
import csv
import json
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import psutil
except ImportError:
    psutil = None

LEGEND_LABELS_COMMENT = "Legend labels:"


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


def legend_label_from_column(label: str) -> str:
    """Return the curve legend text encoded in a COMSOL-style column header.

    COMSOL multi-curve exports commonly use headers such as
    ``Iout (mA), V_dc=1 V`` - or, for nested sweeps,
    ``Iout (mA), gap=2 um, ring=1.3 um``. The measured quantity and unit
    belong on the worksheet/axis; everything after the quantity is the
    legend entry, keeping *all* sweep-parameter values (dropping any of
    them would make same-valued inner-sweep curves indistinguishable).
    """
    name, _unit = split_label_unit(label)
    parts = [part.strip() for part in name.split(',') if part.strip()]
    # The legend is the sweep-parameter list ('gap=2 um, ring=1.3 um'); the
    # measured quantity itself is dropped - it's the same for every curve
    # and already lives in the axis title / column comments. Legend-derived
    # series names consist of parameter parts only, so filter by '=' rather
    # than assuming the first part is the quantity.
    params = [part for part in parts if '=' in part]
    if params:
        return ', '.join(params)
    return parts[-1] if len(parts) > 1 else name


def legend_labels_comment(labels: list[str]) -> str:
    """Encode legend labels into a CSV comment line."""
    return f"{LEGEND_LABELS_COMMENT} {json.dumps(labels, ensure_ascii=False)}"


def extract_legend_labels_from_comments(comments: list[str]) -> tuple[list[str], list[str]]:
    """Return (legend labels, comments without the internal legend line)."""
    labels = []
    kept = []
    for comment in comments:
        if comment.startswith(LEGEND_LABELS_COMMENT):
            raw = comment[len(LEGEND_LABELS_COMMENT):].strip()
            try:
                values = json.loads(raw)
            except json.JSONDecodeError:
                values = []
            if isinstance(values, list):
                labels = [str(value) for value in values if str(value).strip()]
            continue
        kept.append(comment)
    return labels, kept


def set_legend_labels(df: pd.DataFrame, labels: list[str] | None):
    """Attach non-empty legend labels to a DataFrame."""
    clean = [str(label).strip() for label in labels or [] if str(label).strip()]
    if clean:
        df.attrs['legend_labels'] = clean


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
        if df.attrs.get('legend_labels'):
            f.write(f"% {legend_labels_comment(df.attrs['legend_labels'])}\n")
        writer = csv.writer(f)
        writer.writerow(names)
        writer.writerow(units)
        for row in df.itertuples(index=False, name=None):
            writer.writerow(row)


def load_dataset_csv(path: Path, low_memory: bool = False) -> tuple[pd.DataFrame, list[str]]:
    """Read a CSV written by write_csv_with_comments() back into a
    (DataFrame, comments) pair, recombining the two header rows into
    'Name (unit)' columns - used to re-import a '<model>_results' folder into
    OriginLab without COMSOL running again. Only the leading comment/header
    lines are read up front (not the whole file - see read_leading_comments)
    before the data itself is parsed directly from disk. low_memory parses
    into float32 instead of float64 (see parse_comsol_export).
    """
    comments = []
    n_comment_lines = 0
    with open(path, 'r', encoding='utf-8', newline='') as f:
        for line in f:
            if not line.startswith('%'):
                names = next(csv.reader([line]))
                units = next(csv.reader([f.readline()]))
                break
            comments.append(line[1:].strip())
            n_comment_lines += 1

    labels, comments = extract_legend_labels_from_comments(comments)
    headers = [f"{name} ({unit})" if unit else name for name, unit in zip(names, units)]

    df = pd.read_csv(path, skiprows=n_comment_lines + 2, header=None, names=headers)
    if low_memory:
        float_cols = df.select_dtypes('float64').columns
        if len(float_cols):
            df[float_cols] = df[float_cols].astype('float32')
    set_legend_labels(df, labels)
    return df, comments


def is_monotonic_series(values: pd.Series) -> bool:
    """Return True when values are consistently nondecreasing/nonincreasing."""
    clean = pd.to_numeric(values, errors='coerce').dropna()
    if len(clean) < 3:
        return True
    diffs = clean.diff().dropna()
    diffs = diffs[diffs != 0]
    if diffs.empty:
        return True
    return bool((diffs >= 0).all() or (diffs <= 0).all())


def split_line_segments(df: pd.DataFrame, expected: int | None = None) -> list[pd.DataFrame]:
    """Split a two-column line export into repeated x-sweeps.

    COMSOL sometimes exports parametric line plots as one long x/y pair:
    sweep 1, then sweep 2, then sweep 3, and so on. Plotting that directly
    connects the last point of one sweep to the first point of the next,
    creating the visible zigzag. Break the data where the x direction
    reverses or jumps back to the start of the next sweep.

    expected is the curve count COMSOL's own legend announces, when known.
    Sweeps covering disjoint x windows (e.g. one narrow band around each
    parameter combination's resonance) leave no reversal at their boundary
    when the next window starts above the previous one's end - the largest
    forward x jumps are added as boundaries until the expected count is
    reached.
    """
    num = df.select_dtypes('number')
    if num.shape[1] != 2 or len(num) < 4 or expected == 1:
        return [df]

    x = num.iloc[:, 0].reset_index(drop=True)
    diffs = x.diff()
    nonzero = diffs[diffs != 0].dropna()
    if nonzero.empty:
        return [df]

    direction = 1 if (nonzero > 0).sum() >= (nonzero < 0).sum() else -1
    starts = {int(idx) for idx, diff in diffs.items()
              if idx != 0 and not pd.isna(diff) and diff != 0 and diff * direction < 0}

    if expected is not None and len(starts) < expected - 1:
        forward = diffs.iloc[1:]
        forward = forward[(forward * direction > 0) & ~forward.index.isin(starts)]
        extra = forward.abs().nlargest(expected - 1 - len(starts))
        starts |= {int(idx) for idx in extra.index}

    breaks = [0] + sorted(starts) + [len(df)]
    segments = [df.iloc[start:end].reset_index(drop=True)
                for start, end in zip(breaks, breaks[1:]) if end > start]
    if expected is not None and len(segments) == expected:
        return segments  # matches COMSOL's own curve count - trust it
    # A genuine concatenated sweep splits into a few long monotonic runs.
    # Scattered x values (e.g. a per-parameter extrema table, one resonance
    # frequency per parameter combination) reverse direction constantly and
    # would shatter into many 1-2 point "sweeps" - keep those as one series.
    # ponytail: average run length >= 3 as the sweep/scatter cutoff; make it
    # smarter (e.g. repeated identical x-starts) if a real sweep ever trips it.
    if segments and len(df) / len(segments) < 3:
        return [df]
    return segments or [df]


def line_series_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Return a wide line-series frame suitable for plotting/export.

    Existing wide data is kept as-is. Long data with explicit x/y/group
    columns is pivoted by group. Two-column concatenated sweeps are split
    into one y column per sweep, preventing cross-sweep connector lines.
    """
    if 'group' in df.columns and {'x', 'y'}.issubset(df.columns):
        wide = df.pivot_table(index='x', columns='group', values='y', sort=False).reset_index()
        wide.columns = ['x'] + [str(c) for c in wide.columns[1:]]
        return wide

    num = df.select_dtypes('number')
    if num.shape[1] != 2:
        return df

    labels = df.attrs.get('legend_labels') or []
    segments = split_line_segments(num, expected=len(labels) or None)
    if len(segments) <= 1:
        return df

    x_col, y_col = num.columns[:2]
    y_name, y_unit = split_label_unit(y_col)
    wide = None
    for index, segment in enumerate(segments, start=1):
        part = segment[[x_col, y_col]].copy()
        series_name = labels[index - 1] if index <= len(labels) else f"{y_name} {index}"
        if y_unit:
            series_name = f"{series_name} ({y_unit})"
        part.columns = [x_col, series_name]
        wide = part if wide is None else wide.merge(part, on=x_col, how='outer')
    return wide.sort_values(by=x_col).reset_index(drop=True)


def line_markers(df: pd.DataFrame) -> list[dict]:
    """Return peak markers for each y series in a line-series DataFrame."""
    num = df.select_dtypes('number')
    if num.shape[1] < 2:
        return []
    x_col = num.columns[0]
    markers = []
    for y_col in num.columns[1:]:
        series = num[y_col].dropna()
        if series.empty:
            continue
        idx = series.idxmax()
        markers.append({
            'x_col': x_col,
            'y_col': y_col,
            'x': float(num.loc[idx, x_col]),
            'y': float(num.loc[idx, y_col]),
        })
    return markers


# Preview plots stay responsive/within memory above this row count - large
# point counts are what make matplotlib's triangulated surfaces slow or
# crash-prone, independent of how much memory parsing itself used.
PREVIEW_MAX_PLOT_POINTS = 50_000


def subsample_for_plot(df: pd.DataFrame, max_points: int = PREVIEW_MAX_PLOT_POINTS) -> pd.DataFrame:
    """Return a systematic (every-Nth-row) subsample of df for plotting.

    Preserves row order (so monotonic sweeps/segment-splitting logic stays
    correct) and keeps all columns row-aligned (so a deformed/undeformed
    coordinate pair - see merge_undeformed_reference() - samples together).
    df itself, and the caller's other uses of it (e.g. the Data tab), are
    unaffected - this returns a new, independent DataFrame.
    """
    if len(df) <= max_points:
        return df
    stride = max(1, len(df) // max_points)
    return df.iloc[::stride]


def surface_columns(df: pd.DataFrame, kind: str) -> tuple[str, str, str, str | None] | None:
    """Pick coordinate/value columns for 2D/3D surface previews.

    COMSOL plot exports normally write deformed plot coordinates when a
    Deformation feature is active, so using the exported coordinate columns
    preserves the displayed shape without recomputing displacement here.
    """
    num = df.select_dtypes('number')
    if kind == '2d' and num.shape[1] >= 3:
        return num.columns[0], num.columns[1], num.columns[2], None
    if kind == '3d' and num.shape[1] >= 3:
        value_col = num.columns[3] if num.shape[1] > 3 else num.columns[2]
        return num.columns[0], num.columns[1], num.columns[2], value_col
    return None


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


def discover_items(model) -> list[dict]:
    """Discover extractable result tables and plot groups in a loaded model.

    Returns a flat list of dicts tagged with 'kind' (table/1d/2d/3d/unknown)
    for grouping in the checklist and for picking the right extraction
    routine later.
    """
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

    return items


def extract_table(model, tag: str, low_memory: bool = False) -> tuple[pd.DataFrame, list[str]] | None:
    """Extract a COMSOL result table into a DataFrame, plus any user comments.

    Column headers (with units, e.g. 'freq (GHz)') come from the table's
    'headers' property - a [index, description] matrix - while the data
    comes from getTableData(True) as strings (COMSOL 6.4 dropped the no-arg
    getTableData()/getColumnHeader()/getDoubleValue() API). low_memory
    parses into float32 instead of float64, halving the table's memory for
    a precision loss well below COMSOL's own exported significant digits.
    """
    try:
        tbl = model.java.result().table(tag)
        headers = [str(row[1]) for row in tbl.getStringMatrix('headers')]

        # Each cell is a Java String (e.g. "3.4065" or "inf"/"NaN"); convert
        # via str() first - Python's float() accepts "inf"/"NaN" natively.
        # Filled into a preallocated array directly rather than building a
        # nested Python list first, which for a large table would otherwise
        # briefly hold both the list-of-floats and the final array at once.
        rows = tbl.getTableData(True)
        dtype = np.float32 if low_memory else np.float64
        data = np.empty((len(rows), len(headers)), dtype=dtype)
        for i, row in enumerate(rows):
            for j, cell in enumerate(row):
                data[i, j] = float(str(cell))

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

def export_via_comsol(model, pg_tag: str, output_dir: Path, suffix: str = '') -> Path | None:
    """Use COMSOL's native 'Plot' export to dump a plot group to a text file,
    falling back to the system temp directory if COMSOL refuses to write
    into output_dir (locking, permissions, ...). suffix distinguishes a
    repeated export of the same plot group (e.g. a deformation-off
    reference pass, see export_undeformed_reference()) so the temp export
    node/file don't collide with a still-pending export of the same tag."""
    fname = sanitize_filename(pg_tag) + suffix + '_export.txt'
    # Temporary export-node tag; created and removed within this function so
    # repeated calls don't accumulate leftover export nodes in the model.
    export_tag = f'exp_{pg_tag}{suffix}'

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


# ---------------------------------------------------------------------------
# Deformation - 2D/3D plots often carry a Deformation sub-feature (nested
# under Surface/Arrow/...) that displaces the plotted geometry for display.
# COMSOL's Plot export already bakes this in at whatever scale the feature
# is configured with; a second export with it toggled off recovers the
# undeformed reference geometry, letting the preview exaggerate the
# deformation by an arbitrary factor instead of only COMSOL's own scale.
# ---------------------------------------------------------------------------

# Above this primary-export size, skip the second (undeformed-reference)
# export pass rather than roughly doubling peak memory for an already large
# plot - see extract_via_export().
DEFORMATION_REFERENCE_MAX_BYTES = 150 * 1024 * 1024

def find_deformation_feature(pg):
    """Locate a plot group's Deformation sub-feature (e.g. under a Surface
    or Arrow plot), if any. COMSOL auto-tags these with a 'defm' prefix;
    fall back to the feature type string for other naming/versions."""
    def is_deformation(tag: str, feat) -> bool:
        if tag.lower().startswith('defm'):
            return True
        try:
            return 'deform' in str(feat.getType()).lower()
        except Exception:
            return False

    def visit(container):
        try:
            tags = [str(t) for t in container.feature().tags()]
        except Exception:
            return None
        children = [(tag, container.feature(tag)) for tag in tags]
        for tag, feat in children:
            if is_deformation(tag, feat):
                return feat
        for _, feat in children:
            found = visit(feat)
            if found is not None:
                return found
        return None

    try:
        return visit(pg)
    except Exception:
        return None


def get_deformation_scale_info(feat) -> dict:
    """Best-effort read of a Deformation feature's active/scale settings,
    for documentation in the exported '%' comments."""
    info = {'active': True, 'scale': None, 'auto_scale': None}
    try:
        info['active'] = bool(feat.isActive())
    except Exception:
        pass
    try:
        info['scale'] = float(feat.getDouble('scale'))
    except Exception:
        pass
    try:
        info['auto_scale'] = str(feat.getString('scaleactive')).lower() != 'manual'
    except Exception:
        pass
    return info


def deformation_comment(info: dict) -> str:
    """Format deformation metadata for a CSV '%' comment line."""
    if info.get('scale') is not None:
        mode = 'auto' if info.get('auto_scale') else 'manual'
        return f"Deformation: scale={info['scale']:.4g} ({mode})"
    return "Deformation: active"


def export_undeformed_reference(model, pg, pg_tag: str, deform_feat,
                                 units_map: dict[str, str], coordinate_unit: str,
                                 low_memory: bool = False) -> pd.DataFrame | None:
    """Re-export a plot group with its Deformation feature temporarily
    disabled, returning just the resulting (undeformed) coordinate
    DataFrame. Always restores the feature's active state, even on failure.
    Exports to the system temp directory only - this is an internal
    computation aid, not part of the saved output."""
    try:
        deform_feat.active(False)
    except Exception:
        return None
    try:
        export_path = export_via_comsol(model, pg_tag, Path(tempfile.gettempdir()),
                                        suffix='_undeformed')
        if export_path is None:
            return None
        try:
            result = parse_comsol_export(export_path, units_map, coordinate_unit,
                                         low_memory=low_memory)
        finally:
            try:
                export_path.unlink(missing_ok=True)
            except Exception:
                pass
        return result[0] if result else None
    except Exception:
        return None
    finally:
        try:
            deform_feat.active(True)
        except Exception:
            pass


def merge_undeformed_reference(df: pd.DataFrame, kind: str, undeformed_df: pd.DataFrame) -> pd.DataFrame:
    """Append undeformed reference coordinate columns ('Undeformed <col>'),
    positionally aligned with df's own coordinate columns, for use by the
    preview's exaggeration slider (see exaggerate_coordinates()). df is
    returned unchanged if the two exports don't line up (different row/
    column counts - e.g. a COMSOL version where deformation.active() didn't
    actually change the export)."""
    n_coord = 3 if kind == '3d' else 2
    if len(undeformed_df) != len(df) or undeformed_df.shape[1] < n_coord or df.shape[1] < n_coord:
        return df
    df = df.copy()
    for i in range(n_coord):
        df[f"Undeformed {undeformed_df.columns[i]}"] = undeformed_df.iloc[:, i].to_numpy()
    return df


def deformation_reference_columns(df: pd.DataFrame, kind: str) -> list[str] | None:
    """Return the 'Undeformed <col>' reference columns added by
    merge_undeformed_reference(), matching surface_columns()'s coordinate
    count/order for the given plot kind, or None if not present."""
    n_coord = 3 if kind == '3d' else 2
    cols = [c for c in df.columns if str(c).startswith('Undeformed ')]
    return cols[:n_coord] if len(cols) >= n_coord else None


def exaggerate_coordinates(deformed: np.ndarray, undeformed: np.ndarray, factor: float) -> np.ndarray:
    """Blend/extrapolate between undeformed reference geometry and COMSOL's
    own deformed coordinates by factor: 1.0 reproduces COMSOL's configured
    scale exactly, 0.0 is the undeformed geometry, and values beyond 1.0
    exaggerate the displacement further."""
    return undeformed + factor * (deformed - undeformed)


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


def get_plot_legend_labels(pg) -> list[str]:
    """Best-effort read of explicit legend labels from a COMSOL plot group.

    COMSOL stores legend text on different properties depending on plot
    feature type/version. Walk plot features recursively and keep plausible
    string-array properties whose names mention legend.
    """
    labels = []

    def plausible(values: list[str]) -> bool:
        clean = [value.strip() for value in values if value.strip()]
        if len(clean) < 2:
            return False
        lowered = {value.lower() for value in clean}
        return not lowered <= {'on', 'off', 'auto', 'manual', 'true', 'false'}

    def visit(feat):
        nonlocal labels
        if labels:
            return
        try:
            names = {str(n) for n in feat.properties()}
        except Exception:
            names = set()

        for prop in names:
            if 'legend' not in prop.lower():
                continue
            try:
                value_type = str(feat.getValueType(prop))
            except Exception:
                continue
            try:
                if value_type == 'StringArray':
                    values = [str(value).strip() for value in feat.getStringArray(prop)]
                elif value_type == 'String':
                    raw = str(feat.getString(prop)).strip()
                    values = re.split(r'\s*[,;]\s*', raw) if raw else []
                else:
                    continue
            except Exception:
                continue
            if plausible(values):
                labels = [value for value in values if value.strip()]
                return

        try:
            for ctag in feat.feature().tags():
                visit(feat.feature(str(ctag)))
        except Exception:
            pass

    try:
        for ftag in pg.feature().tags():
            visit(pg.feature(str(ftag)))
            if labels:
                break
    except Exception:
        pass
    return labels


def read_leading_comments(path: Path) -> list[str]:
    """Read just the leading '%'-prefixed comment/header lines from a COMSOL
    text export, stopping at the first data line.

    COMSOL's comments are always a contiguous block at the top of the file
    (see parse_comsol_export), so this never needs to hold the file's data
    rows in memory - unlike loading the whole file as one string just to
    filter it, which peaks at several times the file size for a large mesh
    export and is a common cause of out-of-memory crashes on big models.
    """
    comments = []
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = repair_mojibake(line.rstrip('\r\n'))
            if not line.startswith('%'):
                break
            comments.append(line[1:].strip())
    return comments


def parse_comsol_export(path: Path, units_map: dict[str, str] | None = None,
                         coordinate_unit: str = '',
                         fallback_headers: list[str] | None = None,
                         low_memory: bool = False) -> tuple[pd.DataFrame, list[str]] | None:
    """Parse a COMSOL text export into (DataFrame, metadata comment lines).

    COMSOL prefixes the file with '%' lines holding metadata (Model, Version,
    Date, Description, ...). The last '%' line normally holds the column
    headers, separated by runs of 2+ spaces (split_header_line).

    units_map (see get_feature_units) fills in units that aren't embedded in
    the header text; coordinate_unit (see get_geometry_length_unit) fills in
    spatial-coordinate columns (COORDINATE_NAMES). If the export has no
    usable header line, fallback_headers (see get_table_graph_headers) is
    used if its length matches the data's column count. low_memory downcasts
    the parsed float64 columns to float32 - roughly halving the DataFrame's
    memory for a precision loss well below COMSOL's own exported digits.
    """
    comment_lines = read_leading_comments(path)

    try:
        df = pd.read_csv(path, comment='%', sep=r'\s+', header=None)
    except Exception:
        return None
    if df.empty:
        return None
    if low_memory:
        float_cols = df.select_dtypes('float64').columns
        if len(float_cols):
            df[float_cols] = df[float_cols].astype('float32')

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


def extract_via_export(model, pg, pg_tag: str, output_dir: Path,
                        kind: str = '', low_memory: bool = False) -> tuple[pd.DataFrame, list[str]] | None:
    """Extract a plot group's data via COMSOL's native text export into a DataFrame.

    For 2D/3D plot groups (kind) with an active Deformation sub-feature, a
    second export with the deformation temporarily disabled adds undeformed
    reference coordinate columns alongside the already-deformed ones (see
    merge_undeformed_reference()), and the feature's scale is recorded as a
    '%' comment - together letting the preview show the geometry at an
    exaggeration factor independent of COMSOL's own configured scale.
    low_memory parses into float32 instead of float64 (see
    parse_comsol_export).
    """
    export_path = export_via_comsol(model, pg_tag, output_dir)
    if export_path is None:
        return None

    try:
        units_map = get_feature_units(pg)
        coordinate_unit = get_geometry_length_unit(model, pg)
        result = parse_comsol_export(export_path, units_map, coordinate_unit,
                                     get_table_graph_headers(model, pg), low_memory=low_memory)
        if result is None:
            return None
        df, comments = result
        labels = get_plot_legend_labels(pg)
        segments = split_line_segments(df, expected=len(labels) or None)
        if len(labels) == len(segments):
            set_legend_labels(df, labels)

        if kind in ('2d', '3d'):
            deform_feat = find_deformation_feature(pg)
            if deform_feat is not None:
                info = get_deformation_scale_info(deform_feat)
                if info.get('active', True):
                    comments = comments + [deformation_comment(info)]
                    try:
                        export_too_large = export_path.stat().st_size > DEFORMATION_REFERENCE_MAX_BYTES
                    except OSError:
                        export_too_large = False
                    if export_too_large:
                        # Skip the second (undeformed-reference) export pass
                        # for very large plots - it would roughly double an
                        # already large peak memory footprint. The primary
                        # (deformed) data is unaffected either way.
                        comments = comments + [
                            "Deformation: undeformed reference skipped "
                            f"(export exceeds {DEFORMATION_REFERENCE_MAX_BYTES // (1024 * 1024)} MB)"
                        ]
                    else:
                        undeformed_df = export_undeformed_reference(
                            model, pg, pg_tag, deform_feat, units_map, coordinate_unit,
                            low_memory=low_memory)
                        if undeformed_df is not None:
                            df = merge_undeformed_reference(df, kind, undeformed_df)

        return df, comments
    except Exception as e:
        print(f"  [!] Could not parse export for '{pg_tag}': {e}")
        return None
