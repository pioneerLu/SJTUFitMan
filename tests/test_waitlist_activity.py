"""Candidate projects stay separate in UI, persisted cooldowns, orders and mail."""
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
import tomllib
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import booking_flow as booking
import mail_notice
import poll_runner as runner
import poller_ui
from poll_settings import BEIJING, SettingsError, WaitlistSettings
from test_polling import Browser, Clock, mailbox, order, snapshot, target_slot
from test_waitlist import plan


class WaitlistActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.clock=Clock()
        stop_path=patch.object(runner,'STOP',self.root/'stop.json');stop_path.start();self.addCleanup(stop_path.stop)

    def test_settings_default_to_gym_and_accept_the_selected_project(self):
        raw={'waitlist':{'date':'2026-10-18','time':'18:00-20:00'}}
        self.assertEqual(WaitlistSettings.read(raw).activity,'健身房')
        raw['waitlist']['activity']='台球'
        config=WaitlistSettings.read(raw)
        self.assertEqual(config.public(self.clock())['activity'],'台球')
        for activity in ('',None,True,'台球\n',' '*4,'a'*81):
            raw['waitlist']['activity']=activity
            with self.subTest(activity=activity), self.assertRaises(SettingsError):
                WaitlistSettings.read(raw)

    def test_legacy_gym_cooldown_is_retained_and_project_records_are_separate(self):
        config=plan();account=Browser.account
        legacy_key=hashlib.sha256(json.dumps([account,config.date,config.period,config.field],ensure_ascii=False).encode()).hexdigest()
        legacy={'account':account,'target_date':config.date,'time':config.period,'field':config.field,
                'phase':'paused','checks':7,'next_check_at':(self.clock()+timedelta(seconds=120)).isoformat()}
        path=self.root/'waitlist.json'
        path.write_text(json.dumps({'schema_version':1,'days':{legacy_key:legacy}}),encoding='utf-8')
        store=runner.WaitlistStore(path)
        gym=store.task(account,config)
        self.assertEqual((gym['checks'],gym['next_check_at']),(7,legacy['next_check_at']))
        piano=store.task(account,plan(activity='钢琴'))
        self.assertEqual((piano['phase'],piano['checks'],piano['activity']),('polling',0,'钢琴'))
        store.update(piano,phase='paused',checks=3,next_check_at=legacy['next_check_at'])
        reloaded=runner.WaitlistStore(path)
        self.assertEqual(reloaded.task(account,plan(activity='钢琴',interval_seconds=90))['checks'],3)
        self.assertEqual(reloaded.task(account,config)['checks'],7)

    def test_scheduled_two_hour_candidate_routes_and_notifies_the_selected_project(self):
        store=runner.WaitlistStore(self.root/'waitlist.json')
        journal=booking.Journal(self.root/'journal.json')
        old=journal.begin(Browser.account,target_slot('18:00-20:00'),[])
        journal.update(old,'confirmed',order=order(target_slot('18:00-20:00')))
        config=plan(activity='钢琴',period='18:00-20:00',start_mode='scheduled',
                    start_at='2026-10-11T12:02:00+08:00')
        grid=snapshot(('18:00-19:00','19:00-20:00'));grid['active_type']['isactiveName']='钢琴'
        browser=Browser(self.clock,[]);times=[]
        def read(activity,date):
            times.append(self.clock())
            self.assertEqual((activity,date),('钢琴','2026-10-18'))
            return grid
        browser.refresh_grid=read
        notify=MagicMock(return_value='sent')
        flow=runner.WaitlistRunner(config,mailbox(),store=store,clock=self.clock,wait=self.clock.advance,
            notifier=notify,stop=runner.StopSignal(clock=self.clock),status=MagicMock())
        result=flow.run_task(browser,journal)
        self.assertEqual(times,[datetime(2026,10,11,12,2,tzinfo=BEIJING)])
        self.assertEqual((result['phase'],browser.commits),('confirmed',1))
        self.assertEqual(result['order']['slot']['activity'],'钢琴')
        self.assertEqual(result['order']['slot']['time'],'18:00-20:00')
        self.assertEqual(notify.call_args.args[1]['slot']['activity'],'钢琴')
        flow.run_task(browser,journal)
        self.assertEqual(browser.commits,1)

    def test_expired_controller_recovers_only_matching_project_reminders(self):
        self.clock.moment=plan().expires_at()
        journal=booking.Journal(self.root/'.local/booking/journal.json')
        for activity,identifier in (('健身房','GYM-TEST'),('钢琴','PIANO-TEST')):
            selected=replace(target_slot(),activity=activity)
            entry=journal.begin('account',selected,[])
            journal.update(entry,'confirmed',order={**order(selected),'order_id':identifier})
        outbox=self.root/'outbox.json'
        with patch.multiple(runner,ROOT=self.root,PRIVATE=self.root/'automation',STOP=self.root/'stop.json',
                beijing_now=self.clock,publish=MagicMock()), \
                patch.object(runner,'load_waitlist_settings',return_value=(plan(activity='钢琴'),mailbox())), \
                patch.object(runner,'Outbox',side_effect=lambda:mail_notice.Outbox(outbox,self.clock)), \
                patch.object(runner,'service_outbox'),patch.object(runner,'BookingBrowser') as browser:
            runner.run_waitlist(self.root/'config.toml',threading.Event())
            runner.run_waitlist(self.root/'config.toml',threading.Event())
            browser.assert_not_called()
        box=mail_notice.Outbox(outbox,self.clock)
        self.assertEqual(len(box.records),1)
        self.assertEqual(box.records[0]['order']['slot']['activity'],'钢琴')


class WaitlistActivityUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'config.toml'
        self.path.write_text((Path(__file__).resolve().parents[1]/'config/config.example.toml').read_text(encoding='utf-8'),encoding='utf-8')
        self.config_patch=patch.object(poller_ui,'SETTINGS_PATH',self.path);self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.window=tk.Tk();self.window.withdraw();self.app=poller_ui.PollerApp(self.window,initial_tab='waitlist')

    def tearDown(self):
        self.app.process=None;self.app.set_busy(False);self.app.close()

    def test_independent_project_switch_two_hour_config_and_busy_state(self):
        app=self.app
        app.daily_activity.set('台球');app.change_daily_activity()
        app.waitlist_vars['field'].set('旧场地')
        app.waitlist_vars['activity'].set('钢琴');app.change_waitlist_activity()
        self.assertEqual(app.waitlist_vars['time'].get(),'')
        self.assertEqual(app.waitlist_vars['field'].get(),'')
        self.assertEqual(app.daily_activity.get(),'台球')
        current=datetime(2026,10,12,11,tzinfo=BEIJING)
        with patch.object(poller_ui,'beijing_now',return_value=current):
            app.apply_waitlist_options({'activity':'钢琴','activities':['健身房','台球','钢琴','新项目'],
                'dates':['2026-10-12','2026-10-18'],'date':'2026-10-18',
                'time_slots':['12:00-13:00','13:00-14:00']})
        app.waitlist_vars['duration'].set('连续2小时');app.waitlist_period_choices()
        self.assertEqual(app.waitlist_time_choice.cget('values'),('12:00-14:00',))
        app.waitlist_vars['time'].set('12:00-14:00')
        self.assertTrue(app.save_form())
        with self.path.open('rb') as stream: saved=tomllib.load(stream)
        self.assertEqual(saved['booking']['activity'],'台球')
        self.assertEqual(saved['waitlist']['activity'],'钢琴')
        self.assertEqual(saved['waitlist']['time'],'12:00-14:00')
        app.read_form()
        self.assertEqual(app.waitlist_vars['activity'].get(),'钢琴')
        self.assertEqual(app.waitlist_vars['time'].get(),'12:00-14:00')
        app.set_busy(True)
        self.assertEqual(str(app.waitlist_activity_choice.cget('state')),'disabled')
        app.set_busy(False)
        self.assertEqual(str(app.waitlist_activity_choice.cget('state')),'readonly')

    def test_sync_worker_uses_candidate_project_and_date(self):
        app=self.app
        app.waitlist_vars['activity'].set('钢琴');app.change_waitlist_activity()
        app.waitlist_vars['date'].set('2026-10-18（周日）')
        with patch.object(poller_ui,'worker_command',return_value=['worker','options']) as command, \
                patch.object(poller_ui.subprocess,'Popen') as process,patch.object(poller_ui.threading,'Thread'):
            app.launch('options')
        command.assert_called_once_with('booking','options')
        self.assertEqual(process.call_args.args[0],['worker','options','--activity','钢琴','--date','2026-10-18'])

    def test_stale_other_project_options_do_not_change_candidate_targets(self):
        app=self.app
        app.waitlist_vars['activity'].set('钢琴');app.change_waitlist_activity()
        with self.assertRaises(ValueError):
            app.apply_waitlist_options({'activity':'健身房','dates':['2026-10-18'],'time_slots':['18:00-19:00']})
        self.assertEqual(app.waitlist_hours,())
        self.assertEqual(app.waitlist_vars['time'].get(),'')


if __name__=='__main__': unittest.main()
