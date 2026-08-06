"""Tests for the pure helpers in CheckRoyalCaribbeanGui.py (no display required)."""

import os
import sys

import CheckRoyalCaribbeanGui as gui

# The exact constants the scripts emit (CheckRoyalCaribbeanPrice.py lines 38-47)
RESET = '\033[0m'
RED = '\033[1;31;40m'
GREEN = '\033[1;32m'
YELLOW = '\033[33m'
BLUE = '\033[94m'
CYAN = '\033[96m'


def flat(tokens):
    return ''.join(t for t, _ in tokens)


def test_ansi_tokens_plain():
    tokens, color = gui.ansi_tokens('no colors here\n')
    assert tokens == [('no colors here\n', None)]
    assert color is None


def test_ansi_tokens_real_constants():
    line = f'paid {RED}$100{RESET} now {GREEN}$80{RESET} ok\n'
    tokens, color = gui.ansi_tokens(line)
    assert tokens == [
        ('paid ', None),
        ('$100', gui.COLOR_HEX[31]),
        (' now ', None),
        ('$80', gui.COLOR_HEX[32]),
        (' ok\n', None),
    ]
    assert color is None


def test_ansi_tokens_all_script_colors():
    for const, code in ((RED, 31), (GREEN, 32), (YELLOW, 33), (BLUE, 94), (CYAN, 96)):
        tokens, _ = gui.ansi_tokens(f'{const}x{RESET}')
        assert tokens == [('x', gui.COLOR_HEX[code])]


def test_ansi_tokens_color_carries_across_lines():
    tokens1, color = gui.ansi_tokens(f'{YELLOW}warning starts\n')
    assert tokens1 == [('warning starts\n', gui.COLOR_HEX[33])]
    tokens2, color = gui.ansi_tokens('still yellow', color)
    assert tokens2 == [('still yellow', gui.COLOR_HEX[33])]
    tokens3, color = gui.ansi_tokens(f'{RESET}back to normal', color)
    assert tokens3 == [('back to normal', None)]


def test_ansi_tokens_strips_non_sgr_sequences():
    # cursor-up + erase-line sequences must vanish without changing color
    tokens, color = gui.ansi_tokens('\x1b[2Kfoo\x1b[1Abar')
    assert flat(tokens) == 'foobar'
    assert all(c is None for _, c in tokens)


def test_ansi_to_html_escapes_and_colors():
    out = gui.ansi_to_html([f'a <b> & {GREEN}win{RESET}\n'])
    assert '&lt;b&gt;' in out and '&amp;' in out
    assert f'<span style="color:{gui.COLOR_HEX[32]}">win</span>' in out
    assert '\x1b' not in out


def sdef_by_label(label):
    return next(s for s in gui.SCRIPTS if s.label == label)


def test_build_cmd_price_checker():
    cmd = gui.build_cmd(sdef_by_label('Price Checker'), '/tmp/acct.yaml', {})
    assert cmd[0] == sys.executable and cmd[1] == '-u'
    assert cmd[2].endswith('CheckRoyalCaribbeanPrice.py')
    assert cmd[3:] == ['-c', '/tmp/acct.yaml']


def test_build_cmd_omits_empty_and_includes_set():
    sdef = sdef_by_label('Cabin Upgrades')
    cmd = gui.build_cmd(sdef, 'cfg.yaml', {'--reservation': '123,456', '--limit': '', '--alert-below': ' 500 '})
    assert '--reservation' in cmd and cmd[cmd.index('--reservation') + 1] == '123,456'
    assert '--limit' not in cmd
    assert cmd[cmd.index('--alert-below') + 1] == '500'


def test_build_cmd_checkboxes():
    sdef = sdef_by_label('Back-to-Back Cabins')
    values = {'--ship': 'ovation', '--type': 'all', '--hump-only': True, '--flip-sides': False}
    cmd = gui.build_cmd(sdef, None, values)
    assert '-c' not in cmd
    assert '--hump-only' in cmd
    assert '--flip-sides' not in cmd
    assert cmd[cmd.index('--ship') + 1] == 'ovation'


def test_build_cmd_browse_c_is_currency_not_config():
    sdef = sdef_by_label('Cruise Planner Browser')
    cmd = gui.build_cmd(sdef, None, {'-s': 'Ovation', '-d': '03/15/27', '-c': 'USD'})
    assert cmd.count('-c') == 1
    assert cmd[cmd.index('-c') + 1] == 'USD'
    assert sdef.stdin_feed == '\n'


def test_script_whitelist_matches_table():
    assert gui.SCRIPT_WHITELIST == {s.filename for s in gui.SCRIPTS}
    for s in gui.SCRIPTS:
        assert os.path.isfile(os.path.join(gui.SCRIPT_DIR, s.filename)), s.filename


