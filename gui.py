"""
Qt GUI: the combined status/items/preview window (PySide6), the native file
and folder pickers, and the matplotlib preview widgets.
"""

import sys
import tempfile
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from PySide6.QtCore import QObject, Qt, QTimer, Signal
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
        QLineEdit, QListWidget, QListWidgetItem, QMdiArea, QPlainTextEdit,
        QProgressBar, QPushButton, QSlider, QSplitter, QTableWidget,
        QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
    )
except ImportError:
    sys.exit(
        "ERROR: PySide6 not installed.\n"
        "  pip install PySide6"
    )

from extraction import (
    deformation_reference_columns, discover_items, exaggerate_coordinates,
    extract_table, extract_via_export, legend_label_from_column, line_markers,
    line_series_dataframe, subsample_for_plot, surface_columns,
)
from license_check import (
    load_mask_hosts_setting, query_license_usage, save_mask_hosts_setting,
    summarize_lmstat,
)
from origin_push import origin_already_running, start_originpro


def qt_app() -> QApplication:
    """Return the process-wide QApplication, creating it on first use."""
    return QApplication.instance() or QApplication(sys.argv)


def pick_file_dialog() -> Path | None:
    """Open a native file-picker dialog and return the selected .mph path."""
    qt_app()
    file_path, _ = QFileDialog.getOpenFileName(
        None, 'Select a COMSOL model file',
        filter='COMSOL models (*.mph);;All files (*.*)',
    )
    return Path(file_path) if file_path else None


def pick_folder_dialog(title: str) -> Path | None:
    """Open a native folder-picker dialog and return the selected directory."""
    qt_app()
    folder = QFileDialog.getExistingDirectory(None, title)
    return Path(folder) if folder else None


# Fixed categorical color order for multi-curve line previews (Okabe-Ito,
# colorblind-safe; validated for CVD separation on a light surface).
PREVIEW_LINE_COLORS = ['#0072B2', '#E69F00', '#009E73', '#CC79A7', '#D55E00', '#56B4E9']

PREVIEW_MAX_ROWS = 2000  # Data tab caps preview rows; the CSV has the full data


def make_preview_figure(df: pd.DataFrame, kind: str, surface_alpha: float = 0.78,
                        exaggeration: float = 1.0):
    """Build a matplotlib Figure previewing a dataset.

    Tables are intentionally not plotted. 1D data is drawn as separate line
    series with peak markers. 2D/3D data is rendered as a triangulated
    surface so COMSOL deformation coordinates remain visible. When an
    undeformed reference geometry was captured (see
    extraction.merge_undeformed_reference()), exaggeration scales the
    displacement away from COMSOL's own configured deformation scale
    (1.0 = as COMSOL shows it, 0.0 = undeformed, >1.0 = exaggerated further).
    """
    try:
        from matplotlib.figure import Figure
    except ImportError:
        return None

    if kind == 'table':
        return None

    df = subsample_for_plot(df)
    num = df.select_dtypes('number')
    if num.shape[1] < 2:
        return None

    fig = Figure(figsize=(5, 4), layout='tight')
    surface = surface_columns(df, kind)
    ref_cols = deformation_reference_columns(df, kind) if surface else None

    def positions(cols: tuple[str, ...]) -> list[np.ndarray]:
        """Exaggerated coordinate arrays for the surface's spatial columns."""
        deformed = [num[c].to_numpy() for c in cols]
        if ref_cols is None or exaggeration == 1.0:
            return deformed
        undeformed = [df[c].to_numpy() for c in ref_cols]
        return [exaggerate_coordinates(d, u, exaggeration) for d, u in zip(deformed, undeformed)]

    if kind == '3d' and surface:
        x_col, y_col, z_col, value_col = surface
        x, y, z = positions((x_col, y_col, z_col))
        ax = fig.add_subplot(projection='3d')
        if value_col and value_col != z_col:
            surf = ax.plot_trisurf(x, y, z, cmap='viridis', linewidth=0.1,
                                   antialiased=True, shade=True,
                                   alpha=surface_alpha)
            surf.set_array(num[value_col].to_numpy())
            surf.autoscale()
            fig.colorbar(surf, ax=ax, label=str(value_col), shrink=0.7)
        else:
            surf = ax.plot_trisurf(x, y, z, cmap='viridis', linewidth=0.1,
                                   antialiased=True, alpha=surface_alpha)
            fig.colorbar(surf, ax=ax, label=str(z_col), shrink=0.7)
        ax.set_xlabel(str(x_col))
        ax.set_ylabel(str(y_col))
        ax.set_zlabel(str(z_col))
    elif kind == '2d' and surface:
        x_col, y_col, value_col, _ = surface
        x, y = positions((x_col, y_col))
        ax = fig.add_subplot()
        filled = ax.tricontourf(x, y, num[value_col], levels=32, cmap='viridis')
        ax.tricontour(x, y, num[value_col], levels=12, colors='k', linewidths=0.25, alpha=0.35)
        fig.colorbar(filled, ax=ax, label=str(value_col))
        ax.set_xlabel(str(x_col))
        ax.set_ylabel(str(y_col))
        ax.set_aspect('equal', adjustable='datalim')
    else:
        num = line_series_dataframe(df).select_dtypes('number')
        cols = num.columns
        if num.shape[1] < 2:
            return None
        ax = fig.add_subplot()
        ax.set_prop_cycle(color=PREVIEW_LINE_COLORS)
        for col in cols[1:]:
            ax.plot(num[cols[0]], num[col], lw=1.5,
                    label=legend_label_from_column(str(col)), marker='')
        for marker in line_markers(num):
            ax.plot(marker['x'], marker['y'], marker='o', ms=4, color='black')
            ax.annotate(f"{marker['y']:.3g}", (marker['x'], marker['y']),
                        textcoords='offset points', xytext=(4, 4), fontsize=8)
        ax.set_xlabel(str(cols[0]))
        if num.shape[1] == 2:
            ax.set_ylabel(str(cols[1]))  # single series: axis label, no legend
        else:
            ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
    return fig


