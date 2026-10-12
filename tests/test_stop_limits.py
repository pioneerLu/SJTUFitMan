"""Time limits use fake clocks, orders and mail; no live site or SMTP calls."""
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import booking_flow as booking
import poll_runner as runner
from poll_settings import BEIJING, PollSettings, WaitlistSettings, SettingsError
from test_polling import Browser, Clock, mailbox, settings, snapshot
from test_waitlist import plan
import test_automatic_ui as form_fixtures
from configuration import read_config


class StopSettingsTests(unittest.TestCase):
    def test_daily_accepts_two_hours_and_time_only_budget(self):
        parsed = PollSettings.read({'booking': {'time_slots': ['18:00-20:00']},
            'polling': {'window_minutes': 120, 'max_checks': 0}})
        self.assertEqual(parsed.window(Clock()().date()), (Clock()(), Clock()() + timedelta(hours=2)))
        self.assertEqual(parsed.max_checks, 0)

    def test_daily_absolute_cutoff_caps_rounds_and_global_schedule(self):
        config = settings(stop_mode='deadline', stop_at='2026-10-12T14:00:00+08:00')
        self.assertEqual(config.window(Clock()().date())[1], datetime(2026, 10, 12, 12, tzinfo=BEIJING))
        self.assertEqual(config.window(datetime(2026, 10, 12).date())[1], datetime(2026, 10, 12, 14, tzinfo=BEIJING))
        config.validate_start(Clock()())
        with self.assertRaises(SettingsError): config.validate_start(datetime(2026, 10, 12, 14, tzinfo=BEIJING))

    def test_waitlist_uses_earlier_of_custom_limit_and_first_hour_start(self):
        started = datetime(2026, 10, 18, 17, 30, tzinfo=BEIJING)
        config = plan(period='18:00-20:00', stop_mode='duration', duration_minutes=120)
        self.assertEqual(config.end_time(started), datetime(2026, 10, 18, 18, tzinfo=BEIJING))
        config = replace(config, stop_mode='deadline', stop_at='2026-10-18T19:00:00+08:00')
        self.assertEqual(config.end_time(started), datetime(2026, 10, 18, 18, tzinfo=BEIJING))

    def test_timezone_cross_year_and_scheduled_duration(self):
        config = plan(date='2027-01-06', start_mode='scheduled', start_at='2026-12-31T15:30:00+00:00',
            stop_mode='duration', duration_minutes=120)
        self.assertEqual(config.end_time(config.start_time()), datetime(2027, 1, 1, 1, 30, tzinfo=BEIJING))
        deadline = replace(config, stop_mode='deadline', stop_at='2027-01-01T00:00:00')
        self.assertEqual(deadline.end_time(config.start_time()), datetime(2027, 1, 1, 0, tzinfo=BEIJING))

    def test_bad_limits_do_not_echo_input(self):
        for values in ({'stop_mode': 'bad'}, {'stop_mode': 'deadline', 'stop_at': 'EXAMPLE-SECRET'},
                {'stop_mode': 'duration', 'duration_minutes': 0}, {'duration_minutes': True},
                {'start_mode': 'scheduled', 'start_at': '2026-10-11T12:00:00+08:00',
                 'stop_mode': 'deadline', 'stop_at': '2026-10-11T11:59:00+08:00'}):
            with self.subTest(values=values), self.assertRaises(SettingsError) as caught:
                WaitlistSettings.read({'waitlist': {'date': '2026-10-18', 'time': '18:00-20:00', **values}})
            self.assertNotIn('EXAMPLE-SECRET', str(caught.exception))


class StopRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.clock = Clock(); self.event = threading.Event()
        stop_path = patch.object(runner, 'STOP', self.root / 'stop.json'); stop_path.start(); self.addCleanup(stop_path.stop)
        self.journal = booking.Journal(self.root / 'journal.json'); self.notified = []

    def waitlist(self, config, end_at=None):
        return runner.WaitlistRunner(config, mailbox(), store=runner.WaitlistStore(self.root / 'waitlist.json'),
            clock=self.clock, wait=self.clock.advance, stop=runner.StopSignal(self.event, self.clock),
            status=lambda *args, **kwargs: None, notifier=lambda *args: self.notified.append(args) or 'sent',
            end_at=end_at)

    def daily(self, config):
        return runner.DailyRunner(config, mailbox(), store=runner.DailyStore(self.root / 'daily.json'),
            clock=self.clock, wait=self.clock.advance, stop=runner.StopSignal(self.event, self.clock),
            status=lambda *args, **kwargs: None, notifier=lambda *args: self.notified.append(args) or 'sent')

    def test_daily_exceeds_old_one_hour_and_sixty_check_cap_then_sends_one_notice(self):
        browser = Browser(self.clock, [None] * 70 + [snapshot()])
        result = self.daily(settings(window_minutes=120, max_checks=0)).run_day(browser, self.journal, self.clock().date())
        self.assertEqual((result['phase'], result['checks'], browser.commits, len(self.notified)), ('confirmed', 71, 1, 1))
        self.assertGreater((browser.read_times[-1] - browser.read_times[0]).total_seconds(), 3600)

    def test_two_hour_duration_is_not_extended_after_each_query(self):
        browser = Browser(self.clock, [])
        result = self.waitlist(plan(stop_mode='duration', duration_minutes=120)).run_task(browser, self.journal)
        self.assertEqual((result['phase'], len(browser.read_times), browser.commits), ('deadline', 120, 0))
        self.assertEqual(self.clock(), datetime(2026, 10, 11, 14, tzinfo=BEIJING))
        self.assertEqual(result['stop_at'], '2026-10-11T14:00:00+08:00')
        self.assertFalse(self.notified)

    def test_deadline_stops_during_cooldown_and_never_reads_at_boundary(self):
        browser = Browser(self.clock, [None, snapshot()])
        result = self.waitlist(plan(stop_mode='deadline', stop_at='2026-10-11T12:00:30+08:00')).run_task(browser, self.journal)
        self.assertEqual((result['phase'], browser.read_times, browser.commits), ('deadline', [datetime(2026, 10, 11, 12, tzinfo=BEIJING)], 0))

    def test_duration_starts_at_scheduled_time(self):
        browser = Browser(self.clock, [])
        result = self.waitlist(plan(start_mode='scheduled', start_at='2026-10-11T12:02:00+08:00',
            stop_mode='duration', duration_minutes=1)).run_task(browser, self.journal)
        self.assertEqual(result['phase'], 'deadline')
        self.assertEqual(browser.read_times, [datetime(2026, 10, 11, 12, 2, tzinfo=BEIJING)])
        self.assertEqual(self.clock(), datetime(2026, 10, 11, 12, 3, tzinfo=BEIJING))

    def test_deadline_during_staging_or_final_dispatch_blocks_order_and_mail(self):
        for step in ('stage', 'dispatch'):
            with self.subTest(step=step):
                self.clock = Clock(); self.journal = booking.Journal(self.root / (step + '.json'))
                (self.root / 'waitlist.json').unlink(missing_ok=True)
                end = self.clock() + timedelta(minutes=1)
                browser = Browser(self.clock, [snapshot()])
                def reach_end(): self.clock.moment = end
                if step == 'stage': browser.on_stage = reach_end
                else: browser.before_commit = reach_end
                result = self.waitlist(plan(stop_mode='duration', duration_minutes=1)).run_task(browser, self.journal)
                self.assertEqual((result['phase'], browser.commits, len(self.notified)), ('deadline', 0, 0))

    def test_target_expiry_still_wins_over_long_duration(self):
        self.clock.moment = datetime(2026, 10, 18, 17, 59, 30, tzinfo=BEIJING)
        browser = Browser(self.clock, [])
        result = self.waitlist(plan(period='18:00-20:00', stop_mode='duration', duration_minutes=120)).run_task(browser, self.journal)
        self.assertEqual((result['phase'], len(browser.read_times), self.clock().hour), ('expired', 1, 18))

    def test_extended_daily_window_resumes_without_resetting_cooldown_or_count(self):
        config = settings(window_minutes=120, max_checks=0); instance = self.daily(config)
        record = instance.store.day(Browser.account, '2026-10-18')
        instance.store.update(record, phase='exhausted', checks=15, next_check_at='2026-10-11T12:20:00+08:00')
        self.clock.moment = datetime(2026, 10, 11, 12, 19, tzinfo=BEIJING)
        browser = Browser(self.clock, [snapshot()])
        result = instance.run_day(browser, self.journal, self.clock().date())
        self.assertEqual((result['checks'], browser.commits), (16, 1))
        self.assertEqual(browser.read_times, [datetime(2026, 10, 11, 12, 20, tzinfo=BEIJING)])


