"""Candidate polling uses fake clocks/browsers/mail. No live school orders."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import poll_runner as runner
import booking_flow as booking
from poll_settings import BEIJING, WaitlistSettings, SettingsError
from test_polling import Browser, Clock, mailbox, order, snapshot, target_slot


def plan(**changes):
    return replace(WaitlistSettings('2026-10-18','18:00-19:00'),**changes)


class WaitlistSettingsTests(unittest.TestCase):
    def test_scheduled_beijing_time_and_expiry_is_first_hour(self):
        config=plan(period='18:00-20:00',start_mode='scheduled',start_at='2026-10-11T04:02:00+00:00')
        self.assertEqual(config.start_time(),datetime(2026,10,11,12,2,tzinfo=BEIJING))
        self.assertEqual(config.expires_at(),datetime(2026,10,18,18,tzinfo=BEIJING))
        self.assertEqual(config.time_slots,('18:00-20:00',))
        self.assertNotIn('max_checks',config.public(Clock()()))

    def test_config_validation_seven_day_range_and_schedule_before_expiry(self):
        raw={'waitlist':{'date':'2026-10-18','time':'18:00-20:00'}}
        parsed=WaitlistSettings.read(raw)
        parsed.validate_start(Clock()())
        for changes in ({'date':'bad'},{'time':'18:00-21:00'},{'interval_seconds':19},
                {'interval_seconds':True},{'start_mode':'scheduled','start_at':'bad'},
                {'start_mode':'scheduled','start_at':'2026-10-18T18:00:00+08:00'}):
            with self.subTest(changes=changes),self.assertRaises(SettingsError):
                WaitlistSettings.read({'waitlist':{**raw['waitlist'],**changes}})
        for config in (plan(date='2026-10-10'),plan(date='2026-10-19')):
            with self.assertRaises(SettingsError): config.validate_start(Clock()())
        with self.assertRaises(SettingsError):
            parsed.validate_start(datetime(2026,10,18,18,tzinfo=BEIJING))


class WaitlistRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.clock=Clock();self.event=threading.Event()
        stop_path=patch.object(runner,'STOP',self.root/'stop.json');stop_path.start();self.addCleanup(stop_path.stop)
        self.store=runner.WaitlistStore(self.root/'waitlist.json')
        self.journal=booking.Journal(self.root/'journal.json');self.notified=[];self.phases=[]

    def instance(self, config=None, notifier=None, validate=None):
        def notify(account,result):
            saved=runner.WaitlistStore(self.store.path).task(account,config or plan())
            self.assertEqual(saved['phase'],'confirmed')
            self.notified.append(result);return 'sent'
        return runner.WaitlistRunner(config or plan(),mailbox(),store=self.store,clock=self.clock,
            wait=self.clock.advance,stop=runner.StopSignal(self.event,self.clock),notifier=notifier or notify,
            validate_config=validate,status=lambda phase,message,**data:self.phases.append(phase))

    def test_more_than_sixty_checks_and_one_hour_then_success_stops(self):
        browser=Browser(self.clock,[None]*70+[snapshot(),snapshot()])
        result=self.instance().run_task(browser,self.journal)
        self.assertEqual((result['phase'],result['checks'],browser.commits,len(self.notified)),('confirmed',71,1,1))
        self.assertGreater((browser.read_times[-1]-browser.read_times[0]).total_seconds(),3600)
        self.assertTrue(all((b-a).total_seconds()>=60 for a,b in zip(browser.read_times,browser.read_times[1:])))

    def test_waits_for_scheduled_time_without_early_reads(self):
        browser=Browser(self.clock,[snapshot()])
        self.instance(plan(start_mode='scheduled',start_at='2026-10-11T12:02:00+08:00')).run_task(browser,self.journal)
        self.assertEqual(browser.read_times,[datetime(2026,10,11,12,2,tzinfo=BEIJING)])

    def test_no_requests_when_expired_and_expiry_during_cooldown(self):
        self.clock.moment=datetime(2026,10,18,17,59,30,tzinfo=BEIJING)
        browser=Browser(self.clock,[None,snapshot()])
        result=self.instance().run_task(browser,self.journal)
        self.assertEqual((result['phase'],len(browser.read_times),browser.commits),('expired',1,0))
        again=Browser(self.clock,[snapshot()])
        self.instance().run_task(again,self.journal)
        self.assertEqual((again.read_times,again.commits),([],0))

    def test_two_hour_requires_both_and_commits_one_order(self):
        browser=Browser(self.clock,[snapshot(),snapshot(('18:00-19:00','19:00-20:00'))])
        result=self.instance(plan(period='18:00-20:00')).run_task(browser,self.journal)
        self.assertEqual((len(browser.read_times),browser.commits,len(self.notified)),(2,1,1))
        self.assertEqual((result['order']['slot']['time'],result['order']['amount_due']),('18:00-20:00','4.00'))

    def test_same_field_required_for_two_hours(self):
        grid=snapshot(('18:00-19:00','19:00-20:00'))
        grid['fields'].append({'fieldId':'other','name':'另一场地'})
        grid['slots'][0]['entries'][1]['can_select']=False
        grid['slots'].append({'field_index':1,'entries':[{'status':0,'can_select':True,'time_range':'19:00-20:00','price':'2.00'}]})
        self.assertIsNone(runner.select_slot(grid,plan(period='18:00-20:00')))

    def test_saved_cooldown_and_restart_after_manual_pause(self):
        record=self.store.task(Browser.account,plan())
        self.store.update(record,phase='paused',checks=8,next_check_at=(self.clock()+timedelta(seconds=60)).isoformat())
        self.store=runner.WaitlistStore(self.store.path)
        browser=Browser(self.clock,[snapshot()]);result=self.instance().run_task(browser,self.journal)
        self.assertEqual(browser.read_times[0],datetime(2026,10,11,12,1,tzinfo=BEIJING))
        self.assertEqual(result['checks'],9)

    def test_success_restart_and_mail_failure_never_make_another_order(self):
        browser=Browser(self.clock,[snapshot()])
        bad=MagicMock(side_effect=OSError('fake mail'))
        result=self.instance(notifier=bad).run_task(browser,self.journal)
        self.assertEqual(result['mail_phase'],'failed')
        again=Browser(self.clock,[snapshot()])
        self.instance(plan(interval_seconds=120)).run_task(again,self.journal)
        self.assertEqual((again.read_times,again.commits),([],0))

    def test_exact_period_journal_recovery_ignores_other_time(self):
        entry=self.journal.begin(Browser.account,target_slot('17:00-18:00'),[])
        self.journal.update(entry,'confirmed',order=order(target_slot('17:00-18:00')))
        browser=Browser(self.clock,[snapshot()])
        self.instance().run_task(browser,self.journal)
        self.assertEqual(browser.commits,1)

    def test_confirmed_journal_recovers_without_polling(self):
        entry=self.journal.begin(Browser.account,target_slot(),[])
        self.journal.update(entry,'confirmed',order=order())
        browser=Browser(self.clock,[snapshot()]);self.instance().run_task(browser,self.journal)
        self.assertEqual((browser.read_times,browser.commits,len(self.notified)),([],0,1))

    def test_unknown_order_is_not_retried_or_emailed(self):
        browser=Browser(self.clock,[snapshot()]);browser.outcome='unknown'
        result=self.instance().run_task(browser,self.journal)
        self.assertEqual(result['phase'],'attention')
        again=Browser(self.clock,[snapshot()]);self.instance().run_task(again,self.journal)
        self.assertEqual((again.read_times,again.commits,len(self.notified)),([],0,0))

    def test_stop_or_expiry_during_staging_prevents_submit(self):
        for action,phase in ((lambda:self.event.set(),'paused'),
                (lambda:setattr(self.clock,'moment',plan().expires_at()),'expired')):
            with self.subTest(phase=phase):
                self.event.clear();self.clock=Clock();self.store=runner.WaitlistStore(self.root/(phase+'.json'))
                browser=Browser(self.clock,[snapshot()]);browser.on_stage=action
                result=self.instance().run_task(browser,self.journal)
                self.assertEqual((result['phase'],browser.commits),(phase,0))

    def test_pending_order_throttle_and_changed_config_stop(self):
        for condition in ('pending','throttle','config'):
            with self.subTest(condition=condition):
                self.store=runner.WaitlistStore(self.root/(condition+'.json'));browser=Browser(self.clock,[snapshot()])
                validate=None
                if condition=='pending': browser.check_no_pending=MagicMock(side_effect=booking.BookingError('pending'))
                if condition=='throttle': browser.gate.rate_limited=True
                if condition=='config': validate=MagicMock(side_effect=booking.BookingError('changed'))
                result=self.instance(validate=validate).run_task(browser,self.journal)
                self.assertEqual((result['phase'],browser.commits),('attention',0))

    def test_read_failure_can_resume_after_user_restart_without_resetting_cooldown(self):
        record=self.store.task(Browser.account,plan())
        self.store.update(record,phase='attention',reason='read_timeouts',checks=3,
            next_check_at=(self.clock()+timedelta(seconds=120)).isoformat())
        browser=Browser(self.clock,[snapshot()]);result=self.instance().run_task(browser,self.journal)
        self.assertEqual((result['phase'],result['checks'],browser.commits),('confirmed',4,1))
        self.assertEqual(browser.read_times,[datetime(2026,10,11,12,2,tzinfo=BEIJING)])


class WaitlistControllerTests(unittest.TestCase):
    def test_scheduler_waits_before_opening_browser_and_exits_after_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);clock=Clock();browser=Browser(clock,[snapshot()]);started=[];phases=[]
            browser.start=lambda *args,**kwargs:started.append(clock());browser.close=MagicMock()
            class Event:
                def is_set(self): return False
                def wait(self, seconds): clock.advance(seconds)
            config=plan(start_mode='scheduled',start_at='2026-10-11T12:02:00+08:00')
            with patch.multiple(runner,ROOT=root,PRIVATE=root/'automation',STOP=root/'stop.json',
                    beijing_now=clock,publish=lambda phase,*args,**kwargs:phases.append(phase)),\
                    patch.object(runner,'load_waitlist_settings',return_value=(config,mailbox())),\
                    patch.object(runner,'load_config',return_value={'auth':{}}),\
                    patch.object(runner,'BookingBrowser',return_value=browser),\
                    patch.object(runner,'sync_playwright',return_value=MagicMock()),\
                    patch.object(runner,'service_outbox'),patch.object(runner,'finish_waitlist_mail'),\
                    patch.object(runner.WaitlistRunner,'notify',return_value='sent') as notify:
                runner.run_waitlist(root/'config.toml',Event())
            self.assertEqual(started,[datetime(2026,10,11,12,2,tzinfo=BEIJING)])
            self.assertEqual(browser.commits,1);notify.assert_called_once();browser.close.assert_called_once()
            self.assertEqual(phases.count('waiting'),1)

    def test_expired_plan_does_not_open_browser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);clock=Clock(plan().expires_at())
            with patch.multiple(runner,ROOT=root,PRIVATE=root/'automation',STOP=root/'stop.json',
                    beijing_now=clock,publish=MagicMock()),\
                    patch.object(runner,'load_waitlist_settings',return_value=(plan(),mailbox())),\
                    patch.object(runner,'service_outbox'),patch.object(runner,'BookingBrowser') as browser:
                runner.run_waitlist(root/'config.toml',threading.Event())
            browser.assert_not_called()


if __name__=='__main__': unittest.main()