def build_preview_plot_widget(df: pd.DataFrame, kind: str):
    """Build a preview's 'Plot' tab: the matplotlib canvas, plus any
    interactive controls - a 3D surface-opacity slider, and a deformation
    exaggeration slider for 2D/3D plots carrying an undeformed reference
    geometry (see extraction.merge_undeformed_reference()). Sliders rebuild
    the whole figure (trisurf/tricontourf have no cheap in-place coordinate
    update) and swap the canvas widget in place. Returns None if there is
    nothing plottable (see make_preview_figure())."""
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

    if kind not in ('2d', '3d'):
        fig = make_preview_figure(df, kind)
        return FigureCanvasQTAgg(fig) if fig is not None else None

    has_deformation = deformation_reference_columns(df, kind) is not None
    plot_state = {'alpha': 0.78, 'exaggeration': 1.0}
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    canvas_holder = {'canvas': None}

    def refresh():
        fig = make_preview_figure(df, kind, surface_alpha=plot_state['alpha'],
                                  exaggeration=plot_state['exaggeration'])
        if fig is None:
            return
        new_canvas = FigureCanvasQTAgg(fig)
        old_canvas = canvas_holder['canvas']
        if old_canvas is not None:
            layout.replaceWidget(old_canvas, new_canvas)
            old_canvas.setParent(None)
            old_canvas.deleteLater()
        else:
            layout.insertWidget(0, new_canvas, 1)
        canvas_holder['canvas'] = new_canvas

    refresh()
    if canvas_holder['canvas'] is None:
        return None  # nothing plottable (e.g. fewer than 2 numeric columns)

    if kind == '3d':
        alpha_row = QHBoxLayout()
        alpha_row.addWidget(QLabel("Surface opacity:"))
        alpha_slider = QSlider(Qt.Orientation.Horizontal)
        alpha_slider.setRange(15, 100)
        alpha_slider.setValue(78)
        alpha_value = QLabel("78%")
        alpha_row.addWidget(alpha_slider, 1)
        alpha_row.addWidget(alpha_value)
        layout.addLayout(alpha_row)

        def on_alpha(value: int):
            plot_state['alpha'] = value / 100
            alpha_value.setText(f"{value}%")
            refresh()

        alpha_slider.valueChanged.connect(on_alpha)

    if has_deformation:
        exag_row = QHBoxLayout()
        exag_row.addWidget(QLabel("Deformation exaggeration:"))
        exag_slider = QSlider(Qt.Orientation.Horizontal)
        exag_slider.setRange(0, 500)
        exag_slider.setValue(100)
        exag_value = QLabel("100% (COMSOL scale)")
        exag_row.addWidget(exag_slider, 1)
        exag_row.addWidget(exag_value)
        layout.addLayout(exag_row)

        def on_exaggeration(value: int):
            plot_state['exaggeration'] = value / 100
            suffix = (" (COMSOL scale)" if value == 100
                     else " (undeformed)" if value == 0 else "")
            exag_value.setText(f"{value}%{suffix}")
            refresh()

        exag_slider.valueChanged.connect(on_exaggeration)

    return widget


