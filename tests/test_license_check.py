from license_check import summarize_lmstat


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
