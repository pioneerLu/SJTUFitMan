"""Local UI round trips use fixture configs, with no account/browser/mail actions."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import tomllib
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from booking_ui import BookingApp
from booking_options import date_value
from poll_settings import BEIJING
import poller_ui


def prepared():
    return {'slot':{'venue_name':'窗口测试','activity':'健身房','date':'2026-10-18',
        'time':'18:00-20:00','field':'场地1','price':'4.00'},'terms_title':'须知测试',
        'terms_text':'用于本地测试的完整须知。\n'*100,'confirmation_sha256':'test-confirmation-only'}


class SelectionUITests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk()
        self.window.withdraw()

    def tearDown(self):
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def test_confirm_without_preparation_fetches_terms_instead_of_stopping_at_hint(self):
        app=BookingApp(self.window)
        app.run=MagicMock()
        app.confirm()
        app.run.assert_called_once_with('prepare',app.selection())
        self.assertIsNone(app.review)
        app.close()

    def test_terms_result_opens_review_and_requires_checkbox_before_submit(self):
        app=BookingApp(self.window)
        app.run=MagicMock()
        data=prepared()
        app.display_result('prepare',data)
        self.assertIsNotNone(app.review)
        self.assertEqual(str(app.review_submit.cget('state')),'disabled')
        app.review_submit.invoke()
        app.run.assert_not_called()
        app.review_checkbox.invoke()
        self.assertEqual(str(app.review_submit.cget('state')),'normal')
        app.confirm()
        self.assertEqual(len([widget for widget in self.window.winfo_children() if isinstance(widget,tk.Toplevel)]),1)
        app.review_submit.invoke()
        app.run.assert_called_once_with('submit',['--confirm-reservation-sha256',data['confirmation_sha256']])
        self.assertIsNone(app.prepared)
        self.assertIsNone(app.review)
        app.close()

    def test_long_terms_cannot_hide_footer_at_minimum_window_size(self):
        app=BookingApp(self.window)
        self.window.attributes('-alpha',0.0)
        self.window.deiconify()
        self.window.update()
        app.prepared=prepared()
        app.confirm()
        review=app.review
        review.attributes('-alpha',0.0)
        review.geometry('560x450')
        review.update()
        for widget in (app.review_checkbox,app.review_submit):
            self.assertTrue(widget.winfo_ismapped())
            self.assertGreaterEqual(widget.winfo_rooty(),review.winfo_rooty())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),review.winfo_rooty()+review.winfo_height())
        app.close()

    def test_dropdown_sync_changes_date_and_invalidates_old_confirmation(self):
        app = BookingApp(self.window)
        app.prepared = {'confirmation_sha256': 'obsolete'}
        app.apply_options({'activity': '健身房', 'dates': ['2026-10-11', '2026-10-17'],
            'date': '2026-10-17', 'time_slots': ['19:00-20:00']})
        self.assertEqual(str(app.date_choice.cget('state')), 'readonly')
        self.assertEqual(str(app.time_choice.cget('state')), 'readonly')
        self.assertEqual(date_value(app.day.get()), '2026-10-17')
        self.assertIn('2026-10-17', app.selection())
        self.assertEqual(app.period.get(), '19:00-20:00')
        self.assertIsNone(app.prepared)
        app.hour_choices = ('18:00-19:00','19:00-20:00')
        app.period.set('18:00-19:00')
        app.duration.set('连续2小时')
        app.change_duration()
        self.assertEqual(app.period.get(),'18:00-20:00')
        self.assertIn('18:00-20:00',app.selection())
        self.assertFalse(hasattr(app,'budget'))
        self.assertNotIn('--max-price',app.selection())
        app.activity.set('台球')
        app.change_activity()
        self.assertEqual(app.period.get(), '')
        self.assertFalse(app.time_choice.cget('values'))

    def test_priority_selection_roundtrip_preserves_mail_and_busy_readonly(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'reservation.toml'
            template = (Path(__file__).resolve().parents[1] / 'config/config.example.toml').read_text(encoding='utf-8')
            template = template.replace('time_slots = []', 'time_slots = ["18:00-19:00"]')
            template = template.replace('[booking]', '[booking]\nmax_price = "0.01"')
            template = template.replace('password = ""', 'password = " local-test-code "')
            path.write_text(template, encoding='utf-8')
            with patch.object(poller_ui, 'SETTINGS_PATH', path), \
                    patch.object(poller_ui, 'beijing_now', return_value=datetime(2026, 12, 31, 12, 15, tzinfo=BEIJING)):
                app = poller_ui.PollerApp(self.window)
                picker = app.time_picker
                picker.selected.set('19:00-20:00')
                picker.add()
                picker.add()
                self.assertEqual(picker.get(), ['18:00-19:00', '19:00-20:00'])
                picker.move(-1)
                self.assertEqual(picker.get(), ['19:00-20:00', '18:00-19:00'])
                self.assertTrue(app.save_form())
                with path.open('rb') as stream:
                    saved = tomllib.load(stream)
                self.assertEqual(saved['booking']['time_slots'], picker.get())
                self.assertEqual(saved['mail']['password'], ' local-test-code ')
                self.assertFalse(saved['polling']['enabled'])
                self.assertEqual(saved['mail']['retry_interval_seconds'], 60)
                self.assertNotIn('max_price',saved['booking'])
                self.assertNotIn('max_price',app.variables)
                self.assertIn('2027-01-08', app.date_preview.get())
                app.set_busy(True)
                self.assertEqual(str(picker.choice.cget('state')), 'disabled')
                app.set_busy(False)
                self.assertEqual(str(picker.choice.cget('state')), 'readonly')
                picker.remove()
                self.assertEqual(picker.get(), ['18:00-19:00'])
                picker.duration.set('连续2小时')
                picker.refresh_choices()
                picker.selected.set('18:00-20:00')
                picker.add()
                self.assertEqual(picker.get(),['18:00-19:00','18:00-20:00'])
                self.assertTrue(app.save_form())
                app.read_form()
                self.assertEqual(picker.get(),['18:00-19:00','18:00-20:00'])
                self.assertIsNone(app.process)
                app.close()

    def test_waitlist_dropdowns_schedule_and_save_are_independent_of_daily_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'reservation.toml'
            template=(Path(__file__).resolve().parents[1]/'config/config.example.toml').read_text(encoding='utf-8')
            path.write_text(template,encoding='utf-8')
            from test_polling import mailbox
            from dataclasses import asdict
            with patch.object(poller_ui,'SETTINGS_PATH',path),patch.object(poller_ui,'beijing_now',
                    return_value=datetime(2026,10,11,12,tzinfo=BEIJING)):
                app=poller_ui.PollerApp(self.window,initial_tab='waitlist')
                self.assertEqual(app.tabs.select(),str(app.waitlist_tab))
                app.waitlist_vars['date'].set('2026-10-18（周日）')
                app.waitlist_vars['duration'].set('连续2小时');app.waitlist_period_choices()
                app.waitlist_vars['time'].set('18:00-20:00')
                app.waitlist_vars['start_mode'].set('定时开始');app.update_waitlist_controls()
                app.waitlist_vars['start_date'].set('2026-10-11（周日）')
                app.waitlist_vars['start_hour'].set('13');app.waitlist_vars['start_minute'].set('05')
                app.waitlist_vars['interval_seconds'].set('90')
                for key,value in asdict(mailbox()).items():
                    if key in app.variables: app.variables[key].set(str(value))
                app.launch=MagicMock()
                app.start_waitlist();app.launch.assert_called_once_with('waitlist')
                with path.open('rb') as stream: saved=tomllib.load(stream)
                self.assertFalse(saved['polling']['enabled']);self.assertEqual(saved['booking']['time_slots'],[])
                self.assertEqual((saved['waitlist']['date'],saved['waitlist']['time']),('2026-10-18','18:00-20:00'))
                self.assertEqual(saved['waitlist']['start_at'],'2026-10-11T13:05:00+08:00')
                self.assertEqual(saved['waitlist']['interval_seconds'],90)
                self.assertEqual(saved['mail']['password'],mailbox().password)
                app.read_form()
                self.assertEqual(app.waitlist_vars['duration'].get(),'连续2小时')
                self.assertEqual(app.waitlist_vars['time'].get(),'18:00-20:00')
                app.set_busy(True);self.assertEqual(str(app.waitlist_date_choice.cget('state')),'disabled')
                app.set_busy(False);self.assertEqual(str(app.waitlist_date_choice.cget('state')),'readonly')
                self.assertEqual(str(app.waitlist_schedule_hour.cget('state')),'readonly')
                app.waitlist_vars['start_mode'].set('立即开始');app.update_waitlist_controls()
                self.assertEqual(str(app.waitlist_schedule_hour.cget('state')),'disabled')
                app.close()

    def test_waitlist_expired_selection_does_not_arm_or_save(self):
        app=poller_ui.PollerApp(self.window,initial_tab='waitlist')
        app.waitlist_vars['date'].set('2026-10-11（周日）');app.waitlist_vars['time'].set('18:00-19:00')
        app.save_form=MagicMock();app.launch=MagicMock()
        with patch.object(poller_ui.WaitlistSettings,'validate_start',side_effect=poller_ui.BookingError('时段已过期')):
            app.start_waitlist()
        app.save_form.assert_not_called();app.launch.assert_not_called();app.close()

    def test_waitlist_sync_includes_full_periods_and_two_hour_options(self):
        app=poller_ui.PollerApp(self.window,initial_tab='waitlist')
        with patch.object(poller_ui,'beijing_now',return_value=datetime(2026,10,11,12,tzinfo=BEIJING)):
            app.apply_waitlist_options({'dates':['2026-10-10','2026-10-11','2026-10-17','2026-10-19'],
                'time_slots':['18:00-19:00','19:00-20:00']})
        self.assertEqual(len(app.waitlist_date_choice.cget('values')),2)
        app.waitlist_vars['duration'].set('连续2小时');app.waitlist_period_choices()
        self.assertEqual(app.waitlist_time_choice.cget('values'),('18:00-20:00',))
        app.close()


if __name__ == '__main__':
    unittest.main()
