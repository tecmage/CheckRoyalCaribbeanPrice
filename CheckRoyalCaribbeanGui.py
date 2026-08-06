#!/usr/bin/env python3
"""
Check Royal Caribbean GUI - one desktop window wrapping the command-line scripts.

Pick a script from the toolbar, pick a config tab along the bottom (one tab per
config.yaml, e.g. one per account), hit Run / Refresh, and watch the script's
colored output stream into the pane. Export any finished run to a standalone
HTML report.

Scripts run as child processes (python script.py -c config ...), so the GUI is
just a launcher + output viewer: nothing in the existing scripts changes, and
their sys.exit()/input() calls can't take the window down.

Frozen (PyInstaller) builds have no python interpreter to spawn, so the exe
re-executes itself with --run-script <name> and runs the bundled script
in-process in the child. See CheckRoyalCaribbeanGui.spec.
"""

import html
import json
import locale
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

##################################
# Paths
##################################

FROZEN = getattr(sys, 'frozen', False)

# Where the bundled/committed script .py files live
SCRIPT_DIR = getattr(sys, '_MEIPASS', None) if FROZEN else os.path.dirname(os.path.abspath(__file__))

# Where user-facing files (gui_settings.json, reports/, history_raw_*.json) go
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(BASE_DIR, 'gui_settings.json')
REPORTS_DIR = os.path.join(BASE_DIR, 'reports')

##################################
# ANSI handling
##################################

# Same pattern as StripAnsiFilter in CheckRoyalCaribbeanPrice.py
ANSI_REGEX = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
SGR_REGEX = re.compile(r'\x1B\[([0-9;]*)m')

# Foreground SGR code -> hex color, tuned for a dark background.
# Standard (30-37) and bright (90-97) map to the same readable shades.
COLOR_HEX = {
    30: '#808080', 90: '#808080',
    31: '#ff5f5f', 91: '#ff5f5f',
    32: '#5fff87', 92: '#5fff87',
    33: '#ffd75f', 93: '#ffff87',
    34: '#5fafff', 94: '#5fafff',
    35: '#ff87ff', 95: '#ff87ff',
    36: '#5fffff', 96: '#5fffff',
    37: '#d0d0d0', 97: '#ffffff',
}


def ansi_tokens(text, start_color=None):
    """
    Split *text* into [(segment, color_hex_or_None), ...] plus the color in
    effect at the end (so a color can carry across lines).
    Non-SGR escape sequences are stripped; SGR params other than reset and
    foreground colors (bold, backgrounds) are ignored.
    """
    tokens = []
    color = start_color
    pos = 0
    for m in ANSI_REGEX.finditer(text):
        if m.start() > pos:
            tokens.append((text[pos:m.start()], color))
        sgr = SGR_REGEX.fullmatch(m.group(0))
        if sgr:
            params = [int(p) for p in sgr.group(1).split(';') if p] or [0]
            for p in params:
                if p in (0, 39):   # full reset / default foreground
                    color = None
                elif p in COLOR_HEX:
                    color = COLOR_HEX[p]
        pos = m.end()
    if pos < len(text):
        tokens.append((text[pos:], color))
    return tokens, color


def ansi_to_html(lines):
    """Convert a list of raw (ANSI-colored) lines to HTML for a <pre> block."""
    out = []
    color = None
    for line in lines:
        tokens, color = ansi_tokens(line, color)
        for text, c in tokens:
            escaped = html.escape(text)
            if c:
                out.append(f'<span style="color:{c}">{escaped}</span>')
            else:
                out.append(escaped)
    return ''.join(out)


