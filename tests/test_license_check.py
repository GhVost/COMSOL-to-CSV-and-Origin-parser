from license_check import (
    load_mask_hosts_setting,
    mask_hostname,
    save_mask_hosts_setting,
    summarize_lmstat,
)
import license_check


def test_summarize_lmstat_filters_hosts_and_strips_pid_tail():
    raw = """
Users of COMSOL:  (Total of 4 licenses issued;  Total of 2 licenses in use)
    alice impt-01 display (v6.4) (server/1718 101), start Mon 6/30 9:15, PID: 123
    bob labpc display (v6.4) (server/1718 102), start Mon 6/30 9:16, PID: 456
"""

    report = summarize_lmstat(raw, "impt-*")

    assert "COMSOL: 2 of 4 in use" in report
    assert "alice on impt-01  (since Mon 6/30 9:15)" in report
    assert "bob" not in report


def test_summarize_lmstat_reports_no_matching_hosts():
    raw = """
Users of COMSOL:  (Total of 4 licenses issued;  Total of 1 license in use)
    alice impt-01 display (v6.4) (server/1718 101), start Mon 6/30 9:15, PID: 123
"""

    assert summarize_lmstat(raw, "lab-*") == (
        "No COMSOL modules are checked out by hosts matching 'lab-*'."
    )


def test_mask_hostname_keeps_short_prefix():
    assert mask_hostname("qc-sim-01") == "qc*******"
    assert mask_hostname("ab") == "**"
    assert mask_hostname("a") == "*"


def test_summarize_lmstat_masks_hosts_but_still_filters_by_real_name():
    raw = """
Users of COMSOL:  (Total of 4 licenses issued;  Total of 1 license in use)
    alice impt-01 display (v6.4) (server/1718 101), start Mon 6/30 9:15, PID: 123
"""

    report = summarize_lmstat(raw, "impt-*", mask_hosts=True)

    assert "alice on im*****  (since Mon 6/30 9:15)" in report  # only prefix kept
    assert "impt-01" not in report  # real hostname never shown when masked


def test_mask_hosts_setting_roundtrips_through_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(license_check, "SETTINGS_PATH", tmp_path / "settings.json")

    assert load_mask_hosts_setting() is False  # no file yet -> default

    save_mask_hosts_setting(True)
    assert load_mask_hosts_setting() is True

    save_mask_hosts_setting(False)
    assert load_mask_hosts_setting() is False