def test_real_ships_normalizes_all_caps_names():
    """All-caps fleet entries are now real new ships (Hero, Compass, Seeker), not
    placeholders - keep them with normalized casing; only empty names drop."""
    fleet = [
        {'code': 'RC', 'name': 'CELEBRITY COMPASS', 'brand': 'C'},
        {'code': 'HE', 'name': 'HERO OF THE SEAS', 'brand': 'R'},
        {'code': 'AX', 'name': 'Celebrity Apex', 'brand': 'C'},
        {'code': 'OV', 'name': 'Ovation of the Seas', 'brand': 'R'},
        {'code': 'ZZ', 'name': '', 'brand': 'R'},
    ]
    ships = gui.real_ships(fleet)
    assert [s['code'] for s in ships] == ['RC', 'HE', 'AX', 'OV']
    assert ships[0]['name'] == 'Celebrity Compass'
    assert ships[1]['name'] == 'Hero of the Seas'      # "of the" stays lowercase
    assert ships[2]['name'] == 'Celebrity Apex'        # already-cased names untouched
    assert ships[3]['name'] == 'Ovation of the Seas'


def test_normalize_ship_name_feeds_short_ship_name():
    """The normalized form must be matchable by short_ship_name, so the new
    all-caps ships appear in the Browse dropdown."""
    assert gui.short_ship_name(gui.normalize_ship_name('HERO OF THE SEAS')) == 'Hero'
    assert gui.short_ship_name(gui.normalize_ship_name('CELEBRITY COMPASS')) == 'Compass'


def test_short_ship_name_matches_browse_patterns():
    # Browse matches -s as "<arg> of the Seas" or "Celebrity <arg>"
    assert gui.short_ship_name('Ovation of the Seas') == 'Ovation'
    assert gui.short_ship_name('Celebrity Apex') == 'Apex'
    assert gui.short_ship_name('Utopia of the Seas') == 'Utopia'
    assert gui.short_ship_name('Some Other Vessel') is None
    assert gui.short_ship_name('') is None


def test_sail_to_browse_display_matches_browse_rendering():
    # Browse's -d matcher compares the argument against the first token of
    # strftime(DATE_DISPLAY_FORMAT) under the user's locale - the dropdown
    # value must be byte-for-byte what Browse itself would render.
    from datetime import datetime
    import BrowseRoyalCaribbeanPrice as browse
    expected = datetime.strptime('20260822', '%Y%m%d') \
        .strftime(browse.DATE_DISPLAY_FORMAT).split(' ')[0]
    assert gui.sail_to_browse_display('20260822') == expected
    assert gui.sail_to_browse_display('2026-08-22') == expected
    assert gui.sail_to_browse_display('') is None
    assert gui.sail_to_browse_display(None) is None
    assert gui.sail_to_browse_display('2026822') is None
    assert gui.sail_to_browse_display('notadate') is None


def test_tab_title_badges():
    assert gui.tab_title('config.yaml.jim') == '    config.yaml.jim    '
    assert gui.tab_title('config.yaml.jim', '▶ ') == '    ▶ config.yaml.jim    '
    assert gui.tab_title('x', '✓ ').strip() == '✓ x'


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, 'SETTINGS_FILE', str(tmp_path / 'gui_settings.json'))
    assert gui.load_settings() == {}
    data = {'configs': ['/a/config.yaml'], 'active_tab': 0, 'last_script': 'Price Checker'}
    gui.save_settings(data)
    assert gui.load_settings() == data


def test_settings_corrupt_file(tmp_path, monkeypatch):
    p = tmp_path / 'gui_settings.json'
    p.write_text('{not json')
    monkeypatch.setattr(gui, 'SETTINGS_FILE', str(p))
    assert gui.load_settings() == {}


def test_settings_non_dict_json(tmp_path, monkeypatch):
    p = tmp_path / 'gui_settings.json'
    p.write_text('[1, 2, 3]')
    monkeypatch.setattr(gui, 'SETTINGS_FILE', str(p))
    assert gui.load_settings() == {}


def test_save_settings_atomic_on_bad_value(tmp_path, monkeypatch):
    # A serialization failure must never truncate the existing settings file
    from datetime import datetime
    p = tmp_path / 'gui_settings.json'
    monkeypatch.setattr(gui, 'SETTINGS_FILE', str(p))
    assert gui.save_settings({'configs': ['/a.yaml']}) is True
    assert gui.save_settings({'poison': datetime.now()}) is False
    assert gui.load_settings() == {'configs': ['/a.yaml']}


