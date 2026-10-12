"""Daily project selection, actual routing and recovery; no live orders or mail."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import tomllib
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import booking_flow as booking
import mail_notice
import poll_runner as runner
import poller_ui
from booking_options import website_options
from poll_settings import BEIJING, PollSettings, SettingsError
from test_polling import Browser, Clock, mailbox, order, settings, snapshot, target_slot


class ActivitySettingsTests(unittest.TestCase):
    def test_legacy_config_defaults_to_gym_and_selected_project_is_public(self):
        config={'booking':{'time_slots':['18:00-20:00']}}
        self.assertEqual(PollSettings.read(config).activity, '健身房')
        config['booking']['activity']='钢琴'
        parsed=PollSettings.read(config)
        self.assertEqual(parsed.activity, '钢琴')
        self.assertEqual(parsed.public(datetime(2026,10,12,11,tzinfo=BEIJING))['activity'], '钢琴')
        for invalid in ('', '   ', None, True, '钢琴\n', 'x'*81):
            config['booking']['activity']=invalid
            with self.subTest(invalid=invalid), self.assertRaises(SettingsError):
                PollSettings.read(config)

    def test_website_projects_export_names_without_signed_ids(self):
        grid=snapshot()
        grid.update(dates=[{'isdate':'2026-10-18'}], types=[
            {'id':'PRIVATE-SIGNED-ID','name':'健身房'}, {'name':'钢琴'}, {'name':'钢琴'},
            {'name':None}, {'name':'bad\nproject'}])
        choices=website_options(grid, datetime(2026,10,12).date())
        self.assertEqual(choices['activities'], ['健身房','钢琴'])
        self.assertNotIn('PRIVATE',str(choices))


class ActivityRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.clock=Clock()
        stop_path=patch.object(runner,'STOP',self.root/'stop.json');stop_path.start();self.addCleanup(stop_path.stop)
        self.store=runner.DailyStore(self.root/'days.json')
        self.journal=booking.Journal(self.root/'journal.json')

    def test_selected_project_routes_two_hour_booking_and_does_not_adopt_gym_order(self):
        old=self.journal.begin(Browser.account,target_slot(),[])
        self.journal.update(old,'confirmed',order=order())
        grid=snapshot(('18:00-19:00','19:00-20:00'))
        grid['active_type']['isactiveName']='钢琴'
        browser=Browser(self.clock,[])
        browser.refresh_grid=MagicMock(return_value=grid)
        notify=MagicMock(return_value='sent');messages=[]
        plan=settings(activity='钢琴',time_slots=('18:00-20:00',))
        flow=runner.DailyRunner(plan,mailbox(),store=self.store,clock=self.clock,
            notifier=notify,wait=self.clock.advance,stop=runner.StopSignal(clock=self.clock),
            status=lambda phase,message,**values:messages.append(message))
        result=flow.run_day(browser,self.journal,self.clock().date())
        browser.refresh_grid.assert_called_once_with('钢琴','2026-10-18')
        self.assertEqual((result['phase'],browser.commits),('confirmed',1))
        self.assertEqual(result['order']['slot']['activity'],'钢琴')
        self.assertEqual(result['order']['slot']['time'],'18:00-20:00')
        self.assertIn('钢琴',messages[0])
        self.assertEqual(notify.call_args.args[1]['slot']['activity'],'钢琴')
        self.assertEqual(len(self.journal.entries),2)

    def test_wrong_project_grid_cannot_generate_an_order_candidate(self):
        with self.assertRaises(booking.BookingError):
            runner.select_slot(snapshot(),settings(activity='钢琴'))

    def test_mail_recovery_filters_on_the_selected_project(self):
        for activity,identifier in (('健身房','GYM-FIXTURE'),('钢琴','PIANO-FIXTURE')):
            selected=replace(target_slot(),activity=activity)
            entry=self.journal.begin('account',selected,[])
            self.journal.update(entry,'confirmed',order={**order(selected),'order_id':identifier})
        path=self.root/'outbox.json'
        runner.recover_payment_mail(settings(activity='钢琴'),mailbox(),self.store,self.journal,self.clock,path)
        box=mail_notice.Outbox(path,self.clock)
        self.assertEqual(len(box.records),1)
        self.assertEqual(box.records[0]['order']['slot']['activity'],'钢琴')


class ActivityUITests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.path=Path(self.temporary.name)/'config.toml'
        content=(Path(__file__).resolve().parents[1]/'config/config.example.toml').read_text(encoding='utf-8')
        self.path.write_text(content.replace('time_slots = []','time_slots = ["18:00-19:00"]'),encoding='utf-8')
        self.settings_patch=patch.object(poller_ui,'SETTINGS_PATH',self.path);self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.window=tk.Tk();self.window.withdraw()
        self.app=poller_ui.PollerApp(self.window)

    def tearDown(self):
        self.app.process=None;self.app.set_busy(False);self.app.close()

    def test_project_switch_sync_two_hours_and_config_round_trip(self):
        app=self.app
        self.assertEqual(str(app.daily_activity_choice.cget('state')),'readonly')
        app.variables['field'].set('旧健身房场地')
        app.daily_activity.set('钢琴');app.change_daily_activity()
        self.assertEqual(app.time_picker.get(),[])
        self.assertEqual(app.variables['field'].get(),'')
        self.assertEqual(app.time_picker.choice.cget('values'),'')
        app.apply_daily_options({'activity':'钢琴','activities':['健身房','钢琴','新项目'],
                                'time_slots':['12:00-13:00','13:00-14:00']})
        self.assertIn('新项目',app.daily_activity_choice.cget('values'))
        app.time_picker.duration.set('连续2小时');app.time_picker.refresh_choices()
        self.assertEqual(app.time_picker.choice.cget('values'),('12:00-14:00',))
        app.time_picker.selected.set('12:00-14:00');app.time_picker.add()
        self.assertTrue(app.save_form())
        with self.path.open('rb') as stream: saved=tomllib.load(stream)
        self.assertEqual(saved['booking']['activity'],'钢琴')
        self.assertEqual(saved['booking']['time_slots'],['12:00-14:00'])
        app.read_form()
        self.assertEqual(app.daily_activity.get(),'钢琴')
        self.assertEqual(app.time_picker.hour_choices,('12:00-13:00','13:00-14:00'))
        app.set_busy(True)
        self.assertEqual(str(app.daily_activity_choice.cget('state')),'disabled')
        app.set_busy(False)
        self.assertEqual(str(app.daily_activity_choice.cget('state')),'readonly')

    def test_sync_uses_selected_project_and_same_noon_window_as_date_preview(self):
        self.app.daily_activity.set('钢琴');self.app.change_daily_activity()
        self.app.variables['window_minutes'].set('60')
        process=MagicMock()
        current=datetime(2026,10,12,12,30,tzinfo=BEIJING)
        with patch.object(poller_ui,'beijing_now',return_value=current), \
                patch.object(poller_ui,'worker_command',return_value=['worker','options']) as command, \
                patch.object(poller_ui.subprocess,'Popen',return_value=process) as launch, \
                patch.object(poller_ui.threading,'Thread'):
            self.app.sync_daily_options()
        command.assert_called_once_with('booking','options')
        self.assertEqual(launch.call_args.args[0],['worker','options','--activity','钢琴','--date','2026-10-19'])
        self.assertEqual(self.app.running_command,'daily-options')

    def test_stale_response_cannot_replace_current_project_times(self):
        self.app.daily_activity.set('钢琴');self.app.change_daily_activity()
        with self.assertRaises(ValueError):
            self.app.apply_daily_options({'activity':'健身房','time_slots':['18:00-19:00']})
        self.assertEqual(self.app.time_picker.get(),[])
        self.assertEqual(self.app.time_picker.hour_choices,())

    def test_new_hours_preserve_priority_and_remove_invalid_candidates(self):
        picker=self.app.time_picker
        picker.set(['18:00-20:00','21:00-22:00','18:00-19:00'])
        removed=picker.set_hours(['18:00-19:00','19:00-20:00'])
        self.assertEqual(removed,1)
        self.assertEqual(picker.get(),['18:00-20:00','18:00-19:00'])


if __name__ == '__main__':
    unittest.main()