class StopControllerTests(unittest.TestCase):
    def test_preparation_work_is_included_in_duration_before_login(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); clock = Clock(); browser = Browser(clock, [])
            browser.start = lambda *args, **kwargs: clock.advance(20); browser.close = MagicMock()
            class Event:
                def is_set(self): return False
                def wait(self, seconds): clock.advance(seconds)
            with patch.multiple(runner, ROOT=root, PRIVATE=root / 'automation', STOP=root / 'stop.json',
                    beijing_now=clock, publish=MagicMock()), patch.object(runner, 'load_waitlist_settings',
                    return_value=(plan(stop_mode='duration', duration_minutes=1), mailbox())), \
                    patch.object(runner, 'load_config', return_value={'auth': {}}), patch.object(runner, 'BookingBrowser', return_value=browser), \
                    patch.object(runner, 'sync_playwright', return_value=MagicMock()), \
                    patch.object(runner, 'service_outbox', side_effect=lambda *args: clock.advance(30)), \
                    patch.object(runner, 'finish_waitlist_mail'):
                runner.run_waitlist(root / 'config.toml', Event())
            self.assertEqual(clock(), datetime(2026, 10, 11, 12, 1, tzinfo=BEIJING))
            self.assertEqual(browser.read_times, [datetime(2026, 10, 11, 12, 0, 50, tzinfo=BEIJING)])

    def test_login_does_not_restart_waitlist_duration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); clock = Clock(); browser = Browser(clock, [])
            browser.start = lambda *args, **kwargs: clock.advance(30); browser.close = MagicMock()
            class Event:
                def is_set(self): return False
                def wait(self, seconds): clock.advance(seconds)
            config = plan(stop_mode='duration', duration_minutes=1)
            with patch.multiple(runner, ROOT=root, PRIVATE=root / 'automation', STOP=root / 'stop.json',
                    beijing_now=clock, publish=MagicMock()), patch.object(runner, 'load_waitlist_settings', return_value=(config, mailbox())), \
                    patch.object(runner, 'load_config', return_value={'auth': {}}), patch.object(runner, 'BookingBrowser', return_value=browser), \
                    patch.object(runner, 'sync_playwright', return_value=MagicMock()), patch.object(runner, 'service_outbox'), \
                    patch.object(runner, 'finish_waitlist_mail'):
                runner.run_waitlist(root / 'config.toml', Event())
            self.assertEqual(clock(), datetime(2026, 10, 11, 12, 1, tzinfo=BEIJING))
            self.assertEqual(browser.read_times, [datetime(2026, 10, 11, 12, 0, 30, tzinfo=BEIJING)])

    def test_past_waitlist_deadline_never_opens_browser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); clock = Clock()
            with patch.multiple(runner, ROOT=root, PRIVATE=root / 'automation', STOP=root / 'stop.json',
                    beijing_now=clock, publish=MagicMock()), patch.object(runner, 'load_waitlist_settings',
                    return_value=(plan(stop_mode='deadline', stop_at='2026-10-11T11:59:00+08:00'), mailbox())), \
                    patch.object(runner, 'service_outbox'), patch.object(runner, 'BookingBrowser') as browser:
                runner.run_waitlist(root / 'config.toml', threading.Event())
            browser.assert_not_called()

    def test_daily_does_not_start_another_round_after_global_cutoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); clock = Clock(); browser = Browser(clock, [snapshot()]); phases = []
            browser.start = MagicMock(); browser.close = MagicMock()
            class Event:
                def is_set(self): return False
                def wait(self, seconds): clock.advance(seconds)
            config = settings(stop_mode='deadline', stop_at='2026-10-11T12:02:00+08:00', max_checks=0)
            with patch.multiple(runner, ROOT=root, PRIVATE=root / 'automation', STOP=root / 'stop.json',
                    beijing_now=clock, publish=lambda phase, *args, **kwargs: phases.append(phase)), \
                    patch.object(runner, 'load_settings', return_value=(config, mailbox())), \
                    patch.object(runner, 'load_config', return_value={'auth': {}}), patch.object(runner, 'BookingBrowser', return_value=browser), \
                    patch.object(runner, 'sync_playwright', return_value=MagicMock()), patch.object(runner, 'recover_payment_mail'), \
                    patch.object(runner, 'service_outbox'), patch.object(runner, 'Outbox', return_value=SimpleNamespace(records=[])), \
                    patch.object(runner.DailyRunner, 'notify', return_value='sent') as notify:
                runner.run_daily(root / 'config.toml', Event())
            browser.start.assert_called_once(); notify.assert_called_once()
            self.assertEqual(browser.commits, 1); self.assertEqual(phases[-1], 'deadline')
            self.assertEqual(clock(), datetime(2026, 10, 11, 12, 2, tzinfo=BEIJING))

    def test_login_manual_wait_is_bounded_by_polling_deadline(self):
        clock = Clock(); flow = MagicMock(); flow.client_logged_in.return_value = False
        flow.page.wait_for_timeout.side_effect = lambda milliseconds: clock.advance(milliseconds / 1000)
        with patch.object(runner, 'LoginBrowser', return_value=flow), patch.object(runner, 'publish'):
            self.assertFalse(runner.restore_login({'auth': {}}, MagicMock(), SimpleNamespace(is_set=lambda: False),
                timeout=300, clock=clock, end_at=clock() + timedelta(seconds=2)))
        self.assertEqual(clock(), datetime(2026, 10, 11, 12, 0, 2, tzinfo=BEIJING)); flow.close.assert_called_once()