def make_data_table(df: pd.DataFrame) -> QTableWidget:
    """Read-only spreadsheet view of a DataFrame for a preview's Data tab."""
    nrows = min(len(df), PREVIEW_MAX_ROWS)
    table = QTableWidget(nrows, len(df.columns))
    table.setHorizontalHeaderLabels([str(c) for c in df.columns])
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    for i in range(nrows):
        for j, val in enumerate(df.iloc[i]):
            text = f"{val:.6g}" if isinstance(val, float) else str(val)
            table.setItem(i, j, QTableWidgetItem(text))
    return table


# Display names for the groups shown in the item-picker, in the order shown.
ITEM_GROUP_LABELS = {
    'probe': 'Probe Tables',
    'table': 'Tables',
    '1d': '1D Plots',
    '2d': '2D Plots',
    '3d': '3D Plots',
}


def item_group(item: dict) -> str:
    """Checklist group of a discovered item. Probe tables (a 'table' whose
    COMSOL label says so) get their own group so a whole category can be
    (de)selected at once; extraction-wise they stay ordinary tables."""
    if item['kind'] == 'table':
        return 'probe' if 'probe' in item['label'].lower() else 'table'
    return item['kind'] if item['kind'] in ITEM_GROUP_LABELS else 'other'


def run_extraction_window(model_path: Path | None, comsol_warning: str | None,
                           push_to_origin_default: bool,
                           low_memory_default: bool = False,
                           extract_fn=None) -> dict | None:
    """Open the combined status/items window immediately. An Open button
    picks the .mph model (skipped if model_path is already given, e.g. from
    the command line); COMSOL starts and loads it in a background thread
    while the window stays responsive.

    A status bar reports progress ("Starting COMSOL server...", "Loading
    model...", ...). Once the model is loaded, the items checklist is
    populated and Extract/Select All/Deselect All become available.

    Clicking a loaded item opens a preview tab (plot + data grid) in the MDI
    area on the right; a "License usage" button reports who currently holds
    FlexNet (FNL) seats of the COMSOL modules, filterable by host pattern.

    extract_fn(model, model_path, selected_items, low_memory,
    collect_datasets, progress) runs the extraction itself: clicking Extract
    keeps the window open, runs extract_fn in a background thread, and shows
    a progress bar with a remaining-time estimate (average duration of
    finished items x items left) fed by progress(done, total, label)
    callbacks. The window closes itself when extract_fn returns; its return
    value is passed back under 'extraction'.

    Returns {'client': ..., 'model': ..., 'model_path': ...,
    'items': [...selected items...], 'push_to_origin': bool,
    'low_memory': bool, 'extraction': ...}, or None if cancelled (closed the
    window or clicked Cancel, before or after loading).
    """
    app = qt_app()

    win = QWidget()
    win.setWindowTitle("COMSOL Extractor")
    win.resize(1080, 620)

    # Left pane: status + item checklist. Right pane: MDI area whose tabs
    # hold per-item previews and the license-usage report.
    left = QWidget()
    layout = QVBoxLayout(left)
    layout.setContentsMargins(0, 0, 0, 0)

    mdi = QMdiArea()
    mdi.setViewMode(QMdiArea.ViewMode.TabbedView)
    mdi.setTabsClosable(True)
    mdi.setTabsMovable(True)

    splitter = QSplitter()
    splitter.addWidget(left)
    splitter.addWidget(mdi)
    splitter.setSizes([440, 640])
    outer = QHBoxLayout(win)
    outer.addWidget(splitter)

    def set_led(led: QLabel, color: str):
        led.setStyleSheet(f"background-color: {color}; border-radius: 7px;")

    def make_led() -> QLabel:
        led = QLabel()
        led.setFixedSize(14, 14)
        set_led(led, '#b0b0b0')
        return led

    # -- Status section --
    status_box = QGroupBox("Status")
    status_layout = QVBoxLayout(status_box)
    layout.addWidget(status_box)

    comsol_row = QHBoxLayout()
    comsol_led = make_led()
    comsol_label = QLabel("COMSOL: not started")
    lic_btn = QPushButton("License usage")
    comsol_row.addWidget(comsol_led)
    comsol_row.addWidget(comsol_label)
    comsol_row.addStretch()
    comsol_row.addWidget(lic_btn)
    status_layout.addLayout(comsol_row)

    if comsol_warning:
        warn = QLabel(comsol_warning)
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #cc6600;")
        status_layout.addWidget(warn)

    origin_row = QHBoxLayout()
    origin_led = make_led()
    origin_status_lbl = QLabel("OriginLab: ...")
    start_btn = QPushButton("Start OriginPro")
    origin_row.addWidget(origin_led)
    origin_row.addWidget(origin_status_lbl)
    origin_row.addWidget(start_btn)
    origin_row.addStretch()
    status_layout.addLayout(origin_row)

    push_check = QCheckBox("Import extracted results into OriginLab")
    push_check.setChecked(push_to_origin_default)
    status_layout.addWidget(push_check)

    low_memory_check = QCheckBox("Low memory (parse as float32)")
    low_memory_check.setChecked(low_memory_default)
    low_memory_check.setToolTip(
        "Halves the memory used by large 2D/3D plots and tables by parsing "
        "into float32 instead of float64 - a precision loss well below "
        "COMSOL's own exported significant digits. Turn on if extraction "
        "crashes or runs out of memory on large models.")
    status_layout.addWidget(low_memory_check)

    def refresh_origin_led() -> bool:
        running = origin_already_running()
        set_led(origin_led, '#2ecc40' if running else '#b0b0b0')
        origin_status_lbl.setText(f"OriginLab: {'running' if running else 'not running'}")
        return running

    # Poll a few times after "Start OriginPro" is clicked, since the
    # process can take a couple of seconds to appear.
    origin_poll = QTimer(win)
    origin_poll.setInterval(500)
    poll_attempt = {'n': 0}

    def poll_origin_status():
        if refresh_origin_led():
            origin_poll.stop()
            push_check.setChecked(True)
            start_btn.setEnabled(True)
            start_btn.setText("Start OriginPro")
            return
        poll_attempt['n'] += 1
        if poll_attempt['n'] >= 20:
            origin_poll.stop()
            origin_status_lbl.setText("OriginLab: starting... (taking a while)")
            start_btn.setEnabled(True)
            start_btn.setText("Start OriginPro")

    origin_poll.timeout.connect(poll_origin_status)

    def on_start_origin():
        start_btn.setEnabled(False)
        start_btn.setText("Starting...")
        app.processEvents()  # repaint before the blocking COM connect
        ok, err = start_originpro()
        if not ok:
            origin_status_lbl.setText(f"OriginLab: failed to start ({err[:60]})")
            start_btn.setEnabled(True)
            start_btn.setText("Start OriginPro")
            return
        poll_attempt['n'] = 0
        origin_poll.start()

    start_btn.clicked.connect(on_start_origin)
    refresh_origin_led()

    # -- Items section --
    layout.addWidget(QLabel("Select which tables/plots to extract "
                            "(click an item to preview it):"))
    item_list = QListWidget()
    layout.addWidget(item_list, 1)

    def show_list_placeholder(text: str):
        item_list.clear()
        placeholder = QListWidgetItem(text)
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
        item_list.addItem(placeholder)

    show_list_placeholder("No model loaded - click Open to pick a .mph file.")

    # -- Extraction progress bar (shown only while extracting) + status bar --
    progress_bar = QProgressBar()
    progress_bar.setVisible(False)
    layout.addWidget(progress_bar)

    status_bar = QLabel("Open a COMSOL model (.mph) to begin.")
    status_bar.setStyleSheet("border: 1px inset palette(mid); padding: 2px 4px;")
    layout.addWidget(status_bar)

    check_items: list[QListWidgetItem] = []
    state = {'client': None, 'model': None, 'model_path': model_path,
             'items': [], 'cancelled': False, 'previewing': False,
             'pending_preview': None,
             'worker_start': time.monotonic(), 'lmstat_raw': '',
             'license_info': '', 'status_text': '',
             # Extraction-progress bookkeeping: run/current-item start
             # times, items finished, items total, current item label, and
             # the work-weighted fractions from extract_selected (see
             # progress_info() there).
             'extracting': False, 'ex_run_t0': 0.0, 'ex_item_t0': 0.0,
             'ex_done': 0, 'ex_total': 0, 'ex_label': '',
             'ex_info': {'frac_done': 0.0, 'item_frac': 0.0, 'item_pred': -1.0}}
    result = {'items': None, 'push_to_origin': False, 'low_memory': False,
              'extraction': None}

    # Checkable bold group heading -> its member entries; toggling the
    # heading (de)selects the whole category at once.
    group_members: dict[QListWidgetItem, list[QListWidgetItem]] = {}

    def populate_items(items):
        # Grouped by category (probe tables / tables / 1D / 2D / 3D), one
        # checkable row per item showing the human-readable COMSOL label.
        item_list.clear()
        check_items.clear()
        group_members.clear()
        bold = QFont()
        bold.setBold(True)
        grouped: dict[str, list[dict]] = {}
        for item in items:
            grouped.setdefault(item_group(item), []).append(item)
        for group in list(ITEM_GROUP_LABELS) + ['other']:
            members = grouped.get(group)
            if not members:
                continue
            heading = QListWidgetItem(ITEM_GROUP_LABELS.get(group, 'Other'))
            heading.setFont(bold)
            heading.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            heading.setCheckState(Qt.CheckState.Checked)
            item_list.addItem(heading)
            group_members[heading] = []
            for item in members:
                entry = QListWidgetItem(f"    {item['label']}")
                entry.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                               | Qt.ItemFlag.ItemIsSelectable)
                entry.setCheckState(Qt.CheckState.Checked)
                entry.setData(Qt.ItemDataRole.UserRole, item)
                entry.setToolTip(f"{item['tag']}: {item['label']}")
                item_list.addItem(entry)
                check_items.append(entry)
                group_members[heading].append(entry)

    # Guards the heading<->members synchronisation below so programmatic
    # check-state updates don't re-trigger it.
    sync = {'active': False}

    def on_list_item_changed(changed):
        if sync['active']:
            return
        entries = group_members.get(changed)
        if entries is not None:
            # The heading itself was clicked: (de)select the whole category.
            # PartiallyChecked is only ever set programmatically below - a
            # user click always lands on Checked or Unchecked.
            if changed.checkState() == Qt.CheckState.PartiallyChecked:
                return
            sync['active'] = True
            for entry in entries:
                entry.setCheckState(changed.checkState())
            sync['active'] = False
        else:
            # A single item was toggled: keep its choice, and let the
            # heading display the category's state - fully checked, fully
            # unchecked, or partial for a hand-picked subset.
            for heading, members in group_members.items():
                if changed in members:
                    states = {entry.checkState() for entry in members}
                    sync['active'] = True
                    heading.setCheckState(
                        Qt.CheckState.Checked if states == {Qt.CheckState.Checked}
                        else Qt.CheckState.Unchecked if states == {Qt.CheckState.Unchecked}
                        else Qt.CheckState.PartiallyChecked)
                    sync['active'] = False
                    break

    item_list.itemChanged.connect(on_list_item_changed)

    def set_all(checked: bool):
        cs = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for entry in check_items:
            entry.setCheckState(cs)  # each toggle also refreshes its heading

    def on_extract():
        selected = [
            entry.data(Qt.ItemDataRole.UserRole) for entry in check_items
            if entry.checkState() == Qt.CheckState.Checked
        ]
        result['push_to_origin'] = push_check.isChecked()
        result['low_memory'] = low_memory_check.isChecked()
        if extract_fn is None or not selected:
            result['items'] = selected
            win.close()
            return

        # Keep the window open and run the extraction in a background
        # thread, showing per-item progress; the window closes itself once
        # extract_fn finishes (see on_extract_done).
        state['extracting'] = True
        state['ex_run_t0'] = state['ex_item_t0'] = time.monotonic()
        state['ex_done'], state['ex_total'], state['ex_label'] = 0, len(selected), '...'
        state['ex_info'] = {'frac_done': 0.0, 'item_frac': 0.0, 'item_pred': -1.0}
        for w in (open_btn, select_all_btn, deselect_all_btn, extract_btn,
                  cancel_btn, item_list, push_check, low_memory_check):
            w.setEnabled(False)
        # Weighted work fraction in permille, not an item count - the bar
        # keeps moving through one long item.
        progress_bar.setRange(0, 1000)
        progress_bar.setValue(0)
        progress_bar.setVisible(True)
        update_extract_status()
        extract_timer.start()

        low_memory, collect = result['low_memory'], result['push_to_origin']

        def extract_worker():
            try:
                out = extract_fn(
                    state['model'], state['model_path'], selected,
                    low_memory, collect,
                    lambda done, total, label, info:
                        signals.extract_progress.emit(done, total, label, info))
                signals.extract_done.emit((selected, out))
            except Exception as e:
                traceback.print_exc()
                signals.extract_error.emit(str(e))

        threading.Thread(target=extract_worker, daemon=True).start()

    # Buttons in work-sequence order: Open >> Select/Deselect >> Extract.
    btn_row = QHBoxLayout()
    open_btn = QPushButton("Open...")
    select_all_btn = QPushButton("Select All")
    deselect_all_btn = QPushButton("Deselect All")
    extract_btn = QPushButton("Extract")
    cancel_btn = QPushButton("Cancel")
    for b in (select_all_btn, deselect_all_btn, extract_btn):
        b.setEnabled(False)
    select_all_btn.clicked.connect(lambda: set_all(True))
    deselect_all_btn.clicked.connect(lambda: set_all(False))
    extract_btn.clicked.connect(on_extract)
    cancel_btn.clicked.connect(win.close)
    btn_row.addWidget(open_btn)
    btn_row.addWidget(select_all_btn)
    btn_row.addWidget(deselect_all_btn)
    btn_row.addStretch()
    btn_row.addWidget(extract_btn)
    btn_row.addWidget(cancel_btn)
    layout.addLayout(btn_row)

    # -- Start COMSOL, load the model, and discover items in the background --
    # Qt signals are thread-safe when emitted from a plain Python thread
    # (delivered as queued events on the GUI thread), so no polling loop is
    # needed.
    class WorkerSignals(QObject):
        status = Signal(str)
        server_ready = Signal()
        ready = Signal(object, list)
        error = Signal(str)
        preview = Signal(object, object)   # item, (df, comments) or None
        license = Signal(str, str)         # info line, raw lmstat output
        extract_progress = Signal(int, int, str, object)  # done, total, label, work-fraction info dict
        extract_done = Signal(object)      # (selected items, extract_fn result)
        extract_error = Signal(str)

    signals = WorkerSignals()

    def worker(path: Path):
        try:
            client = state['client']
            if client is None:
                signals.status.emit("Starting COMSOL server...")
                try:
                    import mph
                except ImportError as exc:
                    raise RuntimeError(
                        "MPh is not installed. Install it with: pip install MPh"
                    ) from exc
                client = mph.start()
                # Store the client as soon as it exists so the cancel path
                # below can shut it down even if the window closes mid-load.
                state['client'] = client
                if state['cancelled']:
                    client.clear()
                    return
            else:
                client.clear()  # drop the previously opened model
            signals.server_ready.emit()  # COMSOL engine is up - LED can go green now
            signals.status.emit(f"Loading model: {path.name}")
            model = client.load(str(path))
            signals.status.emit("Discovering extractable items...")
            items = discover_items(model)
            signals.ready.emit(model, items)
        except Exception as e:
            signals.error.emit(str(e))

    def start_loading(path: Path):
        state['model_path'] = path
        state['model'] = None
        state['items'] = []
        check_items.clear()
        show_list_placeholder("Waiting for COMSOL to load the model...")
        # Previews belong to the previous model - close them.
        for key, sub in list(open_tabs.items()):
            if key != '__license__':
                sub.close()
        open_btn.setEnabled(False)
        for b in (select_all_btn, deselect_all_btn, extract_btn):
            b.setEnabled(False)
        set_led(comsol_led, '#b0b0b0')
        comsol_label.setText("COMSOL: starting...")
        state['worker_start'] = time.monotonic()
        state['status_text'] = "Starting COMSOL server..."
        elapsed_timer.start()
        threading.Thread(target=worker, args=(path,), daemon=True).start()

    def on_open():
        path = pick_file_dialog()
        if path is not None:
            start_loading(path.resolve())

    open_btn.clicked.connect(on_open)

    def on_status(text: str):
        state['status_text'] = text
        status_bar.setText(text)

    def on_server_ready():
        set_led(comsol_led, '#2ecc40')
        comsol_label.setText("COMSOL: server running - loading model...")

    def on_ready(model, items):
        state['model'], state['items'] = model, items
        elapsed_timer.stop()
        open_btn.setEnabled(True)
        set_led(comsol_led, '#2ecc40')
        comsol_label.setText(f"COMSOL: model '{state['model_path'].name}' loaded")
        if items:
            populate_items(items)
            status_bar.setText(f"Found {len(items)} extractable item(s). Ready.")
            for b in (select_all_btn, deselect_all_btn, extract_btn):
                b.setEnabled(True)
        else:
            show_list_placeholder("No extractable tables or plot groups found "
                                  "in this model.")
            status_bar.setText("No extractable tables or plot groups found in this model.")

    def on_error(text: str):
        elapsed_timer.stop()
        open_btn.setEnabled(True)
        set_led(comsol_led, '#ff4136')
        comsol_label.setText("COMSOL: failed to start")
        status_bar.setText(f"Error: {text}")
        print(f"\n[!] {text}")

    # -- Extraction progress: bar + "item i/N, elapsed, ~remaining" status --
    def format_hms(seconds: float) -> str:
        return str(timedelta(seconds=max(int(seconds), 0)))

    def update_extract_status():
        done, total = state['ex_done'], state['ex_total']
        info = state['ex_info']
        now = time.monotonic()
        elapsed = now - state['ex_run_t0']
        # Work fraction completed: finished items' weight plus the current
        # item's weight scaled by how far through its predicted time it is
        # (capped below 1 so an overrunning item cannot claim to be done).
        p = info['frac_done']
        if info['item_pred'] > 0:
            cur = now - state['ex_item_t0']
            p += info['item_frac'] * min(cur / info['item_pred'], 0.95)
        progress_bar.setValue(int(p * 1000))
        text = (f"Extracting {min(done + 1, total)}/{total}: {state['ex_label']}  "
                f"(elapsed {format_hms(elapsed)}, ")
        if p > 0:
            # Pace-based estimate: elapsed so far, extrapolated over the
            # work fraction still left. Recomputed every tick, so it rises
            # honestly when an item runs longer than its history predicted
            # instead of counting down to zero and sticking there.
            text += f"~{format_hms(elapsed * (1 - p) / p)} remaining)"
        else:
            text += "estimating remaining time...)"
        status_bar.setText(text)

    def on_extract_progress(done: int, total: int, label: str, info: dict):
        state['ex_done'], state['ex_label'], state['ex_info'] = done, label, info
        state['ex_item_t0'] = time.monotonic()
        update_extract_status()

    def on_extract_done(payload):
        selected, out = payload
        extract_timer.stop()
        state['extracting'] = False
        result['items'] = selected
        result['extraction'] = out
        win.close()

    def on_extract_error(text: str):
        extract_timer.stop()
        state['extracting'] = False
        progress_bar.setVisible(False)
        for w in (open_btn, select_all_btn, deselect_all_btn, extract_btn,
                  cancel_btn, item_list, push_check, low_memory_check):
            w.setEnabled(True)
        status_bar.setText(f"Extraction failed: {text} (details in the console)")

    signals.status.connect(on_status)
    signals.server_ready.connect(on_server_ready)
    signals.ready.connect(on_ready)
    signals.error.connect(on_error)
    signals.extract_progress.connect(on_extract_progress)
    signals.extract_done.connect(on_extract_done)
    signals.extract_error.connect(on_extract_error)

    # -- Preview tabs (MDI) --
    open_tabs = {}  # preview key (item tag or '__license__') -> QMdiSubWindow

    def add_mdi_tab(widget, title: str, key: str):
        sub = mdi.addSubWindow(widget)
        sub.setWindowTitle(title)
        sub.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        sub.destroyed.connect(lambda *_, k=key: open_tabs.pop(k, None))
        open_tabs[key] = sub
        sub.show()
        mdi.setActiveSubWindow(sub)

    def preview_key(item: dict) -> str:
        return f"{item['kind']}:{item['tag']}"

    def request_preview(entry):
        if entry is None or entry not in check_items:
            return  # nothing selected, or a group heading
        item = entry.data(Qt.ItemDataRole.UserRole)
        tag = item['tag']
        key = preview_key(item)
        if key in open_tabs:
            mdi.setActiveSubWindow(open_tabs[key])
            return
        if state['previewing']:
            state['pending_preview'] = entry
            status_bar.setText(f"Queued preview of '{tag}'...")
            return  # one preview extraction at a time; COMSOL calls don't overlap
        state['previewing'] = True
        state['pending_preview'] = None
        status_bar.setText(f"Extracting preview of '{tag}'...")

        low_memory = low_memory_check.isChecked()

        def preview_worker():
            try:
                if item['kind'] == 'table':
                    data = extract_table(state['model'], tag, low_memory=low_memory)
                else:
                    data = extract_via_export(state['model'], item['pg'], tag,
                                              Path(tempfile.gettempdir()), kind=item['kind'],
                                              low_memory=low_memory)
            except Exception:
                traceback.print_exc()
                data = None
            signals.preview.emit(item, data)

        threading.Thread(target=preview_worker, daemon=True).start()

    def on_preview(item, data):
        state['previewing'] = False
        if data is None or data[0].empty:
            status_bar.setText(f"Preview of '{item['tag']}' failed - no data "
                               "extracted (details in the console output).")
            pending = state.get('pending_preview')
            if pending is not None:
                state['pending_preview'] = None
                request_preview(pending)
            return
        df = data[0]
        tabs = QTabWidget()
        try:
            plot_widget = (build_preview_plot_widget(df, item['kind'])
                           if item['kind'] != 'table' else None)
            if plot_widget is not None:
                tabs.addTab(plot_widget, 'Plot')
        except Exception:
            traceback.print_exc()  # plot failed; still show the Data tab
        tabs.addTab(make_data_table(df), 'Data')
        add_mdi_tab(tabs, item['label'], preview_key(item))
        status_bar.setText(f"Preview ready: {item['tag']} "
                           f"({len(df)} rows x {len(df.columns)} cols)")
        pending = state.get('pending_preview')
        if pending is not None:
            state['pending_preview'] = None
            request_preview(pending)

    item_list.itemClicked.connect(request_preview)
    signals.preview.connect(on_preview)

    # -- FlexNet license usage (host-filterable report tab) --
    def on_license_clicked():
        lic_btn.setEnabled(False)
        status_bar.setText("Querying FlexNet license server (lmstat)...")
        threading.Thread(target=lambda: signals.license.emit(*query_license_usage()),
                         daemon=True).start()

    def render_license():
        key = '__license__'
        if key not in open_tabs:
            return
        box = open_tabs[key].widget()
        raw = state['lmstat_raw']
        if not raw:
            box.text_view.setPlainText(state['license_info'])
            return
        report = (summarize_lmstat(raw, box.filter_edit.text(), box.mask_check.isChecked())
                  or "Unexpected lmstat output (no 'Users of <module>' "
                     "lines):\n\n" + raw)
        box.text_view.setPlainText(f"{state['license_info']}\n\n{report}")

    def on_mask_toggled(checked: bool):
        save_mask_hosts_setting(checked)
        render_license()

    def on_license_report(info: str, raw: str):
        lic_btn.setEnabled(True)
        status_bar.setText("License report updated.")
        state['license_info'], state['lmstat_raw'] = info, raw
        key = '__license__'
        if key not in open_tabs:
            box = QWidget()
            box_layout = QVBoxLayout(box)
            filter_row = QHBoxLayout()
            filter_row.addWidget(QLabel("Host filter:"))
            box.filter_edit = QLineEdit('*-*')
            box.filter_edit.setToolTip(
                "fnmatch pattern applied to session hostnames, e.g. '*-*' or "
                "'impt-*'; '*' shows all hosts")
            filter_row.addWidget(box.filter_edit)
            box.mask_check = QCheckBox("Mask hostnames")
            box.mask_check.setToolTip(
                "Obscure workstation names in the displayed report (filtering "
                "above still matches the real hostname). Remembered for next start.")
            box.mask_check.setChecked(load_mask_hosts_setting())
            filter_row.addWidget(box.mask_check)
            box_layout.addLayout(filter_row)
            box.text_view = QPlainTextEdit()
            box.text_view.setReadOnly(True)
            box_layout.addWidget(box.text_view)
            box.filter_edit.textChanged.connect(lambda _: render_license())
            box.mask_check.toggled.connect(on_mask_toggled)
            add_mdi_tab(box, "License usage", key)
        render_license()
        mdi.setActiveSubWindow(open_tabs[key])

    lic_btn.clicked.connect(on_license_clicked)
    signals.license.connect(on_license_report)

    # Append elapsed time to the status bar while the model is loading.
    elapsed_timer = QTimer(win)
    elapsed_timer.setInterval(500)
    elapsed_timer.timeout.connect(lambda: status_bar.setText(
        f"{state['status_text']} ({int(time.monotonic() - state['worker_start'])}s)"))

    # Tick the current item's elapsed time / remaining estimate while a
    # single long COMSOL call blocks the extraction thread.
    extract_timer = QTimer(win)
    extract_timer.setInterval(500)
    extract_timer.timeout.connect(update_extract_status)

    # Ignore attempts to close the window mid-extraction - the extraction
    # thread is deep in a blocking COMSOL call and cannot be cancelled.
    win.closeEvent = lambda event: (
        event.ignore() if state['extracting'] else event.accept())

    if model_path is not None:
        start_loading(model_path)

    win.show()
    app.exec()

    if result['items'] is None:
        state['cancelled'] = True
        if state['client'] is not None:
            try:
                # Best effort: the worker may still be loading the model on
                # this client; clear() can then fail, and the worker's own
                # 'cancelled' check covers the client-created-after-close race.
                state['client'].clear()
            except Exception:
                pass
        return None
    return {'client': state['client'], 'model': state['model'],
            'model_path': state['model_path'], 'items': result['items'],
            'push_to_origin': result['push_to_origin'], 'low_memory': result['low_memory'],
            'extraction': result['extraction']}
