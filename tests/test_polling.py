"""Time, throttling, recovery and SMTP invariants, with no school orders or real mail."""
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
import json
from pathlib import Path
import smtplib
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import booking_flow as booking
import mail_notice as mail
import poll_runner as runner
from poll_settings import BEIJING, MailSettings, PollSettings, SettingsError, load_settings, load_mail_settings


def settings(**changes):
    return replace(PollSettings(True,('18:00-19:00','19:00-20:00'),''),**changes)


def mailbox(**changes):
    return replace(MailSettings('smtp.example.test',465,'ssl','local@example.test',
        'secret-authorization-code','local@example.test','recipient@example.test'),**changes)


def target_slot(period='18:00-19:00',day='2026-10-18'):
    return booking.Slot('gym-venue','学生中心','健身房',day,'gym-field','健身房',period,'2.00',0,0)


def order(slot=None):
    return {'phase':'confirmed','simulated':False,'real_order_created':True,'state':'0',
        'order_id':'TEST-ORDER-123','slot':asdict(slot or target_slot()),'amount_due':(slot or target_slot()).price,
        'confirmed_at':'2026-10-11T04:00:01+00:00'}


def snapshot(periods=('18:00-19:00',),prices=None):
    return {'loading':False,'unpaid_order_blocks_booking':False,
        'venue':{'venueId':'gym-venue','venueName':'学生中心'},
        'active_type':{'isactiveName':'健身房'},'selected_date':{'isdate':'2026-10-18'},
        'fields':[{'fieldId':'gym-field','name':'健身房'}],
        'slots':[{'field_index':0,'entries':[{'status':0,'can_select':True,
            'time_range':period,'price':(prices or {}).get(period,'2.00')} for period in periods]}]}


class Clock:
    def __init__(self,moment=None): self.moment=moment or datetime(2026,10,11,12,tzinfo=BEIJING)
    def __call__(self): return self.moment
    def advance(self,seconds): self.moment+=timedelta(seconds=seconds)


class Browser:
    account='test-account-fingerprint'
    def __init__(self,clock,grids):
        self.clock=clock;self.grids=list(grids);self.read_times=[];self.commits=0;self.stages=0
        self.gate=SimpleNamespace(rate_limited=False,auth_failed=False)
        self.page=SimpleNamespace(wait_for_timeout=lambda milliseconds:clock.advance(milliseconds/1000))
        self.outcome='confirmed';self.on_stage=None;self.before_commit=None
        self.order_state='0';self.order_queries=[];self.existing_orders={}
    def check_no_pending(self): return []
    def query_order(self, identifier):
        self.order_queries.append(identifier)
        slot=self.existing_orders.get(identifier,target_slot())
        gate=booking.NetworkGate('simulate');gate.slot=slot
        payload=gate.simulated_order()
        payload['data']['orderStateId']=self.order_state
        return payload
    def refresh_grid(self,activity,day):
        if activity!='健身房' or day!='2026-10-18': raise AssertionError('wrong target')
        self.read_times.append(self.clock())
        value=self.grids.pop(0) if self.grids else None
        if isinstance(value,Exception): raise value
        return value
    def stage(self,slot):
        self.stages+=1
        if self.on_stage: self.on_stage()
        return {'terms':'checked'}
    def commit(self,slot,prepared,journal,before_submit=None):
        if self.before_commit: self.before_commit()
        if before_submit: before_submit()
        self.commits+=1
        entry=journal.begin(self.account,slot,[])
        result=order(slot)
        self.existing_orders[result['order_id']]=slot
        if self.outcome=='confirmed': journal.update(entry,'confirmed',order=result)
        else:
            result={'phase':self.outcome};journal.update(entry,self.outcome)
        return result


