#!/usr/bin/env python3
"""Compare supported cumulative per-UID batterystats check-in counters.

Shapes and units: AOSP BatteryStats.java dumpCheckinLocked/printWakeLockCheckin,
android-5.0.0_r1, android-7.0.0_r1, android-9.0.0_r1 and master.
Unknown/truncated shapes are unavailable evidence, never an unchanged result.
"""
import csv
import io
import json
from pathlib import Path
import re
import sys

NAMED = {'wl', 'jb', 'sy', 'pr'}
SECTIONS = NAMED | {'nt', 'wfl'}
NETWORK = ['mobile_rx_bytes', 'mobile_tx_bytes', 'wifi_rx_bytes', 'wifi_tx_bytes',
           'mobile_rx_packets', 'mobile_tx_packets', 'wifi_rx_packets', 'wifi_tx_packets',
           'mobile_active_us', 'mobile_active_count', 'bluetooth_rx_bytes', 'bluetooth_tx_bytes',
           'mobile_wakeup_count', 'wifi_wakeup_count', 'mobile_background_rx_bytes',
           'mobile_background_tx_bytes', 'wifi_background_rx_bytes', 'wifi_background_tx_bytes',
           'mobile_background_rx_packets', 'mobile_background_tx_packets',
           'wifi_background_rx_packets', 'wifi_background_tx_packets']
PROCESS = ['user_ms', 'system_ms', 'foreground_ms', 'starts_count', 'anr_count', 'crash_count']
WIFI = ['full_lock_us', 'scan_us', 'running_us', 'scan_count', None, None, None,
        'background_scan_count', 'actual_scan_ms', 'background_actual_scan_ms']
TIMERS = {'f': 'full', 'p': 'partial', 'bp': 'background_partial', 'w': 'window'}


def integer(value, unavailable=False):
    if not re.fullmatch(r'-?[0-9]+', value) or int(value) < (-1 if unavailable else 0):
        raise ValueError('unsupported UID counter value')
    return None if int(value) == -1 and unavailable else int(value)


def fields(section, values):
    """Return cumulative fields, preserving supported unavailable values as None."""
    size = len(values)
    if section == 'wl':
        if size == 9:
            markers, width = ['f', 'p', 'w'], 3
        elif size == 24:
            markers, width = ['f', 'p', 'bp', 'w'], 6
        else:
            raise ValueError('unsupported or incomplete wakelock shape')
        result = {}
        for offset, marker in zip(range(0, size, width), markers):
            group = values[offset:offset+width]
            if group[1] != marker:
                raise ValueError('unsupported wakelock timer order')
            name = TIMERS[marker]
            result[f'{name}_ms'] = integer(group[0])
            result[f'{name}_count'] = integer(group[2])
            if width == 6:
                # Current/max duration may shrink; total unpooled duration is cumulative when present.
                integer(group[3], unavailable=True)
                integer(group[4], unavailable=True)
                result[f'{name}_unpooled_ms'] = integer(group[5], unavailable=True)
        return result
    if section in {'jb', 'sy'}:
        if size not in {2, 4}:
            raise ValueError('unsupported or incomplete job/sync shape')
        result = {'total_ms': integer(values[0]), 'count': integer(values[1])}
        if size == 4:
            bg = [integer(value, unavailable=True) for value in values[2:]]
            if (bg[0] is None) != (bg[1] is None):
                raise ValueError('inconsistent unavailable background timer')
            result.update(background_ms=bg[0], background_count=bg[1])
        return result
    layouts = {'nt': ({10, 12, 22}, NETWORK), 'pr': ({4, 6}, PROCESS), 'wfl': ({3, 7, 10}, WIFI)}
    sizes, labels = layouts[section]
    if size not in sizes:
        raise ValueError('unsupported or incomplete UID counter shape')
    result = {}
    for label, value in zip(labels, values):
        number = integer(value)
        if label is None:
            if number != 0:
                raise ValueError('unsupported legacy Wi-Fi field')
        else:
            result[label] = number
    return result


def sample(text, uid):
    if not re.fullmatch(r'[0-9]+', str(uid)) or int(uid) < 10000:
        raise ValueError('missing application UID')
    counters, schema = {}, {}
    for row in csv.reader(io.StringIO(text), strict=True):
        if len(row) < 4 or row[1] != str(uid) or row[2] != 'l' or row[3] not in SECTIONS:
            continue
        if len(row) < 5 or row[0] != '9':
            raise ValueError('unsupported check-in version or incomplete UID row')
        section = row[3]
        name = row[4] if section in NAMED else ''
        values = row[5:] if section in NAMED else row[4:]
        prefix = f'{section}/{name}'
        if prefix in schema:
            raise ValueError('duplicate UID counter row')
        decoded = fields(section, values)
        counters.update({f'{prefix}/{label}': value for label, value in decoded.items()})
        schema[prefix] = len(values)
    if not any(value is not None for value in counters.values()):
        raise ValueError('no usable cumulative counters for the application UID')
    return counters, schema


def compare(before, after, uid):
    try:
        old, old_shape = sample(before, uid)
        new, new_shape = sample(after, uid)
        if any(new_shape.get(key) != shape for key, shape in old_shape.items()):
            raise ValueError('counter rows disappeared or changed format')
        changes, unavailable = {}, []
        for key, value in sorted(new.items()):
            previous = old.get(key, 0)
            if value is None:
                if key in old and previous is not None:
                    raise ValueError('counter availability changed during sampling')
                unavailable.append(key)
                continue
            if previous is None:
                raise ValueError('counter availability changed during sampling')
            delta = value - previous
            if delta < 0:
                raise ValueError('cumulative counters reset during sampling')
            changes[key] = {'before': previous, 'after': value, 'delta': delta}
        status = 'changed' if any(item['delta'] > 0 for item in changes.values()) else 'unchanged'
        return {'status': status, 'uid': int(uid), 'counters': changes, 'unavailable': unavailable,
                'observed_sections': sorted({key.split('/')[0] for key in changes}),
                'scope': 'Only listed cumulative counters; suffixes state us/ms/bytes/packets/count. Not physical energy or heat.'}
    except (ValueError, csv.Error) as error:
        return {'status': 'skip', 'reason': str(error)}


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit('usage: battery-counters.py UID BEFORE_CHECKIN AFTER_CHECKIN')
    result = compare(Path(sys.argv[2]).read_text(), Path(sys.argv[3]).read_text(), sys.argv[1])
    print(json.dumps(result, indent=2))
    sys.exit({'unchanged': 0, 'changed': 1, 'skip': 2}[result['status']])
