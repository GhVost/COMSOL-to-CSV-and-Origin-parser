from pathlib import Path

import numpy as np
import pandas as pd

from extraction import (
    deformation_comment,
    deformation_reference_columns,
    exaggerate_coordinates,
    legend_label_from_column,
    line_markers,
    line_series_dataframe,
    load_dataset_csv,
    merge_undeformed_reference,
    parse_comsol_export,
    read_leading_comments,
    sanitize_filename,
    split_header_line,
    split_label_unit,
    subsample_for_plot,
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
    assert loaded["Iout (mA)"].dtype == np.float64  # default: full precision


def test_csv_roundtrip_with_no_comments(tmp_path: Path):
    # write_csv_with_comments()'s leading '%' block can be empty - the
    # streaming header read in load_dataset_csv() must still find the two
    # header rows immediately.
    path = tmp_path / "data.csv"
    df = pd.DataFrame({"x (mm)": [1.0, 2.0], "y (mm)": [3.0, 4.0]})

    write_csv_with_comments(df, path, comments=None)
    loaded, comments = load_dataset_csv(path)

    assert comments == []
    assert list(loaded.columns) == ["x (mm)", "y (mm)"]
    assert loaded.shape == (2, 2)


def test_load_dataset_csv_low_memory_parses_float32(tmp_path: Path):
    path = tmp_path / "data.csv"
    df = pd.DataFrame({"x (mm)": [1.0, 2.0], "y (mm)": [3.0, 4.0]})
    write_csv_with_comments(df, path)

    loaded, _ = load_dataset_csv(path, low_memory=True)

    assert loaded["x (mm)"].dtype == np.float32
    assert loaded["x (mm)"].tolist() == [1.0, 2.0]


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


def test_parse_comsol_export_low_memory_downcasts_to_float32(tmp_path: Path):
    path = tmp_path / "plot.txt"
    path.write_text("% x  y\n0.123456 1.654321\n2.0 3.0\n", encoding="utf-8")

    parsed = parse_comsol_export(path, low_memory=True)

    assert parsed is not None
    df, _ = parsed
    assert all(df[col].dtype == np.float32 for col in df.columns)


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


def test_merge_undeformed_reference_appends_prefixed_columns_2d():
    df = pd.DataFrame({"X (mm)": [1.0, 2.0], "Y (mm)": [3.0, 4.0], "T (K)": [300.0, 310.0]})
    undeformed = pd.DataFrame({"X (mm)": [0.0, 0.0], "Y (mm)": [0.0, 0.0], "T (K)": [300.0, 310.0]})

    merged = merge_undeformed_reference(df, "2d", undeformed)

    assert list(merged.columns) == ["X (mm)", "Y (mm)", "T (K)", "Undeformed X (mm)", "Undeformed Y (mm)"]
    assert merged["Undeformed X (mm)"].tolist() == [0.0, 0.0]
    assert deformation_reference_columns(merged, "2d") == ["Undeformed X (mm)", "Undeformed Y (mm)"]


def test_merge_undeformed_reference_skips_on_row_mismatch():
    df = pd.DataFrame({"X (mm)": [1.0, 2.0, 3.0], "Y (mm)": [3.0, 4.0, 5.0]})
    undeformed = pd.DataFrame({"X (mm)": [0.0], "Y (mm)": [0.0]})

    merged = merge_undeformed_reference(df, "2d", undeformed)

    assert list(merged.columns) == ["X (mm)", "Y (mm)"]  # unchanged
    assert deformation_reference_columns(merged, "2d") is None


def test_exaggerate_coordinates_blends_toward_undeformed():
    deformed = np.array([10.0, 20.0])
    undeformed = np.array([0.0, 0.0])

    assert exaggerate_coordinates(deformed, undeformed, 1.0).tolist() == [10.0, 20.0]
    assert exaggerate_coordinates(deformed, undeformed, 0.0).tolist() == [0.0, 0.0]
    assert exaggerate_coordinates(deformed, undeformed, 2.0).tolist() == [20.0, 40.0]


def test_read_leading_comments_stops_at_first_data_line(tmp_path: Path):
    path = tmp_path / "export.txt"
    path.write_text(
        "% Model: demo.mph\n"
        "% x  y  Temperature\n"
        "0 0 300\n"
        "1 0 320\n",
        encoding="utf-8",
    )

    # Only the leading '%' block is read - a huge data body below it never
    # has to be loaded into memory just to find these two lines.
    assert read_leading_comments(path) == ["Model: demo.mph", "x  y  Temperature"]


def test_read_leading_comments_handles_no_comments(tmp_path: Path):
    path = tmp_path / "export.txt"
    path.write_text("0 0 300\n1 0 320\n", encoding="utf-8")

    assert read_leading_comments(path) == []


def test_subsample_for_plot_leaves_small_data_untouched():
    df = pd.DataFrame({"x": range(100), "y": range(100)})

    result = subsample_for_plot(df, max_points=1000)

    assert result is df  # same object - no copy needed when already small


def test_subsample_for_plot_takes_systematic_stride_preserving_order():
    df = pd.DataFrame({"x": range(1000), "y": range(1000)})

    result = subsample_for_plot(df, max_points=100)

    assert 90 <= len(result) <= 110  # stride-based, not an exact count
    assert result["x"].is_monotonic_increasing  # order preserved
    assert result["x"].tolist() == sorted(set(result["x"].tolist()))  # no duplicates


def test_subsample_for_plot_keeps_columns_row_aligned():
    # A deformed/undeformed coordinate pair (see merge_undeformed_reference)
    # must stay paired after subsampling - i.e. whole rows are kept, not
    # independently-sampled columns.
    df = pd.DataFrame({"deformed": range(1000), "undeformed": [v * 2 for v in range(1000)]})

    result = subsample_for_plot(df, max_points=50)

    assert (result["undeformed"] == result["deformed"] * 2).all()


def test_deformation_comment_reports_scale_and_mode():
    assert deformation_comment({"scale": 87.421, "auto_scale": True}) == "Deformation: scale=87.42 (auto)"
    assert deformation_comment({"scale": 5.0, "auto_scale": False}) == "Deformation: scale=5 (manual)"
    assert deformation_comment({"scale": None}) == "Deformation: active"