def build_html_report(title, meta_line, raw_lines):
    body = ansi_to_html(raw_lines)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{ background: #1e1e1e; color: #e0e0e0; font-family: Consolas, Menlo, monospace; margin: 1.5em; }}
h1 {{ font-size: 1.1em; border-bottom: 1px solid #444; padding-bottom: .4em; }}
pre {{ white-space: pre-wrap; word-wrap: break-word; font-size: 13px; line-height: 1.35; }}
</style></head><body>
<h1>{html.escape(meta_line)}</h1>
<pre>{body}</pre>
</body></html>
"""

##################################
# Script definitions
##################################


@dataclass
class Field:
    label: str
    flag: str            # CLI flag, e.g. '--ship'
    kind: str = 'text'   # text | int | float | choice | combo | check
    default: str = ''    # initial widget value ('' = omit flag)
    choices: tuple = ()  # for kind == 'choice' (readonly) / 'combo' (editable)
    required: bool = False
    width: int = 14
    # '' = static; 'ships_code' = fleet list as "Name (CODE)", code passed on;
    # 'ships_short' = fleet list as the short names Browse's -s matching expects
    dynamic: str = ''
    tip: str = ''        # hover tooltip


def tab_title(label, badge=''):
    """Padded notebook tab text; badge is '▶ ', '✓ ', '✖ ' or ''."""
    return f'    {badge}{label}    '


# Mirror BrowseRoyalCaribbeanPrice's locale setup: its -d matcher compares the
# argument verbatim against strftime('%x') under the user's locale, so the
# dropdown must render dates with the exact same locale in effect.
try:
    locale.setlocale(locale.LC_ALL, '')
except locale.Error:
    try:
        locale.setlocale(locale.LC_ALL, 'C')
    except locale.Error:
        pass


def sail_to_browse_display(sail):
    """
    Format a YYYYMMDD (or YYYY-MM-DD) sailDate exactly the way
    BrowseRoyalCaribbeanPrice's -d matcher expects: the first token of the
    locale's %x rendering (e.g. '08/22/2026' under en_US, not 'mm/dd/yy').
    """
    if not sail:
        return None
    s = str(sail).replace('-', '')
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return datetime.strptime(s, '%Y%m%d').strftime('%x').split(' ')[0]
    except ValueError:
        return None


def normalize_ship_name(name):
    """New ships arrive from the fleet API in ALL CAPS ('HERO OF THE SEAS') until
    someone fixes the casing; normalize to the display form the scripts match
    against ('Hero of the Seas' - "of the" stays lowercase because Browse's
    --ship matcher compares literally)."""
    name = name or ''
    if name.isupper():
        name = name.title()
    return name.replace(' Of The ', ' of the ')


def real_ships(fleet):
    """Fleet entries with display-normalized names; only empty-name rows drop.
    (All-caps entries used to be internal placeholders and were filtered out,
    but they are now real new ships - e.g. Hero of the Seas, Celebrity Compass
    - so their casing is normalized instead.)"""
    return [{**s, 'name': normalize_ship_name(s['name'])}
            for s in fleet if (s.get('name') or '')]


def short_ship_name(name):
    """BrowseRoyalCaribbeanPrice matches -s literally as '<arg> of the Seas' or
    'Celebrity <arg>', so its dropdown must hold the short name ('Ovation',
    'Apex'). Returns None for names neither pattern can ever match."""
    if not name:
        return None
    if name.endswith(' of the Seas'):
        return name[:-len(' of the Seas')]
    if name.startswith('Celebrity '):
        return name[len('Celebrity '):]
    return None


@dataclass
class ScriptDef:
    label: str
    filename: str
    uses_config: bool
    fields: list = field(default_factory=list)
    stdin_feed: str = ''   # written to the child's stdin before closing it
    terminal_ok: bool = False  # offer an "Open in Terminal" launch (Windows)


SCRIPTS = [
    ScriptDef('Price Checker', 'CheckRoyalCaribbeanPrice.py', uses_config=True),
    ScriptDef('Cabin Upgrades', 'CheckRoyalCaribbeanUpgrades.py', uses_config=True, fields=[
        Field('Reservations (comma-sep)', '--reservation', width=22,
              tip='Only check these reservation IDs, comma-separated. Blank = every booking.'),
        Field('Limit', '--limit', kind='int', width=6,
              tip='Stop after this many bookings (0 or blank = all).'),
        Field('Alert below $', '--alert-below', kind='float', width=8,
              tip='Send an Apprise alert when an upgrade for the whole cabin costs less than this.'),
    ]),
    ScriptDef('Casino Offers', 'CheckRoyalCaribbeanCasinoOffers.py', uses_config=True, fields=[
        Field('Warn days', '--warn-days', kind='int', default='14', width=6,
              tip='Highlight offers whose reserve-by deadline is within this many days.'),
    ]),
    ScriptDef('Cruise History', 'CheckRoyalCaribbeanCruiseHistory.py', uses_config=True, fields=[
        Field('Double-points IDs', '--double-points', width=22,
              tip='Booking IDs made during a double-points promo window, comma-separated. '
                  'The API has no booking date, so these must be supplied by you.'),
    ]),
    ScriptDef('Back-to-Back Cabins', 'FindBackToBackCabins.py', uses_config=False, fields=[
        # --ship and --type are required: without them the script falls back to
        # interactive pickers that are not isatty-gated (EOFError on closed stdin).
        # The ship combo is filled from the fleet API in the background (values
        # look like "Ovation of the Seas (OV)"; only the code is passed on).
        Field('Brand', '--brand', kind='choice', choices=('', 'ROYAL', 'CELEBRITY'), width=10,
              tip='Filters the ship list. Auto-detected from the ship when left blank.'),
        Field('Ship', '--ship', kind='combo', required=True, width=26, dynamic='ships_code',
              tip='Pick from the list, or type a code (OV) or any part of the name.'),
        Field('Type', '--type', default='all', required=True, width=18,
              tip="interior / oceanview / balcony / suite, comma-separated, or 'all' for every type."),
        Field('Category (e.g. 4D)', '--category', default='all', width=8,
              tip="Category code to match, e.g. 4D. 'all' = every category."),
        Field('Subtype letter (D = 1D,2D,4D…; prefer Category)', '--sub', width=5,
              tip='A subtype is the letter its categories share: D matches 1D, 2D, 4D… '
                  'Usually leave this blank and set Category instead.'),
        Field('Side', '--side', kind='choice', choices=('', 'any', 'port', 'starboard', 'both'), width=10,
              tip="Port/starboard preference. 'any' = no preference, 'both' = show each side separately."),
        Field('Decks (e.g. 7,8,9)', '--decks', width=10,
              tip='Comma-separated deck numbers to include; blank = any deck.'),
        Field('After (YYYY-MM-DD)', '--after', width=11,
              tip='Only sailings on or after this date.'),
        Field('Before (YYYY-MM-DD)', '--before', width=11,
              tip='Only sailings on or before this date.'),
        Field('Sail date (YYYY-MM-DD)', '--saildate', width=11,
              tip='Check one specific sailing (and the legs after it) instead of scanning the schedule.'),
        Field('Adults', '--adults', kind='int', width=4,
              tip='Guests used for the availability query (default 2).'),
        Field('Children', '--children', kind='int', width=4,
              tip='Children used for the availability query (default 0).'),
        Field('Min legs', '--min-legs', kind='int', width=4,
              tip='Minimum consecutive sailings a cabin must be open for (default 2).'),
        Field('Limit', '--limit', kind='int', width=4,
              tip='Max cabins listed per result (0 or blank = all).'),
        Field('Flip sides OK', '--flip-sides', kind='check',
              tip='Accept chains that switch between port and starboard mid-run.'),
        Field('Hide avoid-list', '--hide-avoid', kind='check',
              tip='Hide cabins that are on the known avoid list.'),
        Field('Hump only', '--hump-only', kind='check',
              tip='Only cabins in the hump area (extra-large balconies).'),
        Field('Connecting OK', '--connecting-permitted', kind='check',
              tip='Include connecting cabins (usually filtered out for noise).'),
    ]),
    ScriptDef('Cruise Planner Browser', 'BrowseRoyalCaribbeanPrice.py', uses_config=False,
              stdin_feed='\n', terminal_ok=True, fields=[
        # -s and -d are required to skip the interactive ship/sailing menus;
        # note -c here is CURRENCY, not config.
        Field('Ship', '-s', kind='combo', required=True, width=20, dynamic='ships_short',
              tip='Pick from the list. Choosing a ship loads its sailings into the Sail date box.'),
        Field('Sail date', '-d', kind='combo', required=True, width=11,
              tip='Pick a ship first and this fills with its actual sailings, in the exact '
                  'format the Browse script matches (your locale’s date format).'),
        Field('Currency', '-c', width=8,
              tip='Currency code, e.g. USD. Blank = your system setting.'),
        Field('Sort order', '-o', kind='choice', choices=('', 'asc', 'desc'), width=8,
              tip='Ascending or descending price sort.'),
        Field('Sort key', '-k', kind='choice', choices=('', 'price', 'alpha', 'default'), width=8,
              tip='Sort add-ons by price, alphabetically, or API order.'),
        Field('Activity sort', '-a', kind='choice', choices=('', 'date', 'alpha', 'default'), width=8,
              tip='Sort shore excursions/activities by date, alphabetically, or API order.'),
        Field('Show watchlist codes', '-w', kind='check',
              tip='Print the codes used for config.yaml watchlist entries.'),
    ]),
]

SCRIPT_WHITELIST = {s.filename for s in SCRIPTS}


TCL_MAX_MS = 2 ** 31 - 1   # Tk's after() takes a 32-bit millisecond count


def repeat_delay_ms(text):
    """Repeat-hours entry -> after() delay in ms, or None if not a positive number."""
    try:
        hours = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return min(int(hours * 3600 * 1000), TCL_MAX_MS)


def ship_code_from_display(val):
    """'Ovation of the Seas (OV)' -> 'OV'; anything else passes through stripped."""
    val = (val or '').strip()
    m = re.search(r'\(([A-Z0-9]{2,3})\)\s*$', val)
    return m.group(1) if m else val


def migrate_form_values(fv, script_labels, shared_scope='*shared*'):
    """
    Migrate the pre-per-tab form_values layout ({script: {flag: val}}) into the
    scoped layout ({scope: {script: {flag: val}}}). Only legacy script-label
    keys move (into the shared scope); existing path scopes stay untouched, so
    running this on an already-migrated or mixed file is a safe no-op for them.
    """
    if not isinstance(fv, dict):
        return {}
    legacy = {k: v for k, v in fv.items() if k in script_labels and isinstance(v, dict)}
    if not legacy:
        return fv
    out = {k: v for k, v in fv.items() if k not in legacy}
    shared = dict(out.get(shared_scope) or {})
    for k, v in legacy.items():
        shared.setdefault(k, v)
    out[shared_scope] = shared
    return out


def validate_values(sdef, raw):
    """
    Validate a plain {flag: value} dict against sdef's fields.
    Returns (values, None) or (None, error_message).
    """
    values = {}
    for f in sdef.fields:
        if f.flag not in raw:
            continue
        val = raw[f.flag]
        if f.kind == 'check':
            values[f.flag] = bool(val)
            continue
        val = str(val).strip()
        if f.dynamic == 'ships_code' and val:
            # dropdown entries look like "Ovation of the Seas (OV)" - pass the code
            val = ship_code_from_display(val)
        if f.required and not val:
            return None, f'"{f.label}" is required for {sdef.label}.'
        if val:
            if f.kind == 'int':
                try:
                    int(val)
                except ValueError:
                    return None, f'"{f.label}" must be a whole number.'
            elif f.kind == 'float':
                try:
                    float(val)
                except ValueError:
                    return None, f'"{f.label}" must be a number.'
        values[f.flag] = val
    return values, None


def build_cmd(sdef, config_path, values):
    """
    Build the child-process argv for *sdef*.
    *values* maps flag -> string value ('' = omit) or bool for checkboxes.
    """
    if FROZEN:
        cmd = [sys.executable, '--run-script', sdef.filename]
    else:
        cmd = [sys.executable, '-u', os.path.join(SCRIPT_DIR, sdef.filename)]
    if sdef.uses_config:
        cmd += ['-c', config_path]
    for f in sdef.fields:
        val = values.get(f.flag)
        if f.kind == 'check':
            if val:
                cmd.append(f.flag)
        elif val is not None and str(val).strip() != '':
            cmd += [f.flag, str(val).strip()]
    return cmd

##################################
# Frozen-exe child dispatch
##################################
# Must run before the tkinter import below so child processes never load Tk.
# A frozen GUI exe has no python interpreter to spawn, so build_cmd() re-executes
# the exe itself with --run-script <name>, and this block runs the bundled script.

if __name__ == '__main__' and len(sys.argv) > 2 and sys.argv[1] == '--run-script':
    _script_name = sys.argv[2]
    if _script_name not in SCRIPT_WHITELIST:
        sys.exit(f'Unknown script: {_script_name}')
    import runpy
    try:
        sys.stdout.reconfigure(line_buffering=True)  # frozen bootloader may ignore PYTHONUNBUFFERED
    except (AttributeError, OSError):
        pass
    sys.argv = [_script_name] + sys.argv[3:]
    runpy.run_path(os.path.join(SCRIPT_DIR, _script_name), run_name='__main__')
    sys.exit(0)

##################################
# Settings persistence
##################################


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_settings(data):
    """
    Atomic write: serialize first, then write a temp file and os.replace() it.
    A serialization error or a failed write must never truncate the existing
    settings file (open('w') + json.dump would).
    """
    try:
        payload = json.dumps(data, indent=2)
    except (TypeError, ValueError):
        return False
    tmp = SETTINGS_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(payload)
        os.replace(tmp, SETTINGS_FILE)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

##################################
# GUI
##################################

# Imported here (not needed by the --run-script child path on the happy import path,
# but harmless there too).
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont

DARK_BG = '#1e1e1e'    # output pane / notebook body
DARK_FG = '#e0e0e0'
PANEL_BG = '#252526'   # toolbars, forms, status bar
FIELD_BG = '#303030'   # entry/combobox interiors
BORDER = '#3c3c3c'
ACCENT = '#264f78'     # selection blue
TAB_SEL = '#094771'    # selected notebook tab


class Tooltip:
    """Hover tooltip: shows `text` in a small borderless window after a delay."""

    def __init__(self, widget, text, delay=600):
        self.widget, self.text, self.delay = widget, text, delay
        self.tip = None
        self.after_id = None
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    def _schedule(self, event=None):
        self._cancel()
        self.after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def _show(self):
        if self.tip or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f'+{x}+{y}')
        tk.Label(self.tip, text=self.text, justify='left', wraplength=380,
                 background=BORDER, foreground=DARK_FG,
                 relief='solid', borderwidth=1, padx=7, pady=4).pack()

    def _hide(self, event=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class ConfigTab:
    """One bottom tab: a config.yaml path (or None) plus its own output pane."""

    def __init__(self, notebook, config_path):
        self.config_path = config_path  # absolute path or None for "(no config)"
        self.raw_lines = []             # captured output with ANSI intact
        self.ansi_color = None          # color carried across appended lines
        self.last_run = None            # dict: script, config, started, ended, exit
        self.frame = ttk.Frame(notebook)
        self.text = tk.Text(self.frame, wrap='word', state='disabled',
                            bg=DARK_BG, fg=DARK_FG, insertbackground=DARK_FG,
                            borderwidth=0, padx=8, pady=6)
        scroll = ttk.Scrollbar(self.frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.text.pack(side='left', fill='both', expand=True)
        # one tag per distinct hex (several SGR codes share a shade)
        self._tag_by_hex = {}
        for code, hexcolor in COLOR_HEX.items():
            if hexcolor not in self._tag_by_hex:
                self.text.tag_configure(f'c_{code}', foreground=hexcolor)
                self._tag_by_hex[hexcolor] = f'c_{code}'
        self.text.tag_configure('find', background='#5f4b00')
        self.text.tag_configure('find_cur', background='#c58f00', foreground='#000000')
        self.text.tag_raise('find_cur')

    @property
    def label(self):
        if not self.config_path:
            return '(no config)'
        # Show the full filename: user configs are named e.g. config.yaml.jim,
        # so stripping the "extension" would hide the part that matters.
        return os.path.basename(self.config_path)

    def clear(self):
        self.raw_lines = []
        self.ansi_color = None
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.configure(state='disabled')

    MAX_RAW_LINES = 10000   # cap memory/export size for repeat runs without clear

    def append(self, line):
        self.append_many([line])

    def append_many(self, lines):
        if not lines:
            return
        self.raw_lines.extend(lines)
        if len(self.raw_lines) > self.MAX_RAW_LINES:
            del self.raw_lines[:len(self.raw_lines) - self.MAX_RAW_LINES]
        # only follow the tail if the user hasn't scrolled up to read something
        at_bottom = self.text.yview()[1] >= 0.999
        self.text.configure(state='normal')
        for line in lines:
            tokens, self.ansi_color = ansi_tokens(line, self.ansi_color)
            for text, color in tokens:
                tag = self._tag_by_hex.get(color)
                self.text.insert('end', text, tag if tag else ())
        self.text.configure(state='disabled')
        if at_bottom:
            self.text.see('end')


class App:
    POLL_MS = 100
    LINES_PER_TICK = 200
    KILL_GRACE_S = 3.0
    SHARED_SCOPE = '*shared*'

    def __init__(self, root):
        self.root = root
        root.title('Check Royal Caribbean')
        self.settings = load_settings()
        if not isinstance(self.settings, dict):
            self.settings = {}
        try:
            root.geometry(str(self.settings.get('geometry', '1050x720')))
        except tk.TclError:
            root.geometry('1050x720')

        self.proc = None
        self.q = queue.Queue()
        self.run_started = None
        self.stop_deadline = None
        self.running_tab = None
        self.field_vars = {}    # flag -> tk Variable, for the current script
        self.field_widgets = {} # flag -> widget, for the current script
        self._fleet = []        # [{code, name, brand}] cached from the fleet API
        self._fleet_loading = False

        # Saved form values: {scope: {script_label: {flag: value}}} where scope
        # is the tab's config path for config-using scripts (so each
        # config.yaml.person tab keeps its own reservations etc.) and
        # SHARED_SCOPE for config-less scripts (ship searches aren't per-account).
        self.form_values = migrate_form_values(
            self.settings.get('form_values', {}),
            {s.label for s in SCRIPTS}, self.SHARED_SCOPE)
        self.tabs = []
        self.run_queue = []        # tabs still to run for Run All Tabs
        self._queue_after_id = None
        self._queue_active = False
        self._stopped = False      # user hit Stop: suppress the repeat re-arm
        self._last_action = 'run'  # 'run' | 'run_all' — what the repeat timer redoes
        self._repeat_after_id = None
        self._ship_field = None
        self._fleet_failed_at = 0.0
        self._missing_configs = []  # configured paths that didn't exist at load
        self._sails_cache = {}     # ship code -> [mm/dd/yy] for the Browse -d combo
        self._sails_loading = set()

        self._apply_dark_theme()
        self._pick_font()
        self._build_toolbar()
        self._build_options_frame()
        self._build_findbar()
        self._build_statusbar()
        self._build_notebook()
        self._load_tabs_from_settings()
        self._on_script_change()

        root.protocol('WM_DELETE_WINDOW', self._on_close)
        root.bind('<Control-f>', lambda e: self._show_find())
        root.bind('<Control-equal>', lambda e: self._zoom(+1))
        root.bind('<Control-plus>', lambda e: self._zoom(+1))
        root.bind('<Control-minus>', lambda e: self._zoom(-1))
        root.bind('<Control-Key-0>', lambda e: self._zoom(0))
        root.bind('<Control-MouseWheel>', lambda e: self._zoom(+1 if e.delta > 0 else -1))
        root.bind('<Control-Button-4>', lambda e: self._zoom(+1))   # X11 wheel up
        root.bind('<Control-Button-5>', lambda e: self._zoom(-1))   # X11 wheel down
        root.after(self.POLL_MS, self._poll)

    # ---------- theme ----------

    def _apply_dark_theme(self):
        self.root.configure(bg=PANEL_BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('.', background=PANEL_BG, foreground=DARK_FG,
                        fieldbackground=FIELD_BG, bordercolor=BORDER,
                        lightcolor=PANEL_BG, darkcolor=PANEL_BG,
                        troughcolor=FIELD_BG, focuscolor=ACCENT,
                        selectbackground=ACCENT, selectforeground='#ffffff')
        style.configure('TLabelframe.Label', foreground='#9cdcfe')
        style.configure('TButton', background='#333333', padding=(9, 3))
        style.map('TButton',
                  background=[('disabled', '#2a2a2a'), ('active', '#3f3f3f')],
                  foreground=[('disabled', '#6a6a6a')])
        style.map('TCheckbutton', background=[('active', PANEL_BG)])
        style.configure('TEntry', insertcolor=DARK_FG)
        style.configure('TCombobox', arrowcolor=DARK_FG, background='#333333')
        style.map('TCombobox',
                  fieldbackground=[('readonly', FIELD_BG), ('disabled', '#2a2a2a')],
                  foreground=[('disabled', '#6a6a6a')],
                  selectbackground=[('readonly', FIELD_BG)],
                  selectforeground=[('readonly', DARK_FG)])
        style.configure('TNotebook', background=DARK_BG, tabposition='sw')
        style.configure('TNotebook.Tab', background='#2d2d2d', foreground='#b0b0b0',
                        padding=(10, 4))
        style.map('TNotebook.Tab',
                  background=[('selected', TAB_SEL)],
                  foreground=[('selected', '#ffffff')])
        style.configure('Vertical.TScrollbar', background=BORDER,
                        troughcolor=DARK_BG, arrowcolor=DARK_FG)
        for opt, val in (('background', FIELD_BG), ('foreground', DARK_FG),
                         ('selectBackground', ACCENT), ('selectForeground', '#ffffff')):
            self.root.option_add(f'*TCombobox*Listbox.{opt}', val)

    def _make_menu(self):
        return tk.Menu(self.root, tearoff=0, background=PANEL_BG, foreground=DARK_FG,
                       activebackground=ACCENT, activeforeground='#ffffff',
                       borderwidth=0)

    # ---------- construction ----------

    def _pick_font(self):
        families = set(tkfont.families())
        for name in ('Consolas', 'Cascadia Mono', 'Menlo', 'DejaVu Sans Mono', 'Courier New'):
            if name in families:
                family = name
                break
        else:
            family = tkfont.nametofont('TkFixedFont').actual('family')
        try:
            size = max(6, min(24, int(self.settings.get('font_size', 10))))
        except (TypeError, ValueError):
            size = 10
        # one shared Font object: resizing it restyles every tab's output pane
        self.mono = tkfont.Font(family=family, size=size)

    def _zoom(self, step):
        size = 10 if step == 0 else max(6, min(24, self.mono['size'] + step))
        self.mono.configure(size=size)
        self.settings['font_size'] = size

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(8, 6, 8, 2))
        bar.pack(side='top', fill='x')
        ttk.Label(bar, text='Script:').pack(side='left')
        self.script_var = tk.StringVar(value=self.settings.get('last_script', SCRIPTS[0].label))
        if self.script_var.get() not in [s.label for s in SCRIPTS]:
            self.script_var.set(SCRIPTS[0].label)
        self.script_combo = ttk.Combobox(bar, textvariable=self.script_var, state='readonly',
                                         values=[s.label for s in SCRIPTS], width=24)
        self.script_combo.pack(side='left', padx=(4, 10))
        self.script_combo.bind('<<ComboboxSelected>>', lambda e: self._on_script_change())
        self.run_btn = ttk.Button(bar, text='Run / Refresh', command=self.run_clicked)
        self.run_btn.pack(side='left', padx=2)
        self.runall_btn = ttk.Button(bar, text='Run All Tabs', command=self.run_all_clicked)
        self.runall_btn.pack(side='left', padx=2)
        self.stop_btn = ttk.Button(bar, text='Stop', command=self.stop_clicked, state='disabled')
        self.stop_btn.pack(side='left', padx=2)
        self.export_btn = ttk.Button(bar, text='Export HTML', command=self.export_clicked)
        self.export_btn.pack(side='left', padx=(10, 2))

        bar2 = ttk.Frame(self.root, padding=(8, 0, 8, 2))
        bar2.pack(side='top', fill='x')
        self.toolbar2 = bar2
        self.clear_var = tk.BooleanVar(value=self.settings.get('clear_before_run', True))
        ttk.Checkbutton(bar2, text='Clear before run', variable=self.clear_var).pack(side='left', padx=(0, 12))
        self.auto_export_var = tk.BooleanVar(value=self.settings.get('auto_export', False))
        ttk.Checkbutton(bar2, text='Auto-export HTML', variable=self.auto_export_var).pack(side='left', padx=(0, 12))
        self.repeat_var = tk.BooleanVar(value=self.settings.get('repeat_enabled', False))
        ttk.Checkbutton(bar2, text='Repeat every', variable=self.repeat_var,
                        command=self._on_repeat_toggle).pack(side='left')
        self.repeat_hours_var = tk.StringVar(value=str(self.settings.get('repeat_hours', '12')))
        ttk.Entry(bar2, textvariable=self.repeat_hours_var, width=5).pack(side='left', padx=3)
        ttk.Label(bar2, text='hours').pack(side='left')

    def _build_options_frame(self):
        self.opts_outer = ttk.LabelFrame(self.root, text='Options', padding=(8, 4))
        # packed/unpacked in _on_script_change depending on whether there are fields

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value='Ready')
        bar = ttk.Frame(self.root)
        bar.pack(side='bottom', fill='x')
        ttk.Label(bar, textvariable=self.status_var, padding=(8, 2)).pack(side='left')

    def _build_findbar(self):
        self.find_frame = ttk.Frame(self.root, padding=(8, 3))
        ttk.Label(self.find_frame, text='Find:').pack(side='left')
        self.find_var = tk.StringVar()
        self.find_var.trace_add('write', lambda *a: self._refresh_find())
        self.find_entry = ttk.Entry(self.find_frame, textvariable=self.find_var, width=30)
        self.find_entry.pack(side='left', padx=4)
        self.find_entry.bind('<Return>', lambda e: self._find_step(+1))
        self.find_entry.bind('<Shift-Return>', lambda e: self._find_step(-1))
        self.find_entry.bind('<Escape>', lambda e: self._hide_find())
        ttk.Button(self.find_frame, text='‹', width=2,
                   command=lambda: self._find_step(-1)).pack(side='left')
        ttk.Button(self.find_frame, text='›', width=2,
                   command=lambda: self._find_step(+1)).pack(side='left', padx=(2, 8))
        self.find_count = ttk.Label(self.find_frame, text='')
        self.find_count.pack(side='left')
        ttk.Button(self.find_frame, text='✕', width=2,
                   command=self._hide_find).pack(side='right')
        self._find_visible = False
        self._find_matches = []
        self._find_index = 0

    def _build_notebook(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(side='top', fill='both', expand=True, padx=4, pady=(2, 0))
        self.plus_frame = ttk.Frame(self.nb)
        self.nb.add(self.plus_frame, text=' + ')
        self.nb.bind('<<NotebookTabChanged>>', self._on_tab_changed)
        self.nb.bind('<Button-3>', self._on_tab_rightclick)
        self._last_real_tab = None

    # ---------- find bar ----------

    def _show_find(self):
        if not self._find_visible:
            self.find_frame.pack(side='bottom', fill='x')
            self._find_visible = True
        self.find_entry.focus_set()
        self.find_entry.select_range(0, 'end')
        self._refresh_find()

    def _clear_find_tags(self):
        """Remove find highlights from every tab, not just the current one."""
        for t in self.tabs:
            t.text.tag_remove('find', '1.0', 'end')
            t.text.tag_remove('find_cur', '1.0', 'end')

    def _hide_find(self):
        if self._find_visible:
            self.find_frame.pack_forget()
            self._find_visible = False
        self._clear_find_tags()
        self.root.focus_set()

    def _refresh_find(self):
        if not self._find_visible:
            return
        tab = self.current_tab()
        if tab is None:
            return
        self._clear_find_tags()
        text = tab.text
        self._find_matches = []
        self._find_index = 0
        term = self.find_var.get()
        if term:
            pos = '1.0'
            count = tk.IntVar()
            while len(self._find_matches) < 5000:
                pos = text.search(term, pos, stopindex='end', nocase=1, count=count)
                if not pos:
                    break
                end = f'{pos}+{count.get()}c'
                text.tag_add('find', pos, end)
                self._find_matches.append((pos, end))
                pos = end
        n = len(self._find_matches)
        self.find_count.configure(text=f'{n} match{"es" if n != 1 else ""}' if term else '')
        if self._find_matches:
            self._mark_current_find(scroll=False)

    def _mark_current_find(self, scroll=True):
        tab = self.current_tab()
        if tab is None or not self._find_matches:
            return
        tab.text.tag_remove('find_cur', '1.0', 'end')
        pos, end = self._find_matches[self._find_index]
        tab.text.tag_add('find_cur', pos, end)
        self.find_count.configure(
            text=f'{self._find_index + 1} of {len(self._find_matches)}')
        if scroll:
            tab.text.see(pos)

    def _find_step(self, delta):
        if not self._find_matches:
            self._refresh_find()
            if not self._find_matches:
                return
        else:
            self._find_index = (self._find_index + delta) % len(self._find_matches)
        self._mark_current_find()

    # ---------- output context menu ----------

    def _output_menu(self, event, tab):
        menu = self._make_menu()
        has_sel = bool(tab.text.tag_ranges('sel'))
        menu.add_command(label='Copy', state='normal' if has_sel else 'disabled',
                         command=lambda: tab.text.event_generate('<<Copy>>'))
        menu.add_command(label='Select All',
                         command=lambda: (tab.text.tag_add('sel', '1.0', 'end-1c'),
                                          tab.text.focus_set()))
        menu.add_separator()
        menu.add_command(label='Find…\tCtrl+F', command=self._show_find)
        menu.add_separator()
        menu.add_command(label='Export HTML…', command=self.export_clicked)
        menu.add_command(label='Open reports folder', command=self._open_reports_folder)
        menu.add_separator()
        menu.add_command(label='Clear output', command=lambda: self._clear_tab(tab))
        menu.tk_popup(event.x_root, event.y_root)

    def _clear_tab(self, tab):
        tab.clear()
        if self._find_visible:
            self._refresh_find()   # drop match ranges that pointed into cleared text

    def _ensure_reports_dir(self):
        try:
            os.makedirs(REPORTS_DIR, exist_ok=True)
            return True
        except OSError as exc:
            messagebox.showerror('Check Royal Caribbean',
                                 f'Could not create {REPORTS_DIR}:\n{exc}')
            return False

    def _open_reports_folder(self):
        if not self._ensure_reports_dir():
            return
        try:
            if os.name == 'nt':
                os.startfile(REPORTS_DIR)
            else:
                subprocess.Popen(['xdg-open', REPORTS_DIR])
        except OSError as exc:
            messagebox.showerror('Check Royal Caribbean', f'Could not open {REPORTS_DIR}:\n{exc}')

    # ---------- config tabs ----------

    def _load_tabs_from_settings(self):
        configs = self.settings.get('configs', [])
        for path in (configs if isinstance(configs, list) else []):
            if isinstance(path, str) and os.path.isfile(path):
                self._add_tab(path, select=False)
            elif isinstance(path, str):
                # remember it (an unmounted drive is not a reason to forget the
                # config forever) but don't build a dead tab for it
                self._missing_configs.append(path)
        if not self.tabs:
            default = os.path.join(BASE_DIR, 'config.yaml')
            if os.path.isfile(default):
                self._add_tab(default, select=False)
        if not self.tabs:
            self._add_tab(None, select=False)  # placeholder for config-less scripts
        active = self.settings.get('active_config')
        tab = next((t for t in self.tabs if t.config_path and active
                    and os.path.normcase(t.config_path) == os.path.normcase(str(active))),
                   self.tabs[0])
        self.nb.select(tab.frame)
        if self._missing_configs:
            names = ', '.join(os.path.basename(p) for p in self._missing_configs)
            self.status_var.set(f'Config(s) not found (kept in settings): {names}')

    def _add_tab(self, config_path, select=True):
        tab = ConfigTab(self.nb, config_path)
        tab.text.configure(font=self.mono)
        tab.text.bind('<Button-3>', lambda e, t=tab: self._output_menu(e, t))
        insert_at = len(self.tabs)  # before the '+' tab
        self.nb.insert(insert_at, tab.frame, text=tab_title(tab.label))
        self.tabs.append(tab)
        if config_path is None:
            tab.append('No config.yaml selected. Use the  +  tab to add one,\n'
                       'or run a script that needs no config (Back-to-Back Cabins, '
                       'Cruise Planner Browser).\n')
        if select:
            self.nb.select(tab.frame)
        return tab

    def _remove_placeholder_if_any(self):
        for tab in list(self.tabs):
            if (tab.config_path is None and len(self.tabs) > 1
                    and self.running_tab is not tab):
                self.nb.forget(tab.frame)
                self.tabs.remove(tab)

    def current_tab(self):
        sel = self.nb.select()
        for tab in self.tabs:
            if str(tab.frame) == sel:
                return tab
        return self.tabs[0] if self.tabs else None

    def _on_tab_changed(self, event):
        sel = self.nb.select()
        if sel == str(self.plus_frame):
            # revert selection first so a cancelled dialog leaves us somewhere sane
            if self._last_real_tab in self.tabs:
                self.nb.select(self._last_real_tab.frame)
            elif self.tabs:
                self.nb.select(self.tabs[0].frame)
            self._add_tab_dialog()
        else:
            for tab in self.tabs:
                if str(tab.frame) == sel:
                    if tab is not self._last_real_tab:
                        # save the outgoing tab's form values, then rebuild the
                        # form so this tab's own saved values appear
                        if self._last_real_tab in self.tabs:
                            self._stash_form_values(tab=self._last_real_tab)
                        self._last_real_tab = tab
                        self._on_script_change()
                        if self._find_visible:
                            self._refresh_find()
                    break

    def _add_tab_dialog(self):
        path = filedialog.askopenfilename(
            title='Choose a config.yaml',
            initialdir=BASE_DIR,
            filetypes=[('Config files', 'config.yaml* *.yaml *.yml'), ('All files', '*.*')])
        if not path:
            return
        path = os.path.abspath(path)
        for tab in self.tabs:
            if tab.config_path and os.path.normcase(tab.config_path) == os.path.normcase(path):
                self.nb.select(tab.frame)
                return
        tab = self._add_tab(path)
        self._remove_placeholder_if_any()
        self._save_settings()
        return tab

    def _on_tab_rightclick(self, event):
        try:
            idx = self.nb.index(f'@{event.x},{event.y}')
        except tk.TclError:
            return
        if idx >= len(self.tabs):
            return  # the '+' tab
        tab = self.tabs[idx]
        menu = self._make_menu()
        if tab.config_path:
            menu.add_command(label='Open config in editor',
                             command=lambda: self._open_in_editor(tab.config_path))
        menu.add_command(label='Remove tab', command=lambda: self._remove_tab(tab))
        menu.tk_popup(event.x_root, event.y_root)

    def _open_in_editor(self, path):
        try:
            if os.name == 'nt':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', path])
        except OSError as exc:
            messagebox.showerror('Check Royal Caribbean', f'Could not open {path}:\n{exc}')

    def _remove_tab(self, tab):
        if self.running_tab is tab:
            messagebox.showwarning('Check Royal Caribbean', 'A script is running in this tab. Stop it first.')
            return
        if tab in self.run_queue:
            self.run_queue.remove(tab)
        if tab is self._last_real_tab:
            # clear BEFORE forget so the resulting tab-change event can't stash
            # the removed tab's on-screen values under the newly selected tab
            self._last_real_tab = None
        self.nb.forget(tab.frame)
        self.tabs.remove(tab)
        if tab.config_path:
            self.form_values.pop(tab.config_path, None)   # prune its saved forms
        if not self.tabs:
            self._add_tab(None, select=True)
        self._save_settings()

    # ---------- options form ----------

    def current_script(self):
        label = self.script_var.get()
        for s in SCRIPTS:
            if s.label == label:
                return s
        return SCRIPTS[0]

    def _on_script_change(self):
        sdef = self.current_script()
        for child in self.opts_outer.winfo_children():
            child.destroy()
        self.field_vars = {}
        self.field_widgets = {}
        self._ship_field = None
        if not sdef.fields and not sdef.terminal_ok:
            self.opts_outer.pack_forget()
            return
        self.opts_outer.pack(side='top', fill='x', padx=8, pady=(0, 2),
                             after=self.toolbar2)
        scope = self._form_scope(sdef)
        saved = (self.form_values.get(scope, {}).get(sdef.label)
                 or self.form_values.get(self.SHARED_SCOPE, {}).get(sdef.label, {}))
        col = 0
        row = 0
        max_cols = 8
        for f in sdef.fields:
            if col >= max_cols:
                col = 0
                row += 1
            if f.kind == 'check':
                var = tk.BooleanVar(value=bool(saved.get(f.flag, False)))
                w = ttk.Checkbutton(self.opts_outer, text=f.label, variable=var)
                w.grid(row=row, column=col, columnspan=2, sticky='w', padx=(0, 10), pady=2)
                col += 2
                if f.tip:
                    Tooltip(w, f.tip)
            else:
                label_txt = f.label + (' *' if f.required else '')
                lbl = ttk.Label(self.opts_outer, text=label_txt)
                lbl.grid(row=row, column=col, sticky='e', padx=(0, 3), pady=2)
                var = tk.StringVar(value=str(saved.get(f.flag, f.default)))
                if f.kind == 'choice':
                    w = ttk.Combobox(self.opts_outer, textvariable=var,
                                     values=list(f.choices), width=f.width, state='readonly')
                elif f.kind == 'combo':
                    w = ttk.Combobox(self.opts_outer, textvariable=var,
                                     values=list(f.choices), width=f.width)
                else:
                    w = ttk.Entry(self.opts_outer, textvariable=var, width=f.width)
                w.grid(row=row, column=col + 1, sticky='w', padx=(0, 12), pady=2)
                col += 2
                self.field_widgets[f.flag] = w
                if f.tip:
                    Tooltip(lbl, f.tip)
                    Tooltip(w, f.tip)
            self.field_vars[f.flag] = var
        if sdef.terminal_ok and os.name == 'nt':
            ttk.Button(self.opts_outer, text='Open in Terminal (interactive)',
                       command=self._open_in_terminal).grid(
                row=row + 1, column=0, columnspan=4, sticky='w', pady=(4, 2))
        self._setup_ship_dropdown()

    # ---------- fleet dropdown (Back-to-Back Cabins) ----------

    def _setup_ship_dropdown(self):
        """If the current form has a fleet-backed ship combo, fill it."""
        sdef = self.current_script()
        self._ship_field = next((f for f in sdef.fields if f.dynamic), None)
        if self._ship_field is None:
            return
        brand_w = self.field_widgets.get('--brand')
        if brand_w is not None:
            brand_w.bind('<<ComboboxSelected>>', lambda e: self._apply_fleet_filter(), add='+')
        # Browse: picking a ship loads its sailings into the -d combo
        if self._ship_field.dynamic == 'ships_short' and '-d' in self.field_widgets:
            ship_w = self.field_widgets.get(self._ship_field.flag)
            if ship_w is not None:
                ship_w.bind('<<ComboboxSelected>>', lambda e: self._on_browse_ship_pick(), add='+')
                if self.field_vars[self._ship_field.flag].get().strip():
                    self._on_browse_ship_pick()   # prefill for a remembered ship
        if self._fleet:
            self._apply_fleet_filter()
        elif (not self._fleet_loading
                and time.monotonic() - self._fleet_failed_at > 60):
            # 60 s backoff after a failed fetch so an offline session doesn't
            # relaunch a thread on every script/tab switch
            self._fleet_loading = True
            self.status_var.set('Loading ship list…')
            threading.Thread(target=self._fetch_fleet, daemon=True).start()

    def _fetch_fleet(self):
        """Worker thread: fetch [{code, name, brand}] via FindBackToBackCabins."""
        err = ''
        try:
            if SCRIPT_DIR not in sys.path:
                sys.path.insert(0, SCRIPT_DIR)
            import FindBackToBackCabins as b2b
            fleet = real_ships(b2b.get_fleet())
        except Exception as exc:
            fleet = []
            err = str(exc)
        self.q.put(('fleet', (fleet, err)))

    def _apply_fleet_filter(self):
        f = getattr(self, '_ship_field', None)
        ship_w = self.field_widgets.get(f.flag) if f else None
        if not (isinstance(ship_w, ttk.Combobox) and ship_w.winfo_exists() and self._fleet):
            return
        brand_var = self.field_vars.get('--brand')
        want = {'ROYAL': 'R', 'CELEBRITY': 'C'}.get(brand_var.get() if brand_var else '')
        ships = [s for s in self._fleet if not want or s['brand'] == want]
        if f.dynamic == 'ships_short':
            ship_w['values'] = [n for n in (short_ship_name(s['name']) for s in ships) if n]
        else:
            ship_w['values'] = [f"{s['name']} ({s['code']})" for s in ships]

    def _browse_ship_code(self):
        """Ship code for the short name currently picked in Browse's -s combo."""
        var = self.field_vars.get('-s')
        short = var.get().strip() if var else ''
        return next((s['code'] for s in self._fleet
                     if short_ship_name(s['name']) == short), None)

    def _on_browse_ship_pick(self):
        code = self._browse_ship_code()
        d_w = self.field_widgets.get('-d')
        if not code or not isinstance(d_w, ttk.Combobox):
            return
        if code in self._sails_cache:
            d_w['values'] = self._sails_cache[code]
            return
        if code in self._sails_loading:
            return
        self._sails_loading.add(code)
        if self.proc is None:
            self.status_var.set('Loading sailings…')
        threading.Thread(target=self._fetch_sails, args=(code,), daemon=True).start()

    def _fetch_sails(self, code):
        """Worker thread: sailing dates for one ship, as mm/dd/yy."""
        try:
            if SCRIPT_DIR not in sys.path:
                sys.path.insert(0, SCRIPT_DIR)
            import FindBackToBackCabins as b2b
            dates = [d for d in (sail_to_browse_display(v.get('sailDate'))
                                 for v in b2b.get_voyages(code)) if d]
        except Exception:
            dates = []
        self.q.put(('sails', (code, dates)))

    def _collect_values(self, sdef):
        """Read the form; returns (values, error_message_or_None)."""
        raw = {f.flag: self.field_vars[f.flag].get()
               for f in sdef.fields if f.flag in self.field_vars}
        return validate_values(sdef, raw)

    def _form_scope(self, sdef, tab=None):
        """Which form_values bucket this script's values live in for a tab."""
        if sdef.uses_config:
            tab = tab or self.current_tab()
            if tab is not None and tab.config_path:
                return tab.config_path
        return self.SHARED_SCOPE

    def _stash_form_values(self, tab=None):
        sdef = self.current_script()
        if self.field_vars:
            scope = self._form_scope(sdef, tab)
            self.form_values.setdefault(scope, {})[sdef.label] = {
                flag: var.get() for flag, var in self.field_vars.items()}

    # ---------- running ----------

    def _abort_queue(self):
        """Cancel any Run-All state: pending queued start, queue, active flag."""
        if self._queue_after_id is not None:
            self.root.after_cancel(self._queue_after_id)
            self._queue_after_id = None
        self.run_queue = []
        self._queue_active = False

    def run_clicked(self):
        if self.proc is not None:
            return
        self._cancel_repeat()
        self._stopped = False
        if not self._queue_active:
            self._last_action = 'run'
        sdef = self.current_script()
        tab = self.current_tab()
        if tab is None:
            self._abort_queue()
            return
        if sdef.uses_config:
            if not tab.config_path:
                self._abort_queue()
                messagebox.showinfo('Check Royal Caribbean',
                                    f'{sdef.label} needs a config.yaml.\n'
                                    'Add one with the  +  tab at the bottom.')
                return
            if not os.path.isfile(tab.config_path):
                self._abort_queue()
                messagebox.showerror('Check Royal Caribbean',
                                     f'Config file not found:\n{tab.config_path}')
                return
        values, err = self._collect_values(sdef)
        if err:
            self._abort_queue()
            messagebox.showerror('Check Royal Caribbean', err)
            return
        self._stash_form_values()
        cmd = build_cmd(sdef, tab.config_path, values)
        if self.clear_var.get():
            tab.clear()
        tab.last_run = {
            'script': sdef.label,
            'config': os.path.basename(tab.config_path) if (sdef.uses_config and tab.config_path) else '',
            'started': datetime.now(),
            'ended': None,
            'exit': None,
        }
        env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
        creationflags = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                cwd=BASE_DIR, env=env, creationflags=creationflags)
        except OSError as exc:
            self.proc = None
            self._abort_queue()   # a failed start must not strand the Run-All queue
            messagebox.showerror('Check Royal Caribbean', f'Could not start {sdef.filename}:\n{exc}')
            return
        try:
            if sdef.stdin_feed:
                self.proc.stdin.write(sdef.stdin_feed)
                self.proc.stdin.flush()
        except OSError:
            pass
        try:
            self.proc.stdin.close()  # children see isatty() == False
        except OSError:
            pass
        self.run_started = time.monotonic()
        self.stop_deadline = None
        self.running_tab = tab
        self._set_badge(tab, '▶ ')
        self.run_btn.configure(state='disabled')
        self.runall_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.script_combo.configure(state='disabled')
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def run_all_clicked(self):
        """Run the selected script once per config tab, sequentially."""
        if self.proc is not None:
            return
        self._cancel_repeat()
        self._stopped = False
        self._last_action = 'run_all'
        sdef = self.current_script()
        if not sdef.uses_config:
            self.run_clicked()
            return
        self._stash_form_values()
        runnable = [t for t in self.tabs if t.config_path and os.path.isfile(t.config_path)]
        missing = [t for t in self.tabs if t.config_path and not os.path.isfile(t.config_path)]
        if not runnable:
            messagebox.showinfo('Check Royal Caribbean', 'No config tabs to run. Add one with the  +  tab.')
            return
        if missing:
            self.status_var.set('Skipping missing config(s): '
                                + ', '.join(t.label for t in missing))
        self.run_queue = runnable
        self._queue_active = True
        self._run_next_in_queue()

    def _run_next_in_queue(self):
        if not self.run_queue:
            return
        tab = self.run_queue.pop(0)
        # Swap tabs synchronously (stash outgoing values, rebuild the form for
        # this tab) so the queued run can't race the async tab-change event and
        # start with the previous tab's form values.
        if self._last_real_tab in self.tabs and self._last_real_tab is not tab:
            self._stash_form_values(tab=self._last_real_tab)
        self._last_real_tab = tab
        self.nb.select(tab.frame)
        self._on_script_change()
        self._queue_after_id = self.root.after(50, self._queue_fire)

    def _queue_fire(self):
        self._queue_after_id = None
        self.run_clicked()

    def _reader(self, proc):
        """Worker thread: pipe -> queue only. Never touches widgets."""
        try:
            for line in proc.stdout:
                self.q.put(('line', line))
        except (OSError, ValueError):
            pass
        self.q.put(('done', proc.wait()))

    def stop_clicked(self):
        self._stopped = True   # also suppresses the repeat re-arm in _finish_run
        self._abort_queue()
        self._cancel_repeat()
        if self.proc is None:
            return
        try:
            self.proc.terminate()
        except OSError:
            pass
        self.stop_deadline = time.monotonic() + self.KILL_GRACE_S
        self.status_var.set('Stopping…')

    def _poll(self):
        # One exception must never kill the poll loop (it would freeze the GUI
        # for the rest of the session), so the body is guarded and the re-arm
        # lives in a finally.
        try:
            self._poll_body()
        except Exception as exc:
            self.status_var.set(f'Internal error: {exc}')
        finally:
            self.root.after(self.POLL_MS, self._poll)

    def _poll_body(self):
        tab = self.running_tab
        done_code = None
        pending_lines = []
        for _ in range(self.LINES_PER_TICK):
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == 'line':
                pending_lines.append(payload)
            elif kind == 'done':
                done_code = payload
            elif kind == 'fleet':
                self._fleet_loading = False
                fleet, err = payload
                self._fleet = fleet or []
                if not fleet:
                    self._fleet_failed_at = time.monotonic()
                if self.proc is None:
                    self.status_var.set(
                        'Ship list loaded' if fleet else
                        f'Could not load ship list ({err or "empty response"}) '
                        '— type a name or code')
                self._apply_fleet_filter()
            elif kind == 'sails':
                s_code, dates = payload
                self._sails_loading.discard(s_code)
                if dates:
                    self._sails_cache[s_code] = dates   # never cache a failure
                d_w = self.field_widgets.get('-d')
                if isinstance(d_w, ttk.Combobox) and d_w.winfo_exists() \
                        and self._browse_ship_code() == s_code:
                    d_w['values'] = dates
                    if self.proc is None:
                        self.status_var.set(f'{len(dates)} sailings loaded' if dates else
                                            'Could not load sailings — type a date manually')
        if pending_lines and tab is not None:
            tab.append_many(pending_lines)
            if self._find_visible and tab is self.current_tab():
                self._refresh_find()
        if self.proc is not None and done_code is None:
            if self.stop_deadline and time.monotonic() > self.stop_deadline:
                try:
                    self.proc.kill()
                except OSError:
                    pass
                self.stop_deadline = None
            elapsed = int(time.monotonic() - self.run_started)
            sdef = self.current_script()
            self.status_var.set(
                f'Running {sdef.label}'
                + (f' — {tab.label}' if tab and tab.config_path else '')
                + f'…  {elapsed // 60}m{elapsed % 60:02d}s')
        if done_code is not None:
            self._finish_run(done_code)

    def _finish_run(self, code):
        tab = self.running_tab
        elapsed = int(time.monotonic() - self.run_started) if self.run_started else 0
        dur = f'{elapsed // 60}m{elapsed % 60:02d}s'
        if tab is not None and tab.last_run:
            tab.last_run['ended'] = datetime.now()
            tab.last_run['exit'] = code
            tab.append(f'\n[finished — exit code {code} — {dur}]\n')
        status = (f'Done (exit {code}, {dur})' if code == 0
                  else f'Finished with exit code {code} ({dur})')
        if tab is not None:
            self._set_badge(tab, '✓ ' if code == 0 else '✖ ')
            if self.auto_export_var.get() and tab.raw_lines:
                path = self._auto_export(tab)
                if path:
                    status += f' — saved {os.path.basename(path)}'
        self.status_var.set(status)
        self.proc = None
        self.running_tab = None
        self.run_started = None
        self.stop_deadline = None
        self.run_btn.configure(state='normal')
        self.runall_btn.configure(state='normal')
        self.stop_btn.configure(state='disabled')
        self.script_combo.configure(state='readonly')
        if self.run_queue:
            self._run_next_in_queue()
        else:
            self._queue_active = False
            if not self._stopped:   # Stop means stop: no repeat re-arm
                self._schedule_repeat()

    def _set_badge(self, tab, badge=''):
        if tab in self.tabs:
            self.nb.tab(tab.frame, text=tab_title(tab.label, badge))

    # ---------- repeat timer ----------

    def _on_repeat_toggle(self):
        if not self.repeat_var.get():
            self._cancel_repeat()
            self.status_var.set('Repeat off')
        elif repeat_delay_ms(self.repeat_hours_var.get()) is None:
            self.status_var.set('Repeat: hours must be a positive number')
        elif self.proc is None and self._repeat_after_id is None:
            self.status_var.set('Repeat armed — starts counting after the next run')

    def _cancel_repeat(self):
        if self._repeat_after_id is not None:
            self.root.after_cancel(self._repeat_after_id)
            self._repeat_after_id = None

    def _schedule_repeat(self):
        self._cancel_repeat()
        if not self.repeat_var.get():
            return
        ms = repeat_delay_ms(self.repeat_hours_var.get())
        if ms is None:
            self.status_var.set('Repeat: hours must be a positive number — not scheduled')
            return
        try:
            self._repeat_after_id = self.root.after(ms, self._repeat_fire)
        except tk.TclError:
            return
        nxt = datetime.now() + timedelta(milliseconds=ms)
        self.status_var.set(self.status_var.get() + f'  —  next run at {nxt:%H:%M}')

    def _repeat_fire(self):
        self._repeat_after_id = None
        if self.proc is not None:
            # busy (e.g. a manual run overlaps the boundary): retry in a minute
            # instead of silently dropping the repeat for the rest of the session
            self._repeat_after_id = self.root.after(60_000, self._repeat_fire)
            return
        if self._last_action == 'run_all':
            self.run_all_clicked()
        else:
            self.run_clicked()

    def _open_in_terminal(self):
        """Windows nicety: launch the browser script interactively in a console."""
        sdef = self.current_script()
        if FROZEN:
            # A windowed exe has no usable console stdio, so interactive mode
            # needs the standalone console build if it's next to this exe.
            exe = os.path.join(BASE_DIR, os.path.splitext(sdef.filename)[0] + '.exe')
            if not os.path.isfile(exe):
                messagebox.showinfo(
                    'Check Royal Caribbean',
                    f'Interactive mode needs {os.path.basename(exe)} in the same '
                    'folder as this program (build it from '
                    f'{os.path.splitext(sdef.filename)[0]}.spec).')
                return
            cmd = [exe]
        else:
            cmd = [sys.executable, os.path.join(SCRIPT_DIR, sdef.filename)]
        try:
            subprocess.Popen(cmd, cwd=BASE_DIR,
                             creationflags=0x00000010)  # CREATE_NEW_CONSOLE
        except OSError as exc:
            messagebox.showerror('Check Royal Caribbean', f'Could not open terminal:\n{exc}')

    # ---------- export ----------

    def _default_report_name(self, tab):
        run = tab.last_run or {}
        started = run.get('started')
        name = 'Report_' + re.sub(r'\W+', '', run.get('script', 'Output'))
        if run.get('config'):
            name += '_' + re.sub(r'[^\w.-]+', '', run['config'])
        return name + (started or datetime.now()).strftime('_%Y%m%d_%H%M%S') + '.html'

    def _write_report(self, tab, path):
        """Render the tab's captured output to `path`; True on success."""
        run = tab.last_run or {}
        script = run.get('script', 'Output')
        meta = script
        if run.get('config'):
            meta += f' — {run["config"]}'
        if run.get('started'):
            meta += ' — ' + run['started'].strftime('%Y-%m-%d %H:%M')
        if run.get('exit') is not None and run.get('ended'):
            secs = int((run['ended'] - run['started']).total_seconds())
            meta += f' (exit {run["exit"]}, {secs // 60}m{secs % 60:02d}s)'
        try:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(build_html_report(f'Check Royal Caribbean — {script}', meta, tab.raw_lines))
            return True
        except OSError as exc:
            messagebox.showerror('Check Royal Caribbean', f'Could not write report:\n{exc}')
            return False

    def _auto_export(self, tab):
        try:
            os.makedirs(REPORTS_DIR, exist_ok=True)
        except OSError:
            return None   # silent by design: mid-_finish_run, no dialogs
        path = os.path.join(REPORTS_DIR, self._default_report_name(tab))
        return path if self._write_report(tab, path) else None

    def export_clicked(self):
        tab = self.current_tab()
        if tab is None or not tab.raw_lines:
            messagebox.showinfo('Check Royal Caribbean', 'Nothing to export yet — run a script first.')
            return
        if not self._ensure_reports_dir():
            return
        path = filedialog.asksaveasfilename(
            title='Export HTML report', initialdir=REPORTS_DIR,
            initialfile=self._default_report_name(tab), defaultextension='.html',
            filetypes=[('HTML report', '*.html')])
        if not path or not self._write_report(tab, path):
            return
        if messagebox.askyesno('Check Royal Caribbean', f'Report saved:\n{path}\n\nOpen it in your browser?'):
            import webbrowser
            from pathlib import Path
            webbrowser.open(Path(path).absolute().as_uri())

    # ---------- shutdown ----------

    def _save_settings(self):
        # note: callers stash form values themselves - stashing here would file
        # the on-screen values under whichever tab is current, which is wrong
        # mid tab-removal
        current = [t.config_path for t in self.tabs if t.config_path]
        current_norm = {os.path.normcase(p) for p in current}
        kept_missing = [p for p in self._missing_configs
                        if os.path.normcase(p) not in current_norm]
        self.settings.pop('active_tab', None)   # superseded by active_config
        self.settings.update({
            'configs': current + kept_missing,
            'active_config': (self._last_real_tab.config_path
                              if self._last_real_tab in self.tabs else None),
            'last_script': self.script_var.get(),
            'clear_before_run': bool(self.clear_var.get()),
            'auto_export': bool(self.auto_export_var.get()),
            'repeat_enabled': bool(self.repeat_var.get()),
            'repeat_hours': self.repeat_hours_var.get(),
            'font_size': self.mono['size'],
            'form_values': self.form_values,
            'geometry': self.root.winfo_geometry(),
        })
        save_settings(self.settings)

    def _on_close(self):
        # Snapshot: the confirm dialog runs a nested event loop, so _poll can
        # finish the run (proc -> None) while it is open.
        proc = self.proc
        if proc is not None and proc.poll() is None:
            if not messagebox.askyesno('Check Royal Caribbean', 'A script is still running. Stop it and quit?'):
                return
        self._abort_queue()
        self._cancel_repeat()
        proc = self.proc
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=self.KILL_GRACE_S)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for stream in (proc.stdout, proc.stdin):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        self._stash_form_values()
        self._save_settings()
        self.root.destroy()


def run_gui():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    run_gui()
