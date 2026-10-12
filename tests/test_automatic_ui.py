"""Shared plan form, settings and record fixtures never start a browser or real SMTP."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import queue
import sys
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import automatic_ui as ui
import login as login_ui
from configuration import read_config, write_config
from records_ui import RecordsPage
from settings_ui import MailSettingsPage
from poll_settings import BEIJING
from test_polling import mailbox, order


class AutomaticFormTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.path = self.root / 'config.toml'
        self.path.write_text((Path(__file__).resolve().parents[1] / 'config/config.example.toml').read_text(encoding='utf-8'), encoding='utf-8')
        values = read_config(self.path); values['mail'] = asdict(mailbox())
        values['auth']['username'] = 'EXAMPLE-STUDENT'; values['auth']['password'] = 'EXAMPLE-PRIVATE-PASSWORD'
        values['booking'].update(activity='台球', time_slots=['18:00-20:00'], field='八球台2')
        values['waitlist'].update(date='2026-10-18', time='19:00-21:00', field='健身房', interval_seconds=90)
        write_config(values, self.path)
        self.before = deepcopy(values)
        self.clock_patch = patch.object(ui, 'beijing_now', return_value=datetime(2026, 10, 11, 12, tzinfo=BEIJING))
        self.clock_patch.start(); self.addCleanup(self.clock_patch.stop)
        self.window = tk.Tk(); self.window.withdraw()
        self.host = ttk.Frame(self.window); self.host.pack(fill='both', expand=True)
        self.page = ui.AutoReservationPage(self.window, self.host, config_path=self.path)

    def tearDown(self):
        self.page.busy = False; self.page.close(); self.window.destroy()

    def select_mode(self, mode):
        self.page.mode.set(self.page.MODES[mode]); self.page.change_mode()

    def test_switch_preserves_two_hour_targets_projects_and_intervals(self):
        self.assertEqual(self.page.time_picker.get(), ['19:00-21:00'])
        self.page.variables['interval_seconds'].set('120')
        self.select_mode('daily')
        self.assertEqual(self.page.variables['activity'].get(), '台球')
        self.assertEqual(self.page.time_picker.get(), ['18:00-20:00'])
        self.assertTrue(self.page.time_picker.multiple)
        self.select_mode('waitlist')
        self.assertEqual(self.page.variables['interval_seconds'].get(), '120')
        self.assertEqual(self.page.variables['activity'].get(), '健身房')
        self.assertEqual(self.page.time_picker.get(), ['19:00-21:00'])
        self.assertFalse(self.page.time_picker.multiple)
        self.assertEqual(read_config(self.path), self.before)

    def test_start_fixed_plan_preserves_daily_login_and_mail_settings(self):
        self.page.launch = MagicMock(return_value=True)
        self.page.start()
        saved = read_config(self.path)
        self.page.launch.assert_called_once_with('poller', 'waitlist')
        self.assertEqual(saved['waitlist']['time'], '19:00-21:00')
        self.assertEqual(saved['ui']['reservation_mode'], 'waitlist')
        for section in ('polling', 'booking', 'auth', 'mail'): self.assertEqual(saved[section], self.before[section])

    def test_daily_start_combines_enable_save_and_start_without_touching_fixed_plan(self):
        self.select_mode('daily'); self.page.variables['interval_seconds'].set('60')
        self.page.launch = MagicMock(return_value=True)
        self.page.start()
        saved = read_config(self.path)
        self.assertTrue(saved['polling']['enabled'])
        self.assertEqual(saved['booking']['time_slots'], ['18:00-20:00'])
        self.page.launch.assert_called_once_with('poller', 'run')
        for section in ('waitlist', 'auth', 'mail'): self.assertEqual(saved[section], self.before[section])

    def test_expired_target_cannot_save_or_start(self):
        self.page.variables['date'].set('2026-10-10（周六）')
        self.page.launch = MagicMock()
        self.page.start()
        self.page.launch.assert_not_called()
        self.assertEqual(read_config(self.path), self.before)

    def test_invalid_interval_cannot_save_or_start(self):
        self.page.variables['interval_seconds'].set('1')
        self.page.launch = MagicMock(); self.page.start()
        self.page.launch.assert_not_called(); self.assertEqual(read_config(self.path), self.before)

    def test_fixed_date_scheduled_start_saved_in_beijing_time(self):
        variables = self.page.variables
        variables['start_mode'].set('定时开始'); variables['start_date'].set('2026-10-11（周日）')
        variables['start_hour'].set('13'); variables['start_minute'].set('05')
        self.page.launch = MagicMock(return_value=True); self.page.start()
        self.assertEqual(read_config(self.path)['waitlist']['start_at'], '2026-10-11T13:05:00+08:00')

    def test_refresh_never_silently_replaces_unopened_target_date(self):
        previous = self.page.variables['date'].get()
        self.page.apply_options({'activity': '健身房', 'activities': ['健身房', '台球'],
            'dates': ['2026-10-11', '2026-10-12'], 'time_slots': ['19:00-20:00', '20:00-21:00']})
        self.assertEqual(self.page.variables['date'].get(), previous)
        self.assertEqual(self.page.time_picker.get(), ['19:00-21:00'])
        self.assertIn('目标日期已保留', self.page.hint.get())

    def test_activity_switch_clears_old_period_and_field(self):
        self.page.variables['activity'].set('钢琴'); self.page.change_activity()
        self.assertEqual(self.page.time_picker.get(), [])
        self.assertEqual(self.page.variables['field'].get(), '')

    def test_busy_form_prevents_target_changes_and_another_start(self):
        self.page.set_busy(True)
        self.assertEqual(str(self.page.mode_choice.cget('state')), 'disabled')
        self.page.launch = MagicMock(); self.page.start(); self.page.launch.assert_not_called()
        self.page.set_busy(False)
        self.assertEqual(str(self.page.activity_choice.cget('state')), 'readonly')

    def test_mail_settings_save_preserves_both_plans_and_exact_authorization_code(self):
        self.page.close()
        pane = MailSettingsPage(self.window, self.host, config_path=self.path)
        try:
            code = ' EXAMPLE-PRIVATE-CODE $() \\" '
            pane.variables['password'].set(code)
            pane.variables['sender'].set('new@example.test'); pane.variables['recipient'].set('target@example.test')
            self.assertTrue(pane.save())
            saved = read_config(self.path)
            self.assertEqual(saved['mail']['password'], code)
            self.assertEqual(saved['mail']['username'], 'new@example.test')
            for section in ('booking', 'waitlist', 'polling', 'auth'): self.assertEqual(saved[section], self.before[section])
        finally: pane.close()

    def test_mail_custom_smtp_username_is_preserved_when_sender_changes(self):
        self.page.close()
        pane = MailSettingsPage(self.window, self.host, config_path=self.path)
        try:
            pane.variables['username'].set('custom-login')
            pane.variables['sender'].set('new@example.test')
            self.assertTrue(pane.save())
            self.assertEqual(read_config(self.path)['mail']['username'], 'custom-login')
        finally: pane.close()

    def test_records_show_success_notice_and_unknown_submission_without_network(self):
        self.page.close()
        local = self.root / '.local/booking'; local.mkdir(parents=True)
        event = order()
        entries = [{'account': 'account', 'phase': 'confirmed', 'order': event, 'slot': event['slot']},
                   {'account': 'account', 'phase': 'unknown', 'order_id': None, 'slot': event['slot']}]
        (local / 'journal.json').write_text(json.dumps({'schema_version': 1, 'entries': entries}), encoding='utf-8')
        pane = RecordsPage(self.window, self.host, root=self.root, config_path=self.path)
        try:
            self.assertIsNone(pane.process)
            self.assertEqual(len(pane.table.get_children()), 2)
            self.assertEqual(pane.table.get_children()[0], '1')
            pane.table.selection_set('0'); pane.select_record()
            text = pane.details.get('1.0', 'end')
            self.assertIn('北京时间', text); self.assertIn('后台补发', text)
            self.assertNotIn('account', text)
        finally: pane.close()


class CompactLoginWorkerTests(unittest.TestCase):
    def test_automatic_button_opens_login_page_before_ocr_and_saves_no_secrets_to_output(self):
        commands, events = queue.Queue(), queue.Queue()
        commands.put(('auto-open', {'username': 'EXAMPLE-STUDENT', 'password': 'EXAMPLE-PRIVATE-PASSWORD'}))
        commands.put(('quit', None))
        flow = MagicMock(); flow.browser.is_connected.return_value = True
        with patch('playwright.sync_api.sync_playwright'), patch.object(login_ui, 'LoginBrowser', return_value=flow):
            login_ui.browser_worker({'auth': {}}, commands, events)
        flow.start.assert_called_once()
        flow.fill_credentials.assert_called_once_with(username='EXAMPLE-STUDENT', password='EXAMPLE-PRIVATE-PASSWORD')
        flow.automatic_login.assert_called_once(); flow.close.assert_called_once()
        self.assertNotIn('EXAMPLE', str(list(events.queue)))


if __name__ == '__main__': unittest.main()
