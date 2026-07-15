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
    - pip install MPh pandas numpy PySide6 matplotlib
    - pip install originpro and OriginLab installed (only for --origin)

Usage:
    python COMSOLExtractor.py --origin

Shows a combined window: an Open button picks the .mph model, every
table/plot group can be checked for extraction (clicking an item opens a
preview tab), and OriginLab's status is displayed alongside a FlexNet
license-usage report. A model path, --output <dir> and
--origin-template <file> can also be given; see --help.

Output is saved to a folder named <model_name>_results/ next to the .mph file.

Code layout:
    COMSOLExtractor.py - command line, extraction workflow (this file)
    extraction.py      - COMSOL data extraction and the CSV format
    origin_push.py     - OriginLab (.opju) integration via originpro
    license_check.py   - FlexNet (FNL) license-usage query and report
    gui.py             - PySide6 window, dialogs, and preview widgets
"""

__version__ = '1.11.0'

import argparse
import os
import sys
import json
import time
import traceback
from pathlib import Path
from datetime import datetime

from extraction import (
    comsol_already_running, extract_table, extract_via_export,
    sanitize_filename, write_csv_with_comments,
)
from origin_push import (
    close_new_origin_processes, get_origin_pids, load_datasets_from_folder,
    origin_already_running, push_to_origin,
)
from gui import pick_folder_dialog, run_extraction_window


def confirm_or_exit(message: str):
    """Print a message and wait for the user to press Enter (or Ctrl+C to abort)."""
    print(message)
    try:
        input("Press Enter to continue, or Ctrl+C to abort... ")
    except KeyboardInterrupt:
        sys.exit("\nAborted by user.")


def pause_if_frozen():
    """When running as a bundled .exe, wait for Enter before the console
    window closes so an error message stays readable. Used on failure paths
    only - a successful run exits (and thereby shuts down the in-process
    COMSOL engine, releasing its license seat) without needing a keypress."""
    if getattr(sys, 'frozen', False):
        try:
            input("\nPress Enter to exit... ")
        except EOFError:
            pass


def extract_selected(model, model_path: Path, selected: list, output_dir: Path,
                     low_memory: bool, collect_datasets: bool,
                     progress=None) -> tuple[dict, list | None]:
    """Extract the selected items into output_dir (CSV files + manifest.json).

    progress, if given, is called as progress(done, total, label, info) when
    each item starts, and once more with done == total at the end - the GUI
    uses it for its progress bar and remaining-time estimate. info carries
    work-weighted progress fractions (see progress_info() below): each
    item's duration is recorded in <output_dir>/.extract_timing.json, so a
    re-run of the same model weights each item by how long it actually took
    last time (implicitly, by its data size); the GUI then estimates the
    remaining time from this run's measured pace over the work fraction
    completed, which stays honest even when one item overruns its history.

    Returns (manifest, datasets): the manifest dict (also written to
    manifest.json) and, when collect_datasets is True, the list of
    {'name', 'kind', 'df', 'comments'} dicts for push_to_origin(), else None.
    """
    # Date stamp recorded with every dataset (CSV '%' comments, manifest,
    # and Origin worksheet comments), e.g. 'Extracted: 20260702'.
    date_stamp = f"Extracted: {datetime.now().strftime('%Y%m%d')}"

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
    # Collected for direct OriginLab export ({'name', 'kind', 'df', 'comments'}
    # per item) - only kept when actually needed, since holding every
    # extracted item's full DataFrame for the whole run (on top of the one
    # currently being extracted) is itself a common cause of running out of
    # memory on a large model with many selected items.
    datasets = [] if collect_datasets else None

    # Dataset/file names come from the human-readable COMSOL label ("Probe
    # Table 1", "S-parameter"), not the internal tag ("tbl1", "pg66"); the
    # tag is appended only to break a duplicate-label collision.
    used_names: set[str] = set()

    def dataset_name(label: str, tag: str) -> str:
        name = sanitize_filename(label) or tag
        if name.lower() in used_names:
            name = sanitize_filename(f"{label} ({tag})")
        used_names.add(name.lower())
        return name

    # Per-item duration history from previous runs of this model ('tag' ->
    # {'seconds': float, 'rows': int}) - a re-run predicts each remaining
    # item's time from how long that same item (at its recorded size) took
    # last time, so one huge half-million-row plot among small tables is
    # weighted accordingly instead of being averaged away.
    timing_path = output_dir / '.extract_timing.json'
    try:
        with open(timing_path, encoding='utf-8') as f:
            history = json.load(f)
        history = {tag: rec for tag, rec in history.items()
                   if isinstance(rec, dict) and isinstance(rec.get('seconds'), (int, float))}
    except Exception:
        history = {}
    durations: dict[str, float] = {}  # this run's actual seconds per tag

    def progress_info(next_index: int) -> dict:
        """Work-weighted progress for the GUI's remaining-time estimate.

        Each item's weight is its duration from a previous run (never-seen
        items get the average of the known weights, or 1 on a first run, so
        only the relative sizes matter). Returns
          frac_done - weight fraction of the items already finished,
          item_frac - weight fraction of the item about to start,
          item_pred - that item's expected wall seconds at this run's
                      measured pace (-1 when there is no basis yet).
        The GUI turns these into remaining = elapsed * (1 - p) / p.
        """
        weights = [history[s['tag']]['seconds'] if s['tag'] in history else None
                   for s in selected]
        known = [w for w in weights if w is not None]
        default = sum(known) / len(known) if known else 1.0
        weights = [w if w is not None else default for w in weights]
        total_w = sum(weights)
        if next_index >= len(selected) or total_w <= 0:
            return {'frac_done': 1.0, 'item_frac': 0.0, 'item_pred': 0.0}
        done_w = sum(weights[:next_index])
        act = sum(durations.get(s['tag'], 0.0) for s in selected[:next_index])
        if act > 0 and done_w > 0:
            scale = act / done_w  # this run's seconds per weight unit
        elif selected[next_index]['tag'] in history:
            scale = 1.0  # no pace measured yet; trust history as-is
        else:
            scale = -1.0  # first run, first item: no basis for a prediction
        return {'frac_done': done_w / total_w,
                'item_frac': weights[next_index] / total_w,
                'item_pred': weights[next_index] * scale if scale > 0 else -1.0}

    total = len(selected)
    for done, item in enumerate(selected):
        tag, label, kind = item['tag'], item['label'], item['kind']
        if progress is not None:
            progress(done, total, f"{tag} ({label})", progress_info(done))
        # perf_counter, not monotonic: the latter ticks in 15.6 ms steps on
        # Windows, recording sub-tick items as 0-second history entries.
        item_t0 = time.perf_counter()

        if kind == 'table':
            print(f"  - {tag} ({label})")
            result = extract_table(model, tag, low_memory=low_memory)
            if result is not None and not result[0].empty:
                df, comments = result
                comments = comments + [date_stamp]
                name = dataset_name(label, tag)
                fname = name + '.csv'
                write_csv_with_comments(df, output_dir / fname, comments)
                manifest['tables'].append({'tag': tag, 'label': label, 'file': fname,
                                           'rows': len(df), 'cols': list(df.columns),
                                           'comments': comments})
                if datasets is not None:
                    datasets.append({'name': name, 'kind': 'table', 'df': df, 'comments': comments})
                print(f"    -> Saved {fname}  ({len(df)} rows x {len(df.columns)} cols)")
            durations[tag] = time.perf_counter() - item_t0
            history[tag] = {'seconds': durations[tag],
                            'rows': len(result[0]) if result is not None else 0}
            continue

        # Everything else (1D/2D/3D plot groups, or anything else
        # get_plot_type() couldn't classify) goes through COMSOL's "Plot"
        # export and is written as '<label>.csv'.
        pg, class_name, ptype = item['pg'], item['class_name'], kind
        print(f"\n  [{ptype.upper():>7}] {tag} ({label})  [{class_name}]")

        result = extract_via_export(model, pg, tag, output_dir, kind=ptype, low_memory=low_memory)
        if result is not None and not result[0].empty:
            df, comments = result

            try:
                note = str(pg.comments())
                if note:
                    comments = [note] + comments
            except Exception:
                pass
            comments = comments + [date_stamp]

            name = dataset_name(label, tag)
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
            if datasets is not None:
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

        durations[tag] = time.perf_counter() - item_t0
        history[tag] = {'seconds': durations[tag],
                        'rows': len(result[0]) if result is not None else 0}

    if progress is not None:
        progress(total, total, 'writing manifest...', progress_info(total))

    # Save the measured per-item durations for the next run's estimates.
    try:
        with open(timing_path, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2)
    except OSError as e:
        print(f"  [!] Could not save timing history: {e}")

    # -- Write manifest --
    manifest_path = output_dir / 'manifest.json'
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"\nManifest written: {manifest_path}")

    # -- Summary --
    print(f"\n{'='*50}")
    print(f"Extraction complete!")
    print(f"  Tables:    {len(manifest['tables'])}")
    print(f"  1D plots:  {len(manifest['1d_plots'])}")
    print(f"  2D plots:  {len(manifest['2d_plots'])}")
    print(f"  3D plots:  {len(manifest['3d_plots'])}")
    print(f"  Other:     {len(manifest['other'])}")
    print(f"  Output:    {output_dir}")
    print(f"{'='*50}")

    return manifest, datasets


def main():
    parser = argparse.ArgumentParser(
        description='Extract results from a COMSOL .mph file and/or import them into OriginLab'
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('model', nargs='?', default=None,
                        help='Path to .mph file (otherwise use the Open button)')
    parser.add_argument('--output', '-o', default=None,
                        help='Output directory (default: folder next to .mph named <model>_results/)')
    parser.add_argument('--comsol', action='store_true',
                        help='Extract results from a COMSOL .mph model')
    parser.add_argument('--origin', action='store_true',
                        help='Import results into OriginLab (requires originpro)')
    parser.add_argument('--origin-template', default='',
                        help='Origin graph template (.otpu) to use')
    parser.add_argument('--low-memory', action='store_true',
                        help='Parse data as float32 instead of float64, halving memory use '
                             'on large tables/plots (precision loss below COMSOL\'s own '
                             'exported digits) - pre-ticks the same option in the window')
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
        datasets = load_datasets_from_folder(folder, low_memory=args.low_memory)
        if not datasets:
            sys.exit("No datasets found to import.")

        # Name the .opju after the .mph model recorded in manifest.json,
        # falling back to the results folder's name.
        try:
            with open(folder / 'manifest.json', encoding='utf-8') as f:
                project_name = Path(json.load(f).get('model', '')).stem
        except Exception:
            project_name = ''
        if not project_name:
            project_name = folder.name.removesuffix('_results')

        print("\nPushing results to OriginLab...")
        origin_pids_before = get_origin_pids()
        origin_project = push_to_origin(datasets, folder, template=args.origin_template,
                                        project_name=project_name)
        if origin_project is not None:
            close_new_origin_processes(origin_pids_before)
            os.startfile(origin_project)  # open the project in a fresh Origin instance

        os.startfile(folder)  # open the results folder in File Explorer
        print("Done - closing.")
        return

    # -- Model path: a CLI arg loads immediately; otherwise the window's
    # Open button shows the file dialog --
    model_path = None
    if args.model:
        model_path = Path(args.model).resolve()
        if not model_path.exists():
            sys.exit(f"File not found: {model_path}")

    # COMSOL already-running warning is shown inline in the combined dialog
    # below, rather than a blocking console prompt.
    comsol_warning = None
    if comsol_already_running():
        comsol_warning = (
            "Another COMSOL process is already running on this PC - this "
            "session will start an additional engine instance (uses more "
            "memory and may take a bit longer to start)."
        )

    # Runs inside the window's extraction thread once Extract is clicked;
    # the window stays open and shows a progress bar / remaining-time
    # estimate fed by the progress callback.
    def do_extract(model, chosen_path: Path, selected: list, low_memory: bool,
                   collect_datasets: bool, progress) -> dict:
        # -- Output folder: same location as .mph, named <stem>_results --
        if args.output:
            output_dir = Path(args.output).resolve()
        else:
            output_dir = chosen_path.parent / f"{chosen_path.stem}_results"
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Output folder: {output_dir}")

        manifest, datasets = extract_selected(
            model, chosen_path, selected, output_dir,
            low_memory=low_memory, collect_datasets=collect_datasets,
            progress=progress)
        return {'output_dir': output_dir, 'manifest': manifest, 'datasets': datasets}

    # -- Combined status + item-selection window, shown immediately --
    choice = run_extraction_window(model_path, comsol_warning, do_origin,
                                    low_memory_default=args.low_memory,
                                    extract_fn=do_extract)
    if choice is None:
        print("Nothing selected. Exiting.")
        return
    client = choice['client']
    if not choice['items']:
        print("Nothing selected. Exiting.")
        client.clear()
        return
    do_origin = choice['push_to_origin']
    extraction = choice['extraction']
    if extraction is None:
        # Extraction failed inside the window (error already printed there).
        client.clear()
        pause_if_frozen()
        return
    output_dir = extraction['output_dir']
    datasets = extraction['datasets']

    # -- Optional: push to OriginLab --
    if do_origin:
        print("\nPushing results to OriginLab...")
        origin_pids_before = get_origin_pids()
        origin_project = push_to_origin(datasets, output_dir, template=args.origin_template,
                                        project_name=choice['model_path'].stem)
        if origin_project is not None:
            close_new_origin_processes(origin_pids_before)
            os.startfile(origin_project)  # open the project in a fresh Origin instance

    # -- Open the output folder and clean up --
    os.startfile(output_dir)  # open the results folder in File Explorer
    client.clear()
    # Exiting also shuts down the in-process COMSOL engine (MPh's exit hook
    # stops the JVM), releasing the license seat - no keypress needed.
    print("Done - closing.")


if __name__ == '__main__':
    # Frozen-exe safety net: a successful run exits on its own (taking the
    # in-process COMSOL engine with it), while any error stays readable in
    # the console until Enter is pressed - and still exits cleanly after.
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            pause_if_frozen()
        raise
    except KeyboardInterrupt:
        raise
    except Exception:
        traceback.print_exc()
        pause_if_frozen()
        sys.exit(1)
