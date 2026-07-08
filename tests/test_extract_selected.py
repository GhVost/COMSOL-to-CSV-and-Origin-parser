"""extract_selected(): progress/ETA callbacks, CSV/manifest output, timing history."""

import json

import pandas as pd

import COMSOLExtractor


def run(tmp_path, selected, calls):
    return COMSOLExtractor.extract_selected(
        model=None, model_path=tmp_path / 'model.mph', selected=selected,
        output_dir=tmp_path, low_memory=False, collect_datasets=True,
        progress=lambda done, total, label, info: calls.append((done, total, label, info)))


def test_extract_selected_progress_and_outputs(tmp_path, monkeypatch):
    df = pd.DataFrame({'freq (GHz)': [1.0, 2.0], 'Q': [10.0, 20.0]})
    monkeypatch.setattr(COMSOLExtractor, 'extract_table',
                        lambda model, tag, low_memory=False: (df.copy(), []))

    selected = [{'tag': 'tbl1', 'label': 'Probe Table 1', 'kind': 'table'},
                {'tag': 'tbl2', 'label': 'Table 2', 'kind': 'table'}]

    calls = []
    manifest, datasets = run(tmp_path, selected, calls)

    # One call per item start + one final done==total call.
    assert [(c[0], c[1]) for c in calls] == [(0, 2), (1, 2), (2, 2)]
    assert calls[0][2] == 'tbl1 (Probe Table 1)'
    # First run: equal weights; no per-item prediction before anything
    # finished, a measured-pace one afterwards; final call reports all done.
    assert calls[0][3] == {'frac_done': 0.0, 'item_frac': 0.5, 'item_pred': -1.0}
    assert calls[1][3]['frac_done'] == 0.5 and calls[1][3]['item_pred'] > 0
    assert calls[2][3]['frac_done'] == 1.0

    assert len(manifest['tables']) == 2
    assert (tmp_path / manifest['tables'][0]['file']).exists()
    assert json.loads((tmp_path / 'manifest.json').read_text(encoding='utf-8'))['tables']
    assert len(datasets) == 2

    # Timing history was saved with per-item duration and size.
    history = json.loads((tmp_path / '.extract_timing.json').read_text(encoding='utf-8'))
    assert set(history) == {'tbl1', 'tbl2'}
    assert history['tbl1']['rows'] == 2 and history['tbl1']['seconds'] >= 0

    # Second run: history weights give a size-aware prediction from the
    # very first item.
    calls2 = []
    run(tmp_path, selected, calls2)
    first = calls2[0][3]
    assert first['frac_done'] == 0.0 and 0.0 < first['item_frac'] <= 1.0
    assert first['item_pred'] >= 0.0
    assert calls2[-1][3]['frac_done'] == 1.0


def test_dataset_names_use_labels_and_dedupe(tmp_path, monkeypatch):
    monkeypatch.setattr(COMSOLExtractor, 'extract_table',
                        lambda model, tag, low_memory=False: (pd.DataFrame({'a': [1.0]}), []))
    selected = [{'tag': 'tbl1', 'label': 'Probe Table 1', 'kind': 'table'},
                {'tag': 'tbl2', 'label': 'Q factor', 'kind': 'table'},
                {'tag': 'tbl3', 'label': 'Q factor', 'kind': 'table'}]
    manifest, _ = COMSOLExtractor.extract_selected(
        model=None, model_path=tmp_path / 'model.mph', selected=selected,
        output_dir=tmp_path, low_memory=False, collect_datasets=False)
    files = [t['file'] for t in manifest['tables']]
    # Human labels, no COMSOL tags - except to break a duplicate label.
    assert files == ['Probe_Table_1.csv', 'Q_factor.csv', 'Q_factor_(tbl3).csv']
    for fname in files:
        assert (tmp_path / fname).exists()


def test_collect_datasets_off(tmp_path, monkeypatch):
    monkeypatch.setattr(COMSOLExtractor, 'extract_table',
                        lambda model, tag, low_memory=False: (pd.DataFrame({'a': [1.0]}), []))
    _, datasets = COMSOLExtractor.extract_selected(
        model=None, model_path=tmp_path / 'model.mph',
        selected=[{'tag': 'tbl1', 'label': 'T', 'kind': 'table'}],
        output_dir=tmp_path, low_memory=False, collect_datasets=False)
    assert datasets is None
