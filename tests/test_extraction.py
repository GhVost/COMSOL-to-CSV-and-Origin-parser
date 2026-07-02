from pathlib import Path

import pandas as pd

from extraction import (
    legend_label_from_column,
    line_markers,
    line_series_dataframe,
    load_dataset_csv,
    parse_comsol_export,
    sanitize_filename,
    split_header_line,
    split_label_unit,
    write_csv_with_comments,
)


def test_split_label_unit_handles_middle_unit_with_curve_label():
    assert split_label_unit("Iout (mA), V_dc=1 V") == ("Iout, V_dc=1 V", "mA")


def test_legend_label_from_column_uses_comsol_curve_suffix():
    assert legend_label_from_column("Iout (mA), V_dc=1 V") == "V_dc=1 V"
    assert legend_label_from_column("Iout 1 (mA)") == "Iout 1"


def test_sanitize_filename_removes_windows_forbidden_chars():
    assert sanitize_filename('plot:one/two*three?') == "plot_one_two_three"


def test_csv_roundtrip_preserves_comments_and_units(tmp_path: Path):
    path = tmp_path / "data.csv"
    df = pd.DataFrame({"freq (GHz)": [1.0, 2.0], "Iout (mA)": [0.1, 0.2]})

    write_csv_with_comments(df, path, ["Model: demo", "Extracted: 20260702"])
    loaded, comments = load_dataset_csv(path)

    assert comments == ["Model: demo", "Extracted: 20260702"]
    assert list(loaded.columns) == ["freq (GHz)", "Iout (mA)"]
    assert loaded.shape == (2, 2)


def test_split_header_line_handles_repeated_curve_headers():
    header = "freq (GHz)  Iout (mA), V=0 Iout (mA), V=1"

    assert split_header_line(header, 3) == [
        "freq (GHz)",
        "Iout (mA), V=0",
        "Iout (mA), V=1",
    ]


def test_parse_comsol_export_applies_coordinate_units(tmp_path: Path):
    path = tmp_path / "plot.txt"
    path.write_text(
        "% Model: demo\n"
        "% x  y  Temperature\n"
        "0 0 300\n"
        "1 0 320\n",
        encoding="utf-8",
    )

    parsed = parse_comsol_export(path, units_map={"Temperature": "K"}, coordinate_unit="um")

    assert parsed is not None
    df, meta = parsed
    assert meta == ["Model: demo"]
    assert list(df.columns) == ["x (um)", "y (um)", "Temperature (K)"]


def test_line_series_dataframe_splits_concatenated_sweeps():
    df = pd.DataFrame({
        "freq (GHz)": [1.0, 2.0, 3.0, 1.0, 2.0, 3.0],
        "Iout (mA)": [1.0, 2.0, 3.0, 10.0, 20.0, 30.0],
    })

    wide = line_series_dataframe(df)

    assert list(wide.columns) == ["freq (GHz)", "Iout 1 (mA)", "Iout 2 (mA)"]
    assert wide["Iout 1 (mA)"].tolist() == [1.0, 2.0, 3.0]
    assert wide["Iout 2 (mA)"].tolist() == [10.0, 20.0, 30.0]


def test_line_markers_returns_peak_per_series():
    df = pd.DataFrame({"x": [1, 2, 3], "a": [1, 3, 2], "b": [5, 4, 6]})

    markers = line_markers(df)

    assert [(m["y_col"], m["x"], m["y"]) for m in markers] == [
        ("a", 2.0, 3.0),
        ("b", 3.0, 6.0),
    ]