class StopFormTests(unittest.TestCase):
    setUp = form_fixtures.AutomaticFormTests.setUp
    tearDown = form_fixtures.AutomaticFormTests.tearDown
    select_mode = form_fixtures.AutomaticFormTests.select_mode

    def test_two_hour_daily_save_removes_extra_default_count_cap_and_preserves_private_config(self):
        self.select_mode('daily')
        self.page.variables['stop_mode'].set(self.page.STOP_MODES['duration'])
        self.page.variables['duration_hours'].set('02'); self.page.variables['duration_minutes'].set('00')
        self.page.launch = MagicMock(return_value=True); self.page.start()
        saved = read_config(self.path)
        self.assertEqual((saved['polling']['window_minutes'], saved['polling']['max_checks']), (120, 0))
        for section in ('auth', 'mail', 'waitlist'): self.assertEqual(saved[section], self.before[section])

    def test_fixed_deadline_and_duration_are_saved_as_beijing_options(self):
        values = self.page.variables
        values['stop_mode'].set(self.page.STOP_MODES['deadline']); values['end_date'].set('2026-10-11（周日）')
        values['end_hour'].set('22'); values['end_minute'].set('15')
        self.page.launch = MagicMock(return_value=True); self.page.start()
        self.assertEqual(read_config(self.path)['waitlist']['stop_at'], '2026-10-11T22:15:00+08:00')
        self.page.variables['stop_mode'].set(self.page.STOP_MODES['duration'])
        values['duration_hours'].set('02'); values['duration_minutes'].set('30'); self.page.start()
        self.assertEqual(read_config(self.path)['waitlist']['duration_minutes'], 150)

    def test_inactive_limit_widgets_are_hidden_and_drafts_survive_mode_switch(self):
        values = self.page.variables; values['stop_mode'].set(self.page.STOP_MODES['duration'])
        values['duration_hours'].set('02'); self.page.update_controls()
        self.assertEqual(self.page.duration_area.winfo_manager(), 'grid')
        self.assertEqual(self.page.deadline_area.winfo_manager(), '')
        self.select_mode('daily'); self.select_mode('waitlist')
        self.assertEqual(values['duration_hours'].get(), '02')
        self.assertEqual(values['stop_mode'].get(), self.page.STOP_MODES['duration'])

    def test_past_deadline_and_zero_duration_do_not_write_or_start(self):
        for stop_mode in ('duration', 'deadline'):
            self.page.variables['stop_mode'].set(self.page.STOP_MODES[stop_mode])
            self.page.variables['duration_hours'].set('00'); self.page.variables['duration_minutes'].set('00')
            self.page.variables['end_date'].set('2026-10-11（周日）'); self.page.variables['end_hour'].set('11')
            self.page.variables['end_minute'].set('00'); self.page.launch = MagicMock(); self.page.start()
            self.page.launch.assert_not_called(); self.assertEqual(read_config(self.path), self.before)

    def test_daily_deadline_round_trip_and_inactive_duration_are_valid(self):
        self.select_mode('daily'); values = self.page.variables
        values['stop_mode'].set(self.page.STOP_MODES['deadline'])
        values['end_date'].set('2026-10-11（周日）'); values['end_hour'].set('22'); values['end_minute'].set('00')
        values['duration_hours'].set('00'); values['duration_minutes'].set('00')
        self.page.launch = MagicMock(return_value=True); self.page.start()
        self.page.launch.assert_called_once_with('poller', 'run')
        self.page.load_form()
        self.assertEqual(values['stop_mode'].get(), self.page.STOP_MODES['deadline'])
        self.assertEqual(values['end_hour'].get(), '22')
        self.assertIn('整体截止', self.page.preview.get())


if __name__ == '__main__': unittest.main()
