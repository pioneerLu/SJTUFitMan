"""Daily noon booking and continuous candidate polling, with durable orders and mail."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
import time

from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright_runtime import sync_playwright

from booking_flow import BookingBrowser, BookingError, Journal, ProcessLock, SlotUnavailable, candidates
from login_flow import FlowError, LoginBrowser, atomic_json, load_config
from mail_notice import OUTBOX, Outbox, service_outbox, notify_payment
from poll_settings import BEIJING, SETTINGS_PATH, beijing_now, load_settings, load_waitlist_settings
from session import ROOT
from booking_feedback import failure_detail
from browser_runtime import BrowserError

PRIVATE = ROOT / '.local/automation'
STATUS = PRIVATE / 'status.json'
STOP = PRIVATE / 'stop.json'


class DailyStore:
    def __init__(self, path=None):
        self.path = Path(path or PRIVATE/'days.json')
        self.days = {}
        if self.path.exists():
            raw=json.loads(self.path.read_text(encoding='utf-8'))
            if raw.get('schema_version')!=1 or not isinstance(raw.get('days'),dict):
                raise BookingError('每日预约记录格式错误，已停止。')
            self.days=raw['days']

    def day(self, account, target):
        key=hashlib.sha256((account+'|'+target).encode()).hexdigest()
        if key not in self.days:
            self.days[key]={'account':account,'target_date':target,'phase':'polling',
                'checks':0,'created_at':beijing_now().isoformat()}
            self.save()
        return self.days[key]

    def update(self, record, **values):
        record.update(**values)
        self.save()

    def save(self):
        atomic_json(self.path,{'schema_version':1,'days':self.days})


class StopSignal:
    def __init__(self, event=None, clock=beijing_now):
        self.event=event or threading.Event()
        self.started_at=clock()

    def is_set(self):
        if self.event.is_set(): return True
        try:
            data=json.loads(STOP.read_text(encoding='utf-8'))
            return datetime.fromisoformat(data['requested_at'])>=self.started_at
        except (OSError,ValueError,KeyError): return False


def publish(phase, message, **values):
    sticky={}
    try:
        previous=json.loads(STATUS.read_text(encoding='utf-8'))
        for key in ('last_order','last_mail_phase'):
            if key in previous: sticky[key]=previous[key]
    except (OSError,ValueError): pass
    if values.get('order',{}).get('phase')=='confirmed':
        sticky['last_order']=values['order']
        sticky['last_mail_phase']=values.get('mail_phase','sending')
    atomic_json(STATUS,{'phase':phase,'message':message,'updated_at':beijing_now().isoformat(),**sticky,**values})


def select_slot(snapshot, settings):
    if snapshot is None: return None
    if snapshot.get('active_type',{}).get('isactiveName') != settings.activity:
        raise BookingError('网站当前项目与预约计划不一致，已停止。')
    for period in settings.time_slots:
        choices=candidates(snapshot,period,settings.field or None)
        if choices: return choices[0]
    return None


def restore_login(config, playwright, stop, timeout=300, clock=beijing_now, end_at=None):
    """Use local OCR at most twice; retain a visible login page for human takeover."""
    flow=LoginBrowser(config,lambda kind,value:publish('login',value) if kind in ('status','error') else None)
    try:
        if end_at is not None and clock() >= end_at: return False
        flow.start(playwright,headless=False)
        if end_at is not None and clock() >= end_at: return False
        credentials=config.get('auth',{})
        if credentials.get('username') and credentials.get('password'):
            flow.fill_credentials(credentials['username'],credentials['password'])
            flow.automatic_login()
        deadline=clock()+timedelta(seconds=timeout)
        if end_at is not None: deadline=min(deadline,end_at)
        while not stop.is_set():
            if end_at is not None and clock() >= end_at: return False
            if flow.client_logged_in():
                flow.save_and_verify()
                meta=json.loads((ROOT/'.local/auth/session-meta.json').read_text(encoding='utf-8'))
                if meta.get('restored_and_verified'): return True
                return False
            if clock()>=deadline: break
            publish('manual_login','请在打开的浏览器窗口手动完成登录。')
            flow.page.wait_for_timeout(1000)
        return False
    finally:
        flow.close()


class DailyRunner:
    completion_message = '预约已确认，已停止当日轮询。正在发送付款提醒。'
    def __init__(self, settings, mail, store=None, clock=beijing_now, wait=None,
            notifier=None, stop=None, status=publish, validate_config=None):
        self.settings=settings;self.mail=mail
        self.store=store or DailyStore()
        self.clock=clock
        self.stop=stop or StopSignal(clock=clock)
        self.wait=wait or (lambda seconds: time.sleep(min(seconds,1)))
        self.notifier=notifier or self.notify
        self.status=status
        self.validate_config=validate_config or (lambda:None)

    def wait_until(self, moment, browser=None):
        while not self.stop.is_set() and self.clock()<moment:
            seconds=min(1,(moment-self.clock()).total_seconds())
            if browser is not None: browser.page.wait_for_timeout(round(seconds*1000))
            else: self.wait(seconds)
        return not self.stop.is_set()

    def notify(self, account, order):
        return notify_payment(account, order, self.mail, clock=self.clock)

    def complete(self, record, order):
        # Commit success to disk before SMTP; every restart now skips further booking.
        self.store.update(record,phase='confirmed',order=order)
        self.status('confirmed',self.completion_message,order=order)
        try: mail_phase=self.notifier(record['account'],order)
        except Exception: mail_phase='failed'
        self.store.update(record,mail_phase=mail_phase)
        self.status('confirmed','预约已确认；邮件已发送。' if mail_phase=='sent' else
            '预约已确认。邮件尚未确认发送，请查看邮件状态并自行付款。',
            order=order,mail_phase=mail_phase)
        return record

    def matches_entry(self, record, entry):
        return (entry['slot']['date']==record['target_date'] and entry['slot']['activity']==self.settings.activity)

    def recover(self, browser, journal, record):
        for entry in journal.entries:
            if entry['account']!=browser.account: continue
            if entry['phase'] in ('submitting','unknown','accepted','verification_required'):
                self.store.update(record,phase='attention',reason='previous_submission_unresolved')
                self.status('attention','上次下单尚未核实，请先在预约助手中核查个人订单。')
                return record
        for entry in journal.entries:
            if (entry['account']==browser.account and entry['phase']=='confirmed'
                    and self.matches_entry(record,entry)):
                return self.complete(record,entry['order'])
        return None

    def run_day(self, browser, journal, day):
        start,end=self.settings.window(day)
        if self.clock()<start or self.clock()>=end:
            return {'phase':'outside_window','real_order_created':False}
        target=self.settings.target_date(day)
        record=self.store.day(browser.account,target)
        if (record['phase']=='exhausted' and self.clock()<end and
                (not self.settings.max_checks or record['checks']<self.settings.max_checks)):
            self.store.update(record,phase='polling')
        self.store.update(record,stop_at=end.isoformat())
        return self.run_target(browser,journal,target,start,end,record,self.settings.max_checks or None,
            end_phase='exhausted')

    def run_target(self, browser, journal, target, start, end, record, max_checks=None, end_phase='expired'):
        if record['phase']=='confirmed':
            return self.complete(record,record['order'])
        if record['phase'] in ('attention','exhausted','rejected'):
            return record
        recovered=self.recover(browser,journal,record)
        if recovered: return recovered
        if self.stop.is_set() or self.clock()>=end:
            self.store.update(record,phase='paused' if self.stop.is_set() else
                ('exhausted' if max_checks is not None else end_phase))
            return record
        try: browser.check_no_pending()
        except Exception:
            self.store.update(record,phase='attention',reason='pending_order_or_query_failed')
            self.status('attention','个人订单未能通过检查，请先核查待付款订单。')
            return record
        errors=0
        while self.clock()<end and not self.stop.is_set() and (max_checks is None or record['checks']<max_checks):
            due=datetime.fromisoformat(record['next_check_at']) if record.get('next_check_at') else self.clock()
            if not self.wait_until(min(due,end),browser) or self.clock()>=end: break
            # Reserve the budget and cooldown BEFORE sending even a read request.
            self.store.update(record,phase='polling',checks=record['checks']+1,
                next_check_at=(self.clock()+timedelta(seconds=self.settings.interval_seconds)).isoformat())
            count=f"{record['checks']}/{max_checks}" if max_checks is not None else str(record['checks'])
            self.status('polling',f"正在检查{self.settings.activity}余量（第{count}次）。",
                target_date=target,checks=record['checks'])
            try:
                self.validate_config()
                if browser.gate.rate_limited or browser.gate.auth_failed:
                    raise BookingError('网站要求停止请求或重新登录。')
                snapshot=browser.refresh_grid(self.settings.activity,target)
                if browser.gate.rate_limited or browser.gate.auth_failed:
                    self.store.update(record,phase='attention',reason='server_throttled_or_auth_required')
                    self.status('attention','网站要求停止请求或重新登录，轮询已停止。')
                    return record
                errors=0
                slot=select_slot(snapshot,self.settings)
                if slot:
                    def permitted():
                        self.validate_config()
                        if (self.stop.is_set() or not start<=self.clock()<end
                                or browser.gate.rate_limited or browser.gate.auth_failed):
                            raise BookingError('轮询已停止或预约窗口结束，禁止继续下单。')
                    permitted()
                    browser.manual_timeout=max(0,min(self.settings.manual_timeout_seconds,
                        int((end-self.clock()).total_seconds())))
                    prepared=browser.stage(slot)
                    permitted()
                    journal.assert_can_submit(browser.account,slot)
                    result=browser.commit(slot,prepared,journal,before_submit=permitted)
                    if result['phase']=='confirmed':
                        return self.complete(record,result)
                    self.store.update(record,phase='attention',reason=result['phase'],result=result)
                    detail=failure_detail(result)
                    self.status('attention','本次预约未完成，已停止自动下单。'+('\n'+detail if detail else '请查看本地结果。'),result=result)
                    return record
            except SlotUnavailable:
                # Nothing was submitted; the next bounded round may choose a fresh slot.
                pass
            except BrowserTimeout:
                if any(e['phase'] in ('submitting','unknown','accepted','verification_required') for e in journal.entries):
                    self.store.update(record,phase='attention',reason='unknown_order')
                    return record
                errors+=1
                if errors>=3:
                    self.store.update(record,phase='attention',reason='read_timeouts')
                    self.status('attention','连续查询超时，轮询已停止。')
                    return record
            except Exception:
                unresolved=any(e['phase'] in ('submitting','unknown','accepted','verification_required')
                    for e in journal.entries if e['account']==browser.account)
                if max_checks is None and not unresolved and (self.stop.is_set() or self.clock()>=end): break
                self.store.update(record,phase='attention',reason='flow_stopped')
                self.status('attention','预约流程已停止。请检查会话、页面或个人订单；不会重复下单。')
                return record
            interval=min(600,self.settings.interval_seconds*(2**errors))
            self.store.update(record,next_check_at=(self.clock()+timedelta(seconds=interval)).isoformat())
        exhausted='exhausted' if max_checks is not None else end_phase
        self.store.update(record,phase='paused' if self.stop.is_set() else exhausted)
        self.status(record['phase'],'轮询已停止。' if self.stop.is_set() else '今日轮询已结束，没有生成预约订单。',
            target_date=target,checks=record['checks'])
        return record


class WaitlistStore(DailyStore):
    def __init__(self, path=None):
        super().__init__(path or PRIVATE/'waitlist.json')

    def task(self, account, settings):
        # Timing/frequency changes do not reset success or a saved cooldown.
        identity=[account,settings.date,settings.period,settings.field]
        # Preserve legacy gym keys so an update cannot reset its stored cooldown.
        if settings.activity!='健身房': identity.append(settings.activity)
        key=hashlib.sha256(json.dumps(identity,
            ensure_ascii=False).encode()).hexdigest()
        if key not in self.days:
            self.days[key]={'account':account,'target_date':settings.date,'time':settings.period,
                'activity':settings.activity,
                'field':settings.field,'phase':'polling','checks':0,'created_at':beijing_now().isoformat()}
            self.save()
        return self.days[key]


class WaitlistRunner(DailyRunner):
    completion_message='候补预约已确认，已停止轮询。正在发送付款提醒。'

    def __init__(self,*args,end_at=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.end_at=end_at

    def matches_entry(self, record, entry):
        slot=entry['slot']
        return (super().matches_entry(record,entry) and slot['time']==record['time']
            and (not record['field'] or record['field'] in (slot['field'],slot['field_id'])))

    def run_task(self, browser, journal):
        record=self.store.task(browser.account,self.settings)
        if record['phase']=='confirmed': return self.complete(record,record['order'])
        recovered=self.recover(browser,journal,record)
        if recovered: return recovered
        if record['phase'] in ('attention','rejected'):
            # A user-initiated restart may retry read failures after fixing login/network.
            # Ambiguous or rejected submissions require explicit order reconciliation.
            if record.get('reason') not in ('flow_stopped','read_timeouts','server_throttled_or_auth_required',
                    'pending_order_or_query_failed'):
                self.status('attention','此前候补未能完成，请先核查登录与个人订单。',reason=record.get('reason',''))
                return record
            self.store.update(record,phase='polling')
        start=self.settings.start_time(self.clock())
        end=min(self.settings.expires_at(),self.end_at or self.settings.end_time(start))
        end_phase='expired' if end==self.settings.expires_at() else 'deadline'
        self.store.update(record,stop_at=end.isoformat())
        if self.clock()>=end:
            self.store.update(record,phase=end_phase)
            self.status(end_phase,'目标时段已经开始，候补已结束。' if end_phase=='expired' else
                '已到设置的时间上限，候补已停止，没有生成预约订单。')
            return record
        if not self.wait_until(start):
            self.store.update(record,phase='paused')
            return record
        result=self.run_target(browser,journal,self.settings.date,start,end,record,end_phase=end_phase)
        if result['phase']=='expired':
            self.status('expired','目标时段已经开始，候补已结束，没有生成预约订单。',checks=record['checks'])
        elif result['phase']=='deadline':
            self.status('deadline','已到设置的时间上限，候补已停止，没有生成预约订单。',checks=record['checks'])
        return result


def finish_waitlist_mail(outcome, store, mail, stop):
    """Only mail retry work after success. Never re-enter booking while notifying."""
    if outcome['phase']!='confirmed': return
    for _ in range(mail.max_attempts-1):
        if stop.is_set(): break
        try:
            item=next(item for item in Outbox(recover=False).records if item['account']==outcome['account']
                and item['order']['order_id']==outcome['order']['order_id'])
            if item['phase'] not in ('queued','retryable','unknown') or item['attempts']>=mail.max_attempts: break
            deadline=datetime.fromisoformat(item['next_attempt_at']) if item.get('next_attempt_at') else (
                beijing_now()+timedelta(seconds=mail.retry_interval_seconds))
            while not stop.is_set() and beijing_now()<deadline:
                stop.event.wait(min(5,max(0,(deadline-beijing_now()).total_seconds())))
            if stop.is_set(): break
            service_outbox(mail)
        except Exception: break
    try:
        item=next(item for item in Outbox(recover=False).records if item['account']==outcome['account']
            and item['order']['order_id']==outcome['order']['order_id'])
        store.update(outcome,mail_phase=item['phase'])
        publish('confirmed','候补预约已确认；邮件服务器已接受付款提醒。' if item['phase']=='sent' else
            '候补预约已确认，邮件未确认发送。请在平台核对并付款。',order=outcome['order'],mail_phase=item['phase'])
    except Exception: pass


def run_waitlist(settings_path=SETTINGS_PATH, stop_event=None):
    task_started_at=beijing_now()
    stop=StopSignal(stop_event,clock=beijing_now)
    store=WaitlistStore()
    with ProcessLock(PRIVATE/'runner.lock'):
        settings,mail=load_waitlist_settings(settings_path)
        start=settings.start_time(task_started_at)
        end=settings.end_time(start)
        journal=Journal(ROOT/'.local/booking/journal.json')
        # Recover the narrow crash between order confirmation and creating its email.
        with ProcessLock(ROOT/'.local/mail/send.lock'):
            box=Outbox()
            for entry in journal.entries:
                slot=entry['slot'];order=entry.get('order',{})
                if (entry['phase']=='confirmed' and slot['activity']==settings.activity and slot['date']==settings.date
                        and slot['time']==settings.period and (not settings.field or settings.field in (slot['field'],slot['field_id']))
                        and order.get('state') in ('0','9','1') and order.get('simulated') is False
                        and order.get('real_order_created') is True):
                    record=store.task(entry['account'],settings)
                    store.update(record,phase='confirmed',order=order)
                    box.enqueue(entry['account'],order,mail)
        # This also supports reminders after expiry; it does not query or create orders.
        try: service_outbox(mail)
        except Exception: publish('mail','邮件队列未能处理，请查看邮件状态。')
        current=beijing_now()
        if current>=settings.expires_at():
            publish('expired','目标时段已经开始，候补已结束。');return
        if current>=end:
            publish('deadline','已到设置的截止时间，候补已停止。');return
        settings.validate_start(current)
        publish('waiting',f"候补等待开始：北京时间{start.strftime('%Y-%m-%d %H:%M')}。",
            settings=settings.public(task_started_at))
        def unchanged():
            fresh,fresh_mail=load_waitlist_settings(settings_path)
            if fresh!=settings or fresh_mail!=mail:
                raise BookingError('候补配置在运行期间已修改，请停止后重新启动。')
        while not stop.is_set() and beijing_now()<start:
            unchanged()
            stop.event.wait(min(5,max(0,(start-beijing_now()).total_seconds())))
        if stop.is_set(): publish('paused','候补轮询已停止。');return
        unchanged()
        if beijing_now()>=end:
            publish('expired' if end==settings.expires_at() else 'deadline','已到候补截止时间，任务已停止。');return
        try:
            with ProcessLock(ROOT/'.local/booking/run.lock'),sync_playwright() as pw:
                config=load_config(settings_path)
                browser=BookingBrowser(config,mode='submit',manual_timeout=settings.manual_timeout_seconds)
                try:
                    try: browser.start(pw,headless=False,load_appointment=False)
                    except FlowError:
                        browser.close()
                        if not restore_login(config,pw,stop,settings.manual_timeout_seconds,clock=beijing_now,end_at=end):
                            if beijing_now()>=end:
                                publish('deadline','登录期间已到候补截止时间，任务已停止。');return
                            publish('attention','登录未完成，候补已停止。请完成登录后重新启动。');return
                        if stop.is_set() or beijing_now()>=end:
                            publish('paused' if stop.is_set() else 'deadline','候补已停止或已到截止时间。');return
                        browser=BookingBrowser(config,mode='submit',manual_timeout=settings.manual_timeout_seconds)
                        browser.start(pw,headless=False,load_appointment=False)
                    instance=WaitlistRunner(settings,mail,store=store,clock=beijing_now,stop=stop,
                        status=publish,validate_config=unchanged,end_at=end)
                    outcome=instance.run_task(browser,journal)
                finally: browser.close()
            finish_waitlist_mail(outcome,store,mail,stop)
        except BrowserError as error:
            publish('attention',str(error))
        except Exception:
            publish('attention','候补已停止。请检查网络、登录、页面或已有订单；不会重复下单。')


def recover_payment_mail(settings, mail, store, journal, clock=beijing_now, outbox_path=OUTBOX):
    """Recover a crash between journal confirmation and outbox creation, without web requests."""
    target=settings.target_date(clock().date())
    with ProcessLock(Path(outbox_path).parent/'send.lock'):
        outbox=Outbox(path=outbox_path,clock=clock)
        for entry in journal.entries:
            order=entry.get('order',{})
            if (entry['phase']=='confirmed' and entry['slot']['activity']==settings.activity
                    and entry['slot']['date']==target and order.get('state') in ('0','9','1')
                    and order.get('simulated') is False and order.get('real_order_created') is True):
                record=store.day(entry['account'],target)
                store.update(record,phase='confirmed',order=order)
                outbox.enqueue(entry['account'],order,mail)


def run_daily(settings_path=SETTINGS_PATH, stop_event=None):
    stop=StopSignal(stop_event,clock=beijing_now)
    store=DailyStore()
    last_release_day=None
    with ProcessLock(PRIVATE/'runner.lock'):
        publish('starting','每日任务启动中。')
        while not stop.is_set():
            try: settings,mail=load_settings(settings_path,require_enabled=True)
            except BookingError as error:
                publish('configuration',str(error));return
            try:
                recover_payment_mail(settings,mail,store,Journal(ROOT/'.local/booking/journal.json'))
                service_outbox(mail)
            except Exception: publish('mail','邮件队列尚未处理，请检查邮件配置与记录。')
            current=beijing_now()
            cutoff=settings.deadline()
            if cutoff is not None and current>=cutoff:
                publish('deadline','已到指定截止时间，每日任务已停止。');return
            release=settings.next_release(current)
            if last_release_day is not None and release.date()<=last_release_day:
                release=settings.window(last_release_day+timedelta(days=1))[0]
            waiting_for_cutoff=cutoff is not None and release>=cutoff
            if waiting_for_cutoff:
                # Wait locally until the global cutoff; do not start another round.
                release=cutoff
            publish('waiting',(f"本轮已结束，等待北京时间{release.strftime('%Y-%m-%d %H:%M')}停止每日任务。" if waiting_for_cutoff else
                f"等待北京时间{release.strftime('%Y-%m-%d %H:%M')}预约。"),
                settings=settings.public(release))
            while not stop.is_set() and beijing_now()<release:
                # Local clock/file checks only. No sports requests while waiting.
                stop.event.wait(min(5,max(0,(release-beijing_now()).total_seconds())))
            if stop.is_set(): break
            if cutoff is not None and beijing_now()>=cutoff:
                publish('deadline','已到指定截止时间，每日任务已停止。');return
            try: settings,mail=load_settings(settings_path,require_enabled=True)
            except BookingError as error:
                publish('configuration',str(error));return
            current=beijing_now()
            cutoff=settings.deadline()
            if cutoff is not None and current>=cutoff:
                publish('deadline','已到指定截止时间，每日任务已停止。');return
            if current>=settings.window(release.date())[1]:
                last_release_day=release.date();continue
            try:
                with ProcessLock(ROOT/'.local/booking/run.lock'),sync_playwright() as pw:
                    config=load_config(settings_path)
                    browser=BookingBrowser(config,mode='submit',manual_timeout=settings.manual_timeout_seconds)
                    try:
                        try: browser.start(pw,headless=False,load_appointment=False)
                        except FlowError:
                            browser.close()
                            if not restore_login(config,pw,stop,settings.manual_timeout_seconds,clock=beijing_now,
                                    end_at=settings.window(release.date())[1]):
                                if beijing_now()>=settings.window(release.date())[1]:
                                    last_release_day=release.date();continue
                                publish('attention','登录未完成，每日任务已停止。请完成登录后重新启动。');return
                            browser=BookingBrowser(config,mode='submit',manual_timeout=settings.manual_timeout_seconds)
                            browser.start(pw,headless=False,load_appointment=False)
                        def unchanged():
                            fresh,fresh_mail=load_settings(settings_path,require_enabled=True)
                            if fresh!=settings or fresh_mail!=mail:
                                raise BookingError('预约配置在运行期间已修改，请停止后重新启动。')
                        runner=DailyRunner(settings,mail,store=store,clock=beijing_now,stop=stop,status=publish,validate_config=unchanged)
                        outcome=runner.run_day(browser,Journal(ROOT/'.local/booking/journal.json'),release.date())
                    finally:
                        browser.close()
            except BrowserError as error:
                publish('attention',str(error));return
            except Exception:
                publish('attention','每日任务已停止。请检查网络、会话或已有订单。');return
            if outcome['phase']=='attention': return
            last_release_day=release.date()
            # Stop availability polling after success; service only queued mail retries.
            for _ in range(mail.max_attempts-1):
                if stop.is_set(): break
                try:
                    box=Outbox(recover=False)
                    if not any(item['phase'] in ('queued','retryable','unknown') and item['attempts']<mail.max_attempts for item in box.records): break
                except Exception: break
                deadline=beijing_now()+timedelta(seconds=mail.retry_interval_seconds)
                while not stop.is_set() and beijing_now()<deadline:
                    stop.event.wait(min(5,max(0,(deadline-beijing_now()).total_seconds())))
                if stop.is_set(): break
                try:
                    phases=service_outbox(mail)
                    if not phases['queued'] and not phases['retryable'] and not phases['unknown']: break
                except Exception: break
            if outcome['phase']=='confirmed':
                try:
                    item=next(item for item in Outbox(recover=False).records if item['account']==outcome['account']
                        and item['order']['order_id']==outcome['order']['order_id'])
                    store.update(outcome,mail_phase=item['phase'])
                    publish('confirmed','预约已确认；邮件已发送。' if item['phase']=='sent' else
                        '预约已确认，邮件未确认发送。请在平台核对并付款。',order=outcome['order'],mail_phase=item['phase'])
                except Exception: pass
        publish('stopped','每日预约程序已停止。')