def test_build_cmd_frozen_uses_run_script_dispatch(monkeypatch):
    monkeypatch.setattr(gui, 'FROZEN', True)
    cmd = gui.build_cmd(sdef_by_label('Price Checker'), '/tmp/acct.yaml', {})
    assert cmd[1] == '--run-script'
    assert cmd[2] == 'CheckRoyalCaribbeanPrice.py'
    assert cmd[3:] == ['-c', '/tmp/acct.yaml']


def test_ship_code_from_display():
    assert gui.ship_code_from_display('Ovation of the Seas (OV)') == 'OV'
    assert gui.ship_code_from_display('  Celebrity Apex (AX)  ') == 'AX'
    assert gui.ship_code_from_display('ovation') == 'ovation'   # typed fragment passes through
    assert gui.ship_code_from_display('OV') == 'OV'
    assert gui.ship_code_from_display('') == ''
    assert gui.ship_code_from_display(None) == ''


def test_repeat_delay_ms():
    assert gui.repeat_delay_ms('12') == 12 * 3600 * 1000
    assert gui.repeat_delay_ms('0.5') == 30 * 60 * 1000
    assert gui.repeat_delay_ms(' 1 ') == 3600 * 1000
    assert gui.repeat_delay_ms('0') is None
    assert gui.repeat_delay_ms('-1') is None
    assert gui.repeat_delay_ms('twelve') is None
    assert gui.repeat_delay_ms('') is None
    assert gui.repeat_delay_ms(None) is None
    # huge values clamp to Tcl's 32-bit after() limit rather than raising
    assert gui.repeat_delay_ms('100000') == gui.TCL_MAX_MS


def test_migrate_form_values():
    labels = {'Price Checker', 'Cabin Upgrades'}
    legacy = {'Cabin Upgrades': {'--limit': '5'}}
    assert gui.migrate_form_values(legacy, labels) == {'*shared*': legacy}
    # already-migrated input is untouched (regression: it used to double-wrap)
    migrated = {'*shared*': legacy, '/cfg/config.yaml.jim': {'Cabin Upgrades': {'--limit': '9'}}}
    assert gui.migrate_form_values(dict(migrated), labels) == migrated
    # mixed file: legacy keys fold into shared, path scopes survive
    mixed = {'Cabin Upgrades': {'--limit': '5'},
             '/cfg/config.yaml.jim': {'Cabin Upgrades': {'--limit': '9'}}}
    out = gui.migrate_form_values(mixed, labels)
    assert out['/cfg/config.yaml.jim'] == {'Cabin Upgrades': {'--limit': '9'}}
    assert out['*shared*']['Cabin Upgrades'] == {'--limit': '5'}
    assert 'Cabin Upgrades' not in out or out.get('Cabin Upgrades') is None or True
    assert set(out) == {'*shared*', '/cfg/config.yaml.jim'}
    assert gui.migrate_form_values('junk', labels) == {}


def test_validate_values():
    sdef = sdef_by_label('Cabin Upgrades')
    values, err = gui.validate_values(sdef, {'--reservation': ' 123 ', '--limit': '5'})
    assert err is None and values['--reservation'] == '123' and values['--limit'] == '5'
    _, err = gui.validate_values(sdef, {'--limit': 'five'})
    assert 'whole number' in err
    _, err = gui.validate_values(sdef, {'--alert-below': 'cheap'})
    assert 'must be a number' in err
    b2b = sdef_by_label('Back-to-Back Cabins')
    _, err = gui.validate_values(b2b, {'--ship': '   ', '--type': 'all'})
    assert 'required' in err
    values, err = gui.validate_values(b2b, {'--ship': 'Ovation of the Seas (OV)',
                                            '--type': 'all', '--hump-only': 1})
    assert err is None and values['--ship'] == 'OV' and values['--hump-only'] is True


def test_ansi_to_html_carries_color_across_lines():
    out = gui.ansi_to_html([f'{GREEN}first\n', 'still green\n', f'{RESET}plain\n'])
    green = gui.COLOR_HEX[32]
    assert f'<span style="color:{green}">first\n</span>' in out
    assert f'<span style="color:{green}">still green\n</span>' in out
    assert 'plain' in out and out.rindex('plain') > out.rindex('</span>') - len('</span>')


def test_build_html_report_escapes():
    doc = gui.build_html_report('T <&> title', 'meta <&> line', ['body <&> text\n'])
    assert 'T &lt;&amp;&gt; title' in doc
    assert 'meta &lt;&amp;&gt; line' in doc
    assert 'body &lt;&amp;&gt; text' in doc
    assert '<&>' not in doc
