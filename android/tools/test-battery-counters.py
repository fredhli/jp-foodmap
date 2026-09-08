#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('counters', Path(__file__).with_name('battery-counters.py'))
counters = importlib.util.module_from_spec(spec)
spec.loader.exec_module(counters)


def wake(ms):
    return f'9,10123,l,wl,example,{ms},f,1,0,p,0,0,w,0\n'


class BatteryCountersTest(unittest.TestCase):
    def test_normalization_counterexample_is_a_real_delta(self):
        result = counters.compare(wake(1200), wake(9800), '10123')
        self.assertEqual('changed', result['status'])
        self.assertEqual({'before': 1200, 'after': 9800, 'delta': 8600}, result['counters']['wl/example/full_ms'])

    def test_current_timer_fields_and_unset_durations_are_not_cumulative(self):
        before = '9,10123,l,wl,display,1200,f,1,-1,-1,-1,10,p,1,10,10,10,0,bp,0,0,0,0,0,w,0,0,0,0'
        after = before.replace('1200,f', '9800,f').replace('10,p,1,10,10,10', '10,p,1,0,10,10')
        result = counters.compare(before, after, '10123')
        self.assertEqual('changed', result['status'])
        self.assertEqual(8600, result['counters']['wl/display/full_ms']['delta'])
        self.assertEqual(0, result['counters']['wl/display/partial_ms']['delta'])

    def test_empty_or_missing_uid_never_passes(self):
        for uid, text in [('10123', ''), ('', wake(1200)), ('10124', wake(1200))]:
            self.assertEqual('skip', counters.compare(text, text, uid)['status'])

    def test_zero_network_is_valid_and_retained(self):
        text = '9,10123,l,nt,' + ','.join(['0'] * 10)
        result = counters.compare(text, text, '10123')
        self.assertEqual('unchanged', result['status'])
        self.assertEqual(0, result['counters']['nt//wifi_rx_bytes']['after'])

    def test_counters_reset_or_disappear_is_invalid(self):
        self.assertEqual('skip', counters.compare(wake(9800), wake(1200), '10123')['status'])
        self.assertEqual('skip', counters.compare(wake(1200), '', '10123')['status'])

    def test_other_uid_and_device_clock_do_not_pollute_sample(self):
        self.assertEqual('unchanged', counters.compare(wake(1200)+'9,0,l,bt,1234\n', wake(1200)+wake(9800).replace('10123', '10124')+'9,0,l,bt,9876\n', '10123')['status'])

    def test_unknown_format_is_skip(self):
        self.assertEqual('skip', counters.compare('9,10123,l,nt,1', '9,10123,l,nt,1', '10123')['status'])

    def test_complete_section_shapes_have_named_numeric_deltas(self):
        for section, lengths, label in [('nt', [10, 12, 22], 'mobile_rx_bytes'),
                ('pr', [4, 6], 'user_ms'), ('wfl', [3, 7, 10], 'full_lock_us'),
                ('jb', [2, 4], 'total_ms'), ('sy', [2, 4], 'total_ms')]:
            for length in lengths:
                prefix = f'9,10123,l,{section},' + ('example,' if section in counters.NAMED else '')
                old = prefix + ','.join(['0'] * length)
                new = prefix + ','.join(['8600'] + ['0'] * (length - 1))
                result = counters.compare(old, new, '10123')
                self.assertEqual('changed', result['status'], (section, length, result))
                key = section + '/' + ('example' if section in counters.NAMED else '') + '/' + label
                self.assertEqual({'before': 0, 'after': 8600, 'delta': 8600}, result['counters'][key])

    def test_microseconds_preserve_one_second_and_sub_millisecond(self):
        for micros in [1_000_000, 1, 999]:
            values = [0] * 22
            old = '9,10123,l,nt,' + ','.join(map(str, values))
            values[8] = micros
            result = counters.compare(old, '9,10123,l,nt,' + ','.join(map(str, values)), '10123')
            self.assertEqual('changed', result['status'])
            self.assertEqual(micros, result['counters']['nt//mobile_active_us']['delta'])
            self.assertNotIn('nt//mobile_active_ms', result['counters'])

    def test_review_truncated_rows_and_unknown_layouts_are_skip(self):
        for text in ['9,10123,l,pr,app,0', '9,10123,l,jb,job,0', '9,10123,l,sy,sync,0',
                     '9,10123,l,wfl,0', '9,10123,l,wl,lock,1200,f,1',
                     '9,10123,l,wl,lock,0,f,0,0,p,0', '9,10123,l,pr,app,0,0,0,0,0',
                     '9,10123,l,jb,job,0,0,0', '9,10123,l,wfl,0,0,0,0', '9,10123,l,nt']:
            self.assertEqual('skip', counters.compare(text, text, '10123')['status'], text)

    def test_unavailable_background_is_explicit_not_zero(self):
        for section in ['jb', 'sy']:
            old = f'9,10123,l,{section},example,1200,1,-1,-1'
            new = old.replace('1200', '9800')
            result = counters.compare(old, new, '10123')
            self.assertEqual('changed', result['status'])
            self.assertEqual(8600, result['counters'][f'{section}/example/total_ms']['delta'])
            self.assertEqual([f'{section}/example/background_count', f'{section}/example/background_ms'], result['unavailable'])
            self.assertEqual('skip', counters.compare(old, old.replace('-1,-1', '0,0'), '10123')['status'])
            self.assertEqual('skip', counters.compare(old, old.replace('-1,-1', '-1,0'), '10123')['status'])

    def test_current_wakelock_cumulative_unpooled_and_unknown_are_distinct(self):
        old = '9,10123,l,wl,x,0,f,0,-1,-1,-1,10,p,1,5,5,10,0,bp,0,0,0,0,0,w,0,0,0,0'
        new = old.replace('10,p,1,5,5,10', '10,p,1,0,5,20')
        result = counters.compare(old, new, '10123')
        self.assertEqual(10, result['counters']['wl/x/partial_unpooled_ms']['delta'])
        self.assertIn('wl/x/full_unpooled_ms', result['unavailable'])
        for invalid in [old + ',0', old.replace(',bp,', ',p,'), old.replace('0,w,0', '-2,w,0')]:
            self.assertEqual('skip', counters.compare(invalid, invalid, '10123')['status'])

    def test_row_removal_reset_shape_change_and_new_rows(self):
        old = wake(1200) + '9,10123,l,pr,app,1,0,0,0\n'
        for new in [wake(1200), old.replace('pr,app,1', 'pr,app,0'), old.replace('pr,app,1,0,0,0', 'pr,app,1,0,0,0,0,0')]:
            self.assertEqual('skip', counters.compare(old, new, '10123')['status'])
        added = counters.compare(wake(1200), old, '10123')
        self.assertEqual(1, added['counters']['pr/app/user_ms']['delta'])
        self.assertEqual(['pr', 'wl'], added['observed_sections'])


if __name__ == '__main__':
    unittest.main()