class SettingsTests(unittest.TestCase):
    def test_beijing_noon_and_seven_calendar_days_even_when_host_is_utc(self):
        config=settings()
        before=datetime(2026,12,31,3,59,tzinfo=timezone.utc)
        release=config.next_release(before)
        self.assertEqual(release,datetime(2026,12,31,12,tzinfo=BEIJING))
        self.assertEqual(config.target_date(release.date()),'2027-01-07')
        self.assertEqual(config.target_date(date(2028,2,23)),'2028-03-01')
        self.assertEqual(config.next_release(datetime(2026,10,11,12,15,tzinfo=BEIJING)).day,12)
    def test_during_window_release_stays_today(self):
        self.assertEqual(settings().next_release(datetime(2026,10,11,12,7,tzinfo=BEIJING)).hour,12)
    def test_limits_and_invalid_times_are_rejected(self):
        good={'polling':{'enabled':True},'booking':{'time_slots':['18:00-19:00']}}
        for key,value in [('interval_seconds',19),('interval_seconds',True),('window_minutes',1441),
                ('max_checks',-1),('manual_timeout_seconds',301)]:
            raw={**good,'polling':{key:value}}
            with self.subTest(key=key,value=value),self.assertRaises(SettingsError): PollSettings.read(raw)
        for times in ([],['23:00-00:00'],['18:30-19:30'],['18:00-21:00'],['18:00-19:00']*2):
            with self.subTest(times=times),self.assertRaises(SettingsError):
                PollSettings.read({'booking':{'time_slots':times}})
    def test_mail_requires_tls_and_sanitizes_header_injection(self):
        values=asdict(mailbox())
        for key,value in [('security','plain'),('sender','sender@example.test\r\nBcc:x@example.test'),
                ('recipient','Display Name <a@example.test>'),('port',True)]:
            with self.subTest(key=key),self.assertRaises(SettingsError): MailSettings.read({**values,key:value})
    def test_mail_password_env_and_repr_hide_credentials(self):
        raw={**asdict(mailbox()),'password':'','password_env':'TEST_SMTP_AUTHORIZATION'}
        with patch.dict('os.environ',{'TEST_SMTP_AUTHORIZATION':'local-secret'}): config=MailSettings.read(raw)
        self.assertEqual(config.password,'local-secret')
        self.assertNotIn('local-secret',repr(config));self.assertNotIn(config.recipient,repr(config))
    def test_unconfigured_file_cannot_arm_task(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'settings.toml';path.write_text('[polling]\nenabled=false\n',encoding='utf-8')
            with self.assertRaises(SettingsError): load_settings(path,require_enabled=True)
    def test_mail_setup_does_not_require_booking_times_or_enabled_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'reservation.toml'
            rows=['[polling]','enabled = false','[booking]','time_slots = []','[mail]']
            rows.extend(key+' = '+json.dumps(value) for key,value in asdict(mailbox()).items())
            path.write_text('\n'.join(rows),encoding='utf-8')
            self.assertEqual(load_mail_settings(path),mailbox())
            with self.assertRaises(SettingsError): load_settings(path,require_enabled=True)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.clock=Clock();self.event=threading.Event()
        stop_path=patch.object(runner,'STOP',self.root/'stop.json');stop_path.start();self.addCleanup(stop_path.stop)
        self.store=runner.DailyStore(self.root/'days.json');self.journal=booking.Journal(self.root/'journal.json')
        self.messages=[];self.notified=[]
    def make_runner(self,config=None,notifier=None,validate=None):
        def notify(account,result):
            # The order must already be durable when any SMTP call starts.
            persisted=runner.DailyStore(self.store.path).day(account,result['slot']['date'])
            self.assertEqual(persisted['phase'],'confirmed')
            self.notified.append(result);return 'sent'
        return runner.DailyRunner(config or settings(),mailbox(),store=self.store,clock=self.clock,
            wait=self.clock.advance,notifier=notifier or notify,stop=runner.StopSignal(self.event,self.clock),
            status=lambda phase,message,**data:self.messages.append((phase,message,data)),validate_config=validate)
    def run_day(self,browser,instance=None):
        return (instance or self.make_runner()).run_day(browser,self.journal,date(2026,10,11))
    def test_no_requests_before_noon_or_after_window(self):
        browser=Browser(self.clock,[snapshot()]);self.clock.advance(-1)
        self.assertEqual(self.run_day(browser)['phase'],'outside_window')
        self.clock.advance(901)
        self.assertEqual(self.run_day(browser)['phase'],'outside_window')
        self.assertEqual(browser.read_times,[]);self.assertEqual(browser.commits,0)
    def test_success_stops_polling_and_notifies_after_durable_confirmation(self):
        browser=Browser(self.clock,[None,snapshot(),snapshot()])
        result=self.run_day(browser)
        self.assertEqual((result['phase'],browser.commits,len(browser.read_times),len(self.notified)),('confirmed',1,2,1))
        self.assertGreaterEqual((browser.read_times[1]-browser.read_times[0]).total_seconds(),60)
    def test_priority_and_field_selection_ignore_amount(self):
        chosen=runner.select_slot(snapshot(('19:00-20:00','18:00-19:00')),settings())
        self.assertEqual(chosen.time,'18:00-19:00')
        chosen=runner.select_slot(snapshot(('18:00-19:00','19:00-20:00'),{'18:00-19:00':'3'}),settings())
        self.assertEqual(chosen.time,'18:00-19:00')
        self.assertEqual(chosen.price,'3.00')
        self.assertIsNone(runner.select_slot(snapshot(),settings(field='another field')))
    def test_two_hour_success_creates_one_order_and_one_reminder_then_stops(self):
        config=settings(time_slots=('18:00-20:00',))
        browser=Browser(self.clock,[snapshot(('18:00-19:00','19:00-20:00')),snapshot()])
        result=self.run_day(browser,self.make_runner(config))
        self.assertEqual((result['phase'],browser.commits,len(browser.read_times),len(self.notified)),('confirmed',1,1,1))
        self.assertEqual(result['order']['slot']['time'],'18:00-20:00')
        self.assertEqual(result['order']['amount_due'],'4.00')
    def test_two_hour_target_does_not_silently_fall_back_to_one_hour(self):
        config=settings(time_slots=('18:00-20:00',),max_checks=1)
        browser=Browser(self.clock,[snapshot(('18:00-19:00',))])
        result=self.run_day(browser,self.make_runner(config))
        self.assertEqual(result['phase'],'exhausted')
        self.assertEqual((browser.commits,self.notified),(0,[]))
    def test_max_checks_and_restart_do_not_reset_daily_budget(self):
        browser=Browser(self.clock,[None]*10)
        config=settings(max_checks=3)
        self.assertEqual(self.run_day(browser,self.make_runner(config))['phase'],'exhausted')
        self.assertEqual(len(browser.read_times),3)
        restarted=Browser(self.clock,[snapshot()])
        self.assertEqual(self.run_day(restarted,self.make_runner(config))['phase'],'exhausted')
        self.assertEqual(restarted.read_times,[])
    def test_restart_obeys_persisted_cooldown(self):
        record=self.store.day(Browser.account,'2026-10-18')
        self.store.update(record,checks=1,next_check_at=(self.clock()+timedelta(seconds=60)).isoformat())
        browser=Browser(self.clock,[snapshot()]);self.run_day(browser)
        self.assertEqual(browser.read_times[0].minute,1)
        self.assertEqual(record['checks'],2)
    def test_changed_time_configuration_still_cannot_make_second_daily_order(self):
        first=Browser(self.clock,[snapshot()]);self.run_day(first)
        second=Browser(self.clock,[snapshot(('19:00-20:00',))])
        self.run_day(second,self.make_runner(settings(time_slots=('19:00-20:00',))))
        self.assertEqual((second.read_times,second.commits),([],0))
    def test_mail_failure_never_causes_another_order_on_restart(self):
        bad=MagicMock(side_effect=OSError('fake smtp'))
        browser=Browser(self.clock,[snapshot()]);result=self.run_day(browser,self.make_runner(notifier=bad))
        self.assertEqual((result['phase'],result['mail_phase']),('confirmed','failed'))
        again=Browser(self.clock,[snapshot()]);self.run_day(again)
        self.assertEqual((browser.commits,again.commits,again.read_times),(1,0,[]))
    def test_unknown_order_stops_and_is_never_retried(self):
        browser=Browser(self.clock,[snapshot(),snapshot()]);browser.outcome='unknown'
        self.assertEqual(self.run_day(browser)['phase'],'attention')
        again=Browser(self.clock,[snapshot()]);self.run_day(again)
        self.assertEqual((browser.commits,again.commits,len(self.notified)),(1,0,0))
    def test_website_rejection_details_reach_polling_status_without_another_submission(self):
        browser=Browser(self.clock,[snapshot(),snapshot()]);browser.outcome='rejected'
        commit=browser.commit
        def denied(*args,**kwargs):
            result=commit(*args,**kwargs)
            result['server_feedback']={'http_status':200,'code':1003,'message':'已达到预约次数上限'}
            return result
        browser.commit=denied
        result=self.run_day(browser)
        self.assertEqual((result['phase'],browser.commits,len(self.notified)),('attention',1,0))
        self.assertIn('已达到预约次数上限',self.messages[-1][1])
        self.assertIn('错误码 1003',self.messages[-1][1])
        self.assertEqual(result['result']['server_feedback']['code'],1003)
    def test_preexisting_unresolved_order_stops_all_new_dates(self):
        self.journal.begin(Browser.account,target_slot(day='2026-10-17'),[])
        browser=Browser(self.clock,[snapshot()]);self.assertEqual(self.run_day(browser)['phase'],'attention')
        self.assertEqual(browser.read_times,[])
    def test_journal_recovery_rechecks_order_without_polling_or_resubmitting(self):
        entry=self.journal.begin(Browser.account,target_slot(),[])
        self.journal.update(entry,'confirmed',order=order())
        browser=Browser(self.clock,[snapshot()]);self.assertEqual(self.run_day(browser)['phase'],'confirmed')
        self.assertEqual((browser.commits,browser.read_times,len(self.notified)),(0,[],1))
    def test_stop_or_window_expiry_before_final_dispatch_blocks_order(self):
        for action in (lambda:self.event.set(),lambda:self.clock.advance(901)):
            with self.subTest(action=action):
                self.store=runner.DailyStore(self.root/f'days-{id(action)}.json')
                self.clock=Clock();self.event.clear()
                browser=Browser(self.clock,[snapshot()]);browser.before_commit=action
                self.assertEqual(self.run_day(browser)['phase'],'attention')
                self.assertEqual(browser.commits,0)
    def test_server_throttle_or_login_requirement_stops_queries(self):
        for flag in ('rate_limited','auth_failed'):
            self.store=runner.DailyStore(self.root/f'{flag}.json')
            browser=Browser(self.clock,[snapshot()]);setattr(browser.gate,flag,True)
            self.assertEqual(self.run_day(browser)['phase'],'attention')
            self.assertEqual(browser.commits,0)
    def test_bounded_read_timeouts_use_backoff(self):
        browser=Browser(self.clock,[runner.BrowserTimeout('fake timeout')]*5)
        result=self.run_day(browser)
        self.assertEqual((result['phase'],len(browser.read_times)),('attention',3))
        self.assertEqual([(b-a).total_seconds() for a,b in zip(browser.read_times,browser.read_times[1:])],[120,240])
    def test_slot_taken_before_staging_waits_for_next_check(self):
        browser=Browser(self.clock,[snapshot(),snapshot()])
        def stage():
            browser.on_stage=None;raise booking.SlotUnavailable('slot already gone')
        browser.on_stage=stage
        self.assertEqual(self.run_day(browser)['phase'],'confirmed')
        self.assertEqual(browser.commits,1)
        self.assertEqual((browser.read_times[1]-browser.read_times[0]).total_seconds(),60)
    def test_config_changed_during_polling_stops_before_request(self):
        browser=Browser(self.clock,[snapshot()])
        def changed(): raise SettingsError('configuration changed')
        self.assertEqual(self.run_day(browser,self.make_runner(validate=changed))['phase'],'attention')
        self.assertEqual(browser.read_times,[])
    def test_noon_window_also_bounds_empty_polls(self):
        browser=Browser(self.clock,[None]*10)
        self.run_day(browser,self.make_runner(settings(window_minutes=1,max_checks=20)))
        self.assertEqual(len(browser.read_times),1)
    def test_pending_order_blocks_polling(self):
        browser=Browser(self.clock,[snapshot()]);browser.check_no_pending=MagicMock(side_effect=booking.BookingError('pending'))
        self.assertEqual(self.run_day(browser)['phase'],'attention');self.assertEqual(browser.read_times,[])
    def test_throttled_while_staging_prevents_final_dispatch(self):
        browser=Browser(self.clock,[snapshot()])
        browser.on_stage=lambda:setattr(browser.gate,'rate_limited',True)
        self.assertEqual(self.run_day(browser)['phase'],'attention')
        self.assertEqual(browser.commits,0)


class DailyControllerTests(unittest.TestCase):
    def test_outer_scheduler_waits_until_noon_and_returns_to_next_day_after_one_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);clock=Clock(datetime(2026,10,11,11,59,59,tzinfo=BEIJING))
            store=runner.DailyStore(root/'days.json');browser=Browser(clock,[snapshot()]);phases=[]
            browser.start=MagicMock();browser.close=MagicMock()
            class Event:
                stopped=False
                def is_set(self): return self.stopped
                def wait(self,seconds): clock.advance(seconds)
            event=Event()
            def status(phase,message,**values):
                phases.append(phase)
                if phases.count('waiting')==2: event.stopped=True
            with patch.multiple(runner,ROOT=root,PRIVATE=root/'automation',STOP=root/'stop.json',STATUS=root/'status.json',
                    beijing_now=clock,publish=status),patch.object(runner,'DailyStore',return_value=store),\
                    patch.object(runner,'load_settings',return_value=(settings(),mailbox())),\
                    patch.object(runner,'load_config',return_value={'auth':{}}),\
                    patch.object(runner,'BookingBrowser',return_value=browser),\
                    patch.object(runner,'sync_playwright',return_value=MagicMock()),\
                    patch.object(runner,'recover_payment_mail'),patch.object(runner,'service_outbox'),\
                    patch.object(runner,'Outbox',return_value=SimpleNamespace(records=[])),\
                    patch.object(runner.DailyRunner,'notify',return_value='sent') as notify:
                runner.run_daily(root/'configuration.toml',event)
            self.assertEqual(browser.commits,1);notify.assert_called_once()
            self.assertEqual(browser.read_times,[datetime(2026,10,11,12,tzinfo=BEIJING)])
            self.assertEqual(phases.count('waiting'),2)
            self.assertEqual(phases[-1],'stopped');browser.close.assert_called_once()
    def test_automatic_login_success_with_zero_manual_timeout_is_saved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);metadata=root/'.local/auth/session-meta.json'
            metadata.parent.mkdir(parents=True);metadata.write_text('{"restored_and_verified":true}',encoding='utf-8')
            flow=MagicMock();flow.client_logged_in.return_value=True
            with patch.object(runner,'ROOT',root),patch.object(runner,'LoginBrowser',return_value=flow):
                self.assertTrue(runner.restore_login({'auth':{'username':'local','password':'secret'}},MagicMock(),SimpleNamespace(is_set=lambda:False),timeout=0))
            flow.automatic_login.assert_called_once();flow.save_and_verify.assert_called_once();flow.close.assert_called_once()
    def test_failed_automatic_login_waits_for_manual_then_closes_without_booking(self):
        clock=Clock();flow=MagicMock();flow.client_logged_in.return_value=False
        flow.page.wait_for_timeout.side_effect=lambda milliseconds:clock.advance(milliseconds/1000)
        with patch.object(runner,'LoginBrowser',return_value=flow),patch.object(runner,'publish') as status:
            self.assertFalse(runner.restore_login({'auth':{}},MagicMock(),SimpleNamespace(is_set=lambda:False),timeout=2,clock=clock))
        flow.save_and_verify.assert_not_called();flow.close.assert_called_once()
        self.assertEqual(status.call_args.args[0],'manual_login')


class MailTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'outbox.json';self.clock=Clock();self.config=mailbox()
    def outbox(self,sender): return mail.Outbox(self.path,self.clock,sender)
    def test_send_once_even_after_process_restart(self):
        calls=[]
        def sender(config,message,before):
            before();calls.append(message);return True
        box=self.outbox(sender);item=box.enqueue('account',order(),self.config)
        self.assertEqual(box.deliver(item,self.config),'sent')
        restored=self.outbox(sender);same=restored.enqueue('account',order(),self.config)
        self.assertEqual(restored.deliver(same,self.config),'sent');self.assertEqual(len(calls),1)
        self.assertNotIn(self.config.password,self.path.read_text(encoding='utf-8'))
        body=calls[0].get_content()
        self.assertIn('2026-10-18',body);self.assertIn('12:00:01',body)
        self.assertNotIn(self.config.password,body)
    def test_simulated_or_unverified_or_unsuccessful_orders_cannot_send(self):
        box=self.outbox(MagicMock())
        for changed in ({'simulated':True},{'real_order_created':False},{'phase':'accepted'},{'state':'3'}, {'order_id':'a\nb'}):
            with self.subTest(changed=changed),self.assertRaises(booking.BookingError):
                box.enqueue('account',{**order(),**changed},self.config)
        self.assertEqual(box.records,[])
    def test_temporary_smtp_failure_is_rate_limited_and_bounded_across_restarts(self):
        sender=MagicMock(side_effect=mail.MailFailure('retryable'))
        box=self.outbox(sender);item=box.enqueue('account',order(),self.config)
        box.deliver(item,self.config);box.deliver(item,self.config)
        self.assertEqual(sender.call_count,1)
        for _ in range(3):
            self.clock.advance(60);box=self.outbox(sender);box.deliver(box.records[0],self.config)
        self.assertEqual(sender.call_count,3)
    def test_ambiguous_smtp_delivery_is_retried_with_a_bound(self):
        def ambiguous(config,message,before): before();raise OSError('after DATA')
        sender=MagicMock(side_effect=ambiguous)
        box=self.outbox(sender);item=box.enqueue('account',order(),self.config)
        self.assertEqual(box.deliver(item,self.config),'unknown')
        self.clock.advance(600);restored=self.outbox(sender);restored.deliver(restored.records[0],self.config)
        self.assertEqual(sender.call_count,2)
        for _ in range(3):
            self.clock.advance(600);restored=self.outbox(sender);restored.deliver(restored.records[0],self.config)
        self.assertEqual(sender.call_count,3)
    def test_crash_while_sending_recovers_unknown_and_crash_before_data_retryable(self):
        for phase,expected in [('sending','unknown'),('connecting','retryable')]:
            box=self.outbox(MagicMock());item=box.enqueue('account',order(),self.config)
            item.update(phase=phase,attempts=1);box.save()
            self.assertEqual(self.outbox(MagicMock()).records[0]['phase'],expected)
    def test_sender_must_explicitly_confirm_acceptance(self):
        box=self.outbox(lambda config,message,before:None);item=box.enqueue('account',order(),self.config)
        self.assertEqual(box.deliver(item,self.config),'retryable')
    def test_ssl_context_durable_mark_and_no_quit_resend(self):
        client=MagicMock();events=[]
        client.login.side_effect=lambda *args:events.append('login')
        client.send_message.side_effect=lambda *args,**kwargs:events.append('send') or {}
        client.close.side_effect=OSError('close failed after acceptance')
        with patch.object(mail.smtplib,'SMTP_SSL',return_value=client) as ssl_factory:
            self.assertTrue(mail.smtp_send(self.config,EmailMessage(),lambda:events.append('durable')))
        self.assertEqual(events,['login','durable','send'])
        context=ssl_factory.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        client.quit.assert_not_called()
    def test_starttls_precedes_authentication_and_has_no_plain_fallback(self):
        client=MagicMock();events=[]
        for name in ('ehlo','starttls','login'):
            getattr(client,name).side_effect=lambda *args,_name=name,**kwargs:events.append(_name)
        client.send_message.return_value={}
        with patch.object(mail.smtplib,'SMTP',return_value=client):
            mail.smtp_send(mailbox(security='starttls',port=587),EmailMessage(),lambda:None)
        self.assertEqual(events,['ehlo','starttls','ehlo','login'])
        client.reset_mock();client.starttls.side_effect=smtplib.SMTPNotSupportedError('no TLS')
        with patch.object(mail.smtplib,'SMTP',return_value=client),self.assertRaises(mail.MailFailure) as caught:
            mail.smtp_send(mailbox(security='starttls'),EmailMessage(),lambda:None)
        self.assertEqual(caught.exception.state,'failed');client.login.assert_not_called()
    def test_auth_failure_is_permanent_and_network_after_data_unknown(self):
        client=MagicMock();client.login.side_effect=smtplib.SMTPAuthenticationError(535,b'bad secret')
        with patch.object(mail.smtplib,'SMTP_SSL',return_value=client),self.assertRaises(mail.MailFailure) as caught:
            mail.smtp_send(self.config,EmailMessage(),lambda:None)
        self.assertEqual(caught.exception.state,'failed');self.assertNotIn('bad secret',str(caught.exception))
        client.login.side_effect=None;client.send_message.side_effect=OSError('network interrupted')
        with patch.object(mail.smtplib,'SMTP_SSL',return_value=client),self.assertRaises(mail.MailFailure) as caught:
            mail.smtp_send(self.config,EmailMessage(),lambda:None)
        self.assertEqual(caught.exception.state,'unknown')
    def test_pre_connection_network_failure_can_retry(self):
        with patch.object(mail.smtplib,'SMTP_SSL',side_effect=OSError('network')),self.assertRaises(mail.MailFailure) as caught:
            mail.smtp_send(self.config,EmailMessage(),lambda:None)
        self.assertEqual(caught.exception.state,'retryable')
    def test_recover_mail_from_confirmed_journal_outside_polling_window(self):
        journal=booking.Journal(self.path.parent/'journal.json')
        entry=journal.begin('account',target_slot(),[]);journal.update(entry,'confirmed',order=order())
        store=runner.DailyStore(self.path.parent/'days.json');self.clock.advance(3600)
        runner.recover_payment_mail(settings(),self.config,store,journal,self.clock,self.path)
        runner.recover_payment_mail(settings(),self.config,store,journal,self.clock,self.path)
        self.assertEqual(len(mail.Outbox(self.path).records),1)
        self.assertEqual(store.day('account','2026-10-18')['phase'],'confirmed')


if __name__=='__main__': unittest.main()
