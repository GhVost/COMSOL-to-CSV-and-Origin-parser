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

__version__ = '1.8.0'

import argparse
import os
import sys
import json
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
    """When running as a bundled .exe (no console window of its own), wait
    for Enter before the window closes so any final messages - including
    OriginLab push errors - stay visible."""
    if getattr(sys, 'frozen', False):
        input("\nPress Enter to exit... ")


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
        origin_project = push_to_origin(datasets, folder, template=args.origin_template)
        if origin_project is not None:
            close_new_origin_processes(origin_pids_before)
            os.startfile(origin_project)  # open the project in a fresh Origin instance

        os.startfile(folder)  # open the results folder in File Explorer
        print("Done.")
        pause_if_frozen()
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

    # -- Combined status + item-selection window, shown immediately --
    choice = run_extraction_window(model_path, comsol_warning, do_origin)
    if choice is None:
        print("Nothing selected. Exiting.")
        return
    client, model = choice['client'], choice['model']
    if not choice['items']:
        print("Nothing selected. Exiting.")
        client.clear()
        return
    selected = choice['items']
    do_origin = choice['push_to_origin']
    model_path = choice['model_path']

    # -- Output folder: same location as .mph, named <stem>_results --
    if args.output:
        output_dir = Path(args.output).resolve()
    else:
        output_dir = model_path.parent / f"{model_path.stem}_results"
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output folder: {output_dir}")

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
    datasets = []  # collected for direct OriginLab export: {'name', 'kind', 'df'}

    # ---- Extract selected items ----
    for item in selected:
        tag, label, kind = item['tag'], item['label'], item['kind']

        if kind == 'table':
            print(f"  - {tag} ({label})")
            result = extract_table(model, tag)
            if result is not None and not result[0].empty:
                df, comments = result
                comments = comments + [date_stamp]
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
            comments = comments + [date_stamp]

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

    # -- Optional: push to OriginLab --
    if do_origin:
        print("\nPushing results to OriginLab...")
        origin_pids_before = get_origin_pids()
        origin_project = push_to_origin(datasets, output_dir, template=args.origin_template)
        if origin_project is not None:
            close_new_origin_processes(origin_pids_before)
            os.startfile(origin_project)  # open the project in a fresh Origin instance

    # -- Open the output folder and clean up --
    os.startfile(output_dir)  # open the results folder in File Explorer
    client.clear()
    print("Done.")
    pause_if_frozen()


if __name__ == '__main__':
    main()
