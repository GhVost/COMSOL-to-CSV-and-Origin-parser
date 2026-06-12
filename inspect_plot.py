"""
COMSOL Plot Property Inspector
===============================
Diagnostic helper for ComsolExtractor.py: dumps every property (and value)
of each plot group, its dataset and its features, plus every geometry's
length unit - so the real property names COMSOL uses for axis units (e.g.
for 2D/3D plot R/Z/u/w columns) can be read off directly instead of guessed.

Usage:
    python inspect_plot.py [model.mph] [--tag pg5]

Opens a file dialog if no model is given. With --tag, inspects only that
plot group; otherwise inspects every plot group.
"""

import sys
import argparse
from pathlib import Path

import mph
from mph.node import get as get_property

from ComsolExtractor import pick_file_dialog


def dump_properties(java_obj, label: str, indent: int = 0):
    pad = '  ' * indent
    print(f"{pad}--- {label} ---")
    try:
        names = sorted(str(n) for n in java_obj.properties())
    except Exception as e:
        print(f"{pad}  (no properties: {e})")
        return
    if not names:
        print(f"{pad}  (none)")
    for name in names:
        try:
            value = get_property(java_obj, name)
        except Exception as e:
            value = f"<error: {e}>"
        print(f"{pad}  {name} = {value!r}")


def dump_feature_tree(feat, tag: str, indent: int = 1):
    label = str(feat.label()) if hasattr(feat, 'label') else tag
    dump_properties(feat, f"feature {tag} ({label})", indent=indent)
    try:
        for ctag in feat.feature().tags():
            ctag = str(ctag)
            dump_feature_tree(feat.feature(ctag), ctag, indent=indent + 1)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(
        description='Dump COMSOL plot-group properties for unit debugging'
    )
    parser.add_argument('model', nargs='?', default=None)
    parser.add_argument('--tag', default=None,
                         help="Only inspect this plot group tag (e.g. 'pg5')")
    args = parser.parse_args()

    if args.model:
        model_path = Path(args.model).resolve()
    else:
        model_path = pick_file_dialog()
        if model_path is None:
            sys.exit("No file selected.")
        model_path = model_path.resolve()

    print("Starting COMSOL server...")
    client = mph.start()
    print(f"Loading model: {model_path.name}")
    model = client.load(str(model_path))
    print("Model loaded.\n")

    # Geometry length units (likely source of R/Z axis units in 2D/3D plots).
    try:
        for gtag in model.java.geom().tags():
            gtag = str(gtag)
            try:
                unit = str(model.java.geom(gtag).lengthUnit())
            except Exception as e:
                unit = f"<error: {e}>"
            print(f"geom('{gtag}').lengthUnit() = {unit!r}")
    except Exception as e:
        print(f"Could not list geometries: {e}")
    print()

    java_result = model.java.result()
    pg_tags = [str(t) for t in java_result.tags()]
    if args.tag:
        pg_tags = [t for t in pg_tags if t == args.tag]
        if not pg_tags:
            sys.exit(f"No plot group with tag '{args.tag}' found.")

    for tag in pg_tags:
        try:
            pg = model.java.result(tag)
            label = str(pg.label()) if hasattr(pg, 'label') else tag
        except Exception as e:
            print(f"[!] Could not access '{tag}': {e}")
            continue

        print(f"=== Plot group '{tag}' ({label}) ===")
        dump_properties(pg, f"plot group {tag}")

        # Underlying dataset.
        try:
            ds_tag = str(pg.getString('data'))
            if ds_tag and ds_tag.lower() != 'none':
                ds = model.java.result().dataset(ds_tag)
                dump_properties(ds, f"dataset {ds_tag}", indent=1)
        except Exception as e:
            print(f"  (could not get dataset: {e})")

        # Plot features (e.g. Line Graph, Surface).
        try:
            for ftag in pg.feature().tags():
                ftag = str(ftag)
                feat = pg.feature(ftag)
                dump_feature_tree(feat, ftag, indent=1)
        except Exception as e:
            print(f"  (could not list features: {e})")

        print()

    client.clear()


if __name__ == '__main__':
    main()
