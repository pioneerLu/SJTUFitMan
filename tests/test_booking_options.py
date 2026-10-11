"""Selection extraction and the read-only CLI; no live booking or email."""
from contextlib import redirect_stdout
from datetime import date, datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import booking as cli
from booking_flow import BookingError
from booking_options import date_label, date_value, local_dates, period_choices, website_options
from poll_settings import BEIJING


def grid():
    return {'loading': False, 'active_type': {'isactiveName': '健身房'},
        'dates': [{'isdate': value, 'dateId': 'PRIVATE-SIGNED-VALUE'}
            for value in ('2026-10-10', '2026-10-11', '2026-10-17')],
        'selected_date': {'isdate': '2026-10-17'},
        'slots': [{'entries': [{'time_range': '18:00-19:00', 'status': '-1', 'can_select': False},
            {'time_range': '19:00-20:00', 'status': '0', 'can_select': True}]}],
        'account': 'PRIVATE-USER-INFO'}


class OptionsTests(unittest.TestCase):
    def test_two_hour_choices_only_join_adjacent_website_periods(self):
        periods = ['18:00-19:00','19:00-20:00','21:00-22:00']
        self.assertEqual(period_choices(periods,2),('18:00-20:00',))
        self.assertEqual(period_choices(periods,1),tuple(periods))
    def test_date_labels_keep_weekday_out_of_cli_value(self):
        self.assertEqual(date_label('2026-10-18'), '2026-10-18（周日）')
        self.assertEqual(date_value(date_label('2026-10-18')), '2026-10-18')
        choices = local_dates(date(2028, 2, 23))
        self.assertEqual(len(choices), 8)
        self.assertEqual(date_value(choices[-1]), '2028-03-01')

    def test_options_keep_full_periods_and_remove_expired_dates_and_private_values(self):
        result = website_options(grid(), date(2026, 10, 11))
        self.assertEqual(result['dates'], ['2026-10-11', '2026-10-17'])
        self.assertEqual(result['time_slots'], ['18:00-19:00', '19:00-20:00'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertTrue(result['read_only'])
        self.assertFalse(result['real_order_created'])

    def test_malformed_options_cannot_be_offered_as_valid_selections(self):
        value = grid()
        value['dates'] += [{'isdate': '2026-02-30'}, {'isdate': None}]
        value['slots'][0]['entries'] += [{'time_range': '18:30-19:30'}, {'time_range': '23:00-00:00'}]
        result = website_options(value, date(2026, 10, 11))
        self.assertEqual(len(result['dates']), 2)
        self.assertEqual(len(result['time_slots']), 2)
        for snapshot in (None, {**grid(), 'loading': True}, {**grid(), 'dates': []}, {**grid(), 'slots': []}):
            with self.subTest(snapshot=snapshot), self.assertRaises(BookingError):
                website_options(snapshot, date(2026, 10, 11))

    def test_options_cli_falls_back_to_latest_open_date_without_submitting(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            browser = MagicMock()
            browser.activity_grid.return_value = grid()
            browser.grid.return_value = grid()
            output = io.StringIO()
            with patch.object(cli, 'PRIVATE', path), patch.object(cli, 'BookingBrowser', return_value=browser) as factory, \
                    patch.object(cli, 'load_config', return_value={}), \
                    patch.object(cli, 'sync_playwright'), patch.object(cli, 'ProcessLock'), \
                    patch.object(cli, 'beijing_now', return_value=datetime(2026, 10, 11, 11, tzinfo=BEIJING)), \
                    redirect_stdout(output):
                self.assertEqual(cli.main(['options', '--date', '2026-10-18']), 0)
            self.assertEqual(factory.call_args.kwargs['mode'], 'prepare')
            browser.grid.assert_called_once_with('健身房', '2026-10-17')
            browser.stage.assert_not_called()
            browser.commit.assert_not_called()
            browser.close.assert_called_once()
            self.assertEqual(json.loads(output.getvalue()), json.loads((path / 'options.json').read_text(encoding='utf-8')))
            self.assertFalse((path / 'prepared.json').exists())
            self.assertFalse((path / 'journal.json').exists())

    def test_expired_session_reports_how_to_restore_before_loading_choices(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = MagicMock()
            browser.start.side_effect = cli.FlowError('session expired')
            with patch.object(cli, 'PRIVATE', Path(temporary)), \
                    patch.object(cli, 'BookingBrowser', return_value=browser), \
                    patch.object(cli, 'load_config', return_value={}), \
                    patch.object(cli, 'sync_playwright'), patch.object(cli, 'ProcessLock'), \
                    self.assertRaisesRegex(BookingError, '登录与会话'):
                cli.main(['options'])
            browser.grid.assert_not_called()
            browser.commit.assert_not_called()
            browser.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
