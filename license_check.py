"""
FlexNet (FNL) license usage: locate lmstat/lmutil in the local COMSOL
installation, query the license server, and condense the output into a
per-module usage report filterable by host pattern.
"""

import os
import re
import fnmatch
import subprocess
from pathlib import Path


def find_lmstat() -> tuple[list[str], Path] | None:
    """Locate FlexNet's lmstat command and the license file inside a COMSOL
    installation under Program Files, preferring the newest COMSOL version.

    Returns (command_prefix, license_file). COMSOL 6.x only ships the
    lmutil.exe multiplexer (lmstat runs as `lmutil lmstat`); a standalone
    lmstat.exe from older versions is preferred when both exist.
    """
    roots = {os.environ.get('ProgramFiles'), os.environ.get('ProgramW6432')}
    tools = set()
    for root in filter(None, roots):
        comsol_root = Path(root) / 'COMSOL'
        if comsol_root.is_dir():
            for name in ('lmstat.exe', 'lmutil.exe'):
                # e.g. COMSOL64/Multiphysics/license/win64/lmutil.exe
                tools.update(comsol_root.glob(f'*/license/win64/{name}'))
                tools.update(comsol_root.glob(f'*/*/license/win64/{name}'))

    ordered = sorted(tools, key=lambda p: p.name)  # lmstat.exe before lmutil.exe
    ordered.sort(key=lambda p: str(p.parent).lower(), reverse=True)  # newest first
    for tool in ordered:
        license_file = tool.parents[1] / 'license.dat'
        if license_file.is_file():
            cmd = [str(tool)] if tool.name == 'lmstat.exe' else [str(tool), 'lmstat']
            return cmd, license_file
    return None


def summarize_lmstat(text: str, host_filter: str = '*') -> str:
    """Condense `lmstat -a` output into one line per checked-out COMSOL
    module plus the user@host sessions holding each seat, keeping only
    sessions whose host matches host_filter (an fnmatch pattern, e.g. '*-*'
    or 'impt-*'; '*' keeps everything). Returns '' if the output contains no
    'Users of <module>' lines at all (so the caller can fall back to showing
    the raw output)."""
    pattern = (host_filter or '*').strip() or '*'
    blocks = []    # (header line, [(user, host, since), ...]) per in-use module
    sessions = None  # session list of the current in-use module, if any
    found_any = False
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r'Users of (\S+?):\s+\(Total of (\d+) licenses? issued;'
                     r'\s+Total of (\d+) licenses? in use\)', stripped)
        if m:
            found_any = True
            sessions = None
            if int(m.group(3)) > 0:
                sessions = []
                blocks.append((f"{m.group(1)}: {m.group(3)} of {m.group(2)} in use",
                               sessions))
            continue
        # Uncounted/node-locked features have no issued/in-use totals; list
        # them with whatever sessions follow.
        m = re.match(r'Users of (\S+?):\s+\(Uncounted', stripped)
        if m:
            found_any = True
            sessions = []
            blocks.append((f"{m.group(1)}: uncounted (node-locked)", sessions))
            continue
        if sessions is not None:
            # Session lines look like:
            #   "user host display (v6.4) (server/1718 101), start Mon 6/30 9:15, PID: 123"
            m = re.match(r'\s+(\S+)\s+(\S+)\s+.*,\s*start\s+([^,]+)', line)
            if m:
                sessions.append((m.group(1), m.group(2), m.group(3).strip()))

    if not found_any:
        return ''
    if not blocks:
        return "No COMSOL modules are currently checked out."

    lines_out = []
    for header, sess in blocks:
        shown = [s for s in sess if fnmatch.fnmatch(s[1].lower(), pattern.lower())]
        # A module whose session lines didn't parse is kept (header only), so
        # an in-use module never disappears silently.
        if shown or not sess:
            lines_out.append(header)
            lines_out += [f"    {user} on {host}  (since {since})"
                          for user, host, since in shown]
    if not lines_out:
        return f"No COMSOL modules are checked out by hosts matching '{pattern}'."
    return "COMSOL module usage (FlexNet):\n\n" + "\n".join(lines_out)


def query_license_usage() -> tuple[str, str]:
    """Ask the FlexNet (FNL) license server who currently holds seats of the
    COMSOL modules, by running lmstat from the local COMSOL installation.

    Returns (info_line, raw_lmstat_output); raw output is '' when the tool
    could not run at all, with the reason in info_line. The raw output is
    kept so the GUI can re-filter by host without re-querying the server.
    """
    found = find_lmstat()
    if found is None:
        return ("Neither lmstat.exe nor lmutil.exe found under "
                "Program Files\\COMSOL - install the COMSOL 'License Manager' "
                "component to get the FlexNet license tools.", '')
    cmd, license_file = found
    try:
        proc = subprocess.run(
            cmd + ['-a', '-c', str(license_file)],
            capture_output=True, text=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        return (f"lmstat failed to run: {e}", '')
    return (f"License file: {license_file}", proc.stdout + proc.stderr)
