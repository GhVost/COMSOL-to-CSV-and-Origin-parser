"""
OriginLab integration: pushing extracted datasets into an .opju project via
originpro (COM automation), plus the Origin process management around it.
"""

import os
import sys
import json
import shutil
import tempfile
import time
import traceback
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

from extraction import (
    legend_label_from_column, line_series_dataframe, load_dataset_csv,
    split_label_unit,
)


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


def load_datasets_from_folder(folder: Path, low_memory: bool = False) -> list[dict]:
    """Load CSVs from a previously written '<model>_results' folder for
    direct OriginLab import via push_to_origin(), without re-running the
    COMSOL extraction (e.g. when COMSOL isn't installed/licensed here, or its
    license is busy elsewhere).

    Uses manifest.json to find each extracted file and which section it
    belongs to, then reads it back with load_dataset_csv(). low_memory
    parses into float32 instead of float64 (see parse_comsol_export).
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
            df, comments = load_dataset_csv(path, low_memory=low_memory)
            datasets.append({'name': Path(fname).stem, 'kind': kind, 'df': df,
                              'comments': comments or entry.get('comments', [])})
            print(f"  - Loaded {fname}  ({len(df)} rows x {len(df.columns)} cols)")

    return datasets


def push_to_origin(datasets: list, output_dir: Path, template: str = '') -> Path | None:
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
        return None

    if not datasets:
        print("No data to import into Origin.")
        return None

    saved_path = None
    previous_sdo = None
    staging_dir = None
    origin_closed = False
    try:
        for entry in datasets:
            name, kind, df = entry['name'], entry['kind'], entry['df']
            comments = entry.get('comments') or []

            # Parametric line sweeps can arrive as one stitched x/y curve.
            # Convert them to wide series before writing to Origin so the
            # graph gets independent curves, a real legend, and no connector
            # line between parameter values. The measured quantity from the
            # original 2-column header becomes the graph's y-axis title -
            # the per-curve long names hold only the sweep parameters.
            y_title = ''
            if kind in ('table', '1d'):
                wide = line_series_dataframe(df)
                if len(wide.columns) > len(df.columns) and len(df.columns) == 2:
                    quantity, unit = split_label_unit(df.columns[1])
                    quantity = quantity.split(',')[0].strip()
                    y_title = f"{quantity} ({unit})" if unit else quantity
                df = wide

            wb = op.new_book('w', name)
            sheet = wb[0]
            sheet.from_df(df)
            if len(df.columns) >= 2:
                # One x column, every other column a y series - not the
                # alternating X,Y,X,Y pattern 'xy'+repeat would produce.
                sheet.cols_axis('x' + 'y' * (len(df.columns) - 1), repeat=False)

            # Carry column names/units (from 'Name (unit)' headers) over to
            # Origin's long name / units label rows. Curve columns get the
            # full sweep-parameter list as their long name (that's what the
            # legend shows), with the complete original header - quantity
            # included - preserved in the column's Comments row.
            for i, col in enumerate(df.columns):
                full, unit = split_label_unit(col)
                label = full
                if i > 0 and kind in ('table', '1d'):
                    label = legend_label_from_column(col)
                sheet.set_label(i, label, type='L')
                if unit:
                    sheet.set_label(i, unit, type='U')
                if label != full:
                    sheet.set_label(i, full, type='C')

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
                    layer.add_plot(sheet, coly=i, colx=0, type='l')
                # Group and legend separately - a failed group() must not
                # skip the legend rebuild, or Origin keeps its default
                # single-entry legend.
                try:
                    layer.group(True)
                except Exception:
                    pass
                try:
                    layer.LT_execute('legend -r')
                except Exception:
                    pass
                if y_title:
                    try:
                        layer.LT_execute(f'yl.text$ = "{y_title}"')
                    except Exception:
                        pass
                layer.rescale()
                graph.lname = name

            # Origin's worksheet now holds this dataset's data; drop the
            # Python-side copy so a multi-item batch doesn't keep every
            # already-imported DataFrame alive until the whole push finishes.
            entry['df'] = None
            del df

            print(f"  -> Imported: {name}")

        output_dir.mkdir(parents=True, exist_ok=True)
        opju_path = (output_dir / 'comsol_results.opju').resolve()
        origin_save_path = opju_path

        # Origin's COM save API still fails at the legacy Windows MAX_PATH
        # boundary even when Python itself can access the long destination.
        # Save under a short temporary path, then move the completed file to
        # the requested results directory using Python's long-path-capable
        # filesystem API.
        if len(str(opju_path)) >= 248:
            staging_dir = Path(tempfile.mkdtemp(prefix='comsol_origin_'))
            origin_save_path = staging_dir / 'comsol_results.opju'

        # Origin's external-Python API returns False when saving fails; it
        # does not necessarily raise an exception.  @SDO allows Origin 2022+
        # to save when an invisible/minimized dialog would otherwise block
        # automation.  Restore the user's setting immediately afterwards.
        try:
            previous_sdo = op.lt_float('@SDO')
            op.set_lt_var('@SDO', 1)
        except Exception:
            previous_sdo = None

        save_ok = op.save(os.path.abspath(str(origin_save_path)))
        if not save_ok:
            raise RuntimeError(
                f"Origin rejected the save request for '{origin_save_path}'. "
                "Check for an open dialog, a read-only existing file, or "
                "insufficient write permission."
            )

        # Save is normally synchronous, but give the external COM server a
        # short window to finish publishing the file before disconnecting.
        for _ in range(50):
            if origin_save_path.is_file() and origin_save_path.stat().st_size > 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(
                f"Origin reported a successful save, but no non-empty project "
                f"file appeared at '{origin_save_path}'."
            )

        if origin_save_path != opju_path:
            # Origin holds the current project file open while its automation
            # session is alive. Close that session before moving the staged
            # file, then retry briefly while Windows releases the handle.
            if previous_sdo is not None:
                try:
                    op.set_lt_var('@SDO', previous_sdo)
                except Exception:
                    pass
                previous_sdo = None
            op.exit()
            origin_closed = True

            for attempt in range(50):
                try:
                    shutil.move(str(origin_save_path), str(opju_path))
                    break
                except PermissionError:
                    if attempt == 49:
                        raise
                    time.sleep(0.1)

            if not opju_path.is_file() or opju_path.stat().st_size == 0:
                raise RuntimeError(
                    f"Origin saved the staged project, but it could not be "
                    f"moved to '{opju_path}'."
                )

        saved_path = opju_path
        print(f"\nOrigin project saved: {opju_path}")
    except Exception:
        # Don't let a COM/Origin-side failure take down the whole script
        # after extraction has already succeeded.
        print("\n[!] Failed to push results to OriginLab:")
        traceback.print_exc()
    finally:
        if previous_sdo is not None:
            try:
                op.set_lt_var('@SDO', previous_sdo)
            except Exception:
                pass

        # On success, close the automation-owned Origin instance. On failure,
        # leave Origin visible with the imported data intact so the user can
        # inspect the error or save manually instead of losing the project.
        try:
            if origin_closed:
                pass
            elif saved_path is not None:
                op.exit()
            else:
                op.set_show(True)
                op.detach()
        except Exception:
            pass
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)

    return saved_path
