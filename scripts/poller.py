"""Local daily/candidate reservation and mail commands; no LLM or automatic payment."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from runtime import ensure_venv
if __name__=='__main__': ensure_venv()

from booking_flow import BookingBrowser, BookingError, ProcessLock
from login_flow import atomic_json, load_config
from mail_notice import MailFailure, send_test_mail, service_outbox, recover_confirmed_notices
from poll_runner import run_daily, run_waitlist, STOP, select_slot
from poll_settings import SETTINGS_PATH, beijing_now, load_settings, load_mail_settings, load_waitlist_settings, SettingsError
from session import ROOT
from browser_runtime import BrowserError


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings',type=Path,default=SETTINGS_PATH)
    parser.add_argument('command',choices=('check','run','waitlist','check-waitlist','stop','dry-run','test-mail','retry-mail','service-mail'))
    parser.add_argument('--time',help='Override a time slot only for read-only dry-run.')
    options=parser.parse_args(argv)
    if options.command=='stop':
        atomic_json(STOP,{'requested_at':beijing_now().isoformat()})
        print('已请求停止轮询。程序会完成正在核实的操作后退出。');return 0
    if options.time and options.command!='dry-run':
        raise BookingError('--time只用于不下单的检查。')
    if options.command in ('waitlist','check-waitlist'):
        settings,mail=load_waitlist_settings(options.settings)
        if options.command=='check-waitlist':
            print(json.dumps({**settings.public(),'mail_configured':True},ensure_ascii=False,indent=2))
        else: run_waitlist(options.settings)
        return 0
    if options.command in ('service-mail','retry-mail'):
        try:
            mail=load_mail_settings(options.settings)
        except SettingsError:
            count=recover_confirmed_notices()
            print(json.dumps({'not_configured':True,'retained':count},ensure_ascii=False))
            return 0
        recover_confirmed_notices(mail)
        print(json.dumps(service_outbox(mail,retry_failed=options.command=='retry-mail'),ensure_ascii=False))
        return 0
    if options.command=='test-mail':
        mail=load_mail_settings(options.settings)
        send_test_mail(mail)
        print('测试邮件已由SMTP服务器接受，请检查收件箱。')
        return 0
    if options.command=='dry-run' and options.time:
        from poll_settings import PollSettings
        import tomllib
        with options.settings.open('rb') as stream: config=tomllib.load(stream)
        config.setdefault('booking',{})['time_slots']=[options.time]
        settings=PollSettings.read(config);mail=None
    else:
        settings,mail=load_settings(options.settings,
            require_enabled=options.command=='run',require_mail=options.command!='dry-run')
    if options.command=='check':
        print(json.dumps({**settings.public(),'mail_configured':True},ensure_ascii=False,indent=2))
    elif options.command=='run':
        run_daily(options.settings)
    elif options.command=='dry-run':
        from playwright_runtime import sync_playwright
        with ProcessLock(ROOT/'.local/booking/run.lock'),sync_playwright() as pw:
            browser=BookingBrowser(load_config(options.settings))
            try:
                browser.start(pw,headless=True,load_appointment=False)
                target=settings.target_date(beijing_now().date())
                snapshot=browser.refresh_grid(settings.activity,target)
                slot=select_slot(snapshot,settings)
                result={'read_only':True,'real_order_created':False,'email_sent':False,
                    'activity':settings.activity,
                    'target_date':target,'time_slots':list(settings.time_slots),
                    'date_visible':snapshot is not None,'available':slot is not None,'selected_slot':None if slot is None else {
                        'field':slot.field,'time':slot.time,'price':slot.price},'network':browser.gate.events}
                atomic_json(ROOT/'.local/automation/dry-run.json',result)
                print(json.dumps(result,ensure_ascii=False,indent=2))
            finally: browser.close()
    return 0


if __name__=='__main__':
    if sys.stdout: sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr: sys.stderr.reconfigure(encoding='utf-8')
    try: sys.exit(main())
    except (BookingError,BrowserError) as error:
        print(str(error),file=sys.stderr);sys.exit(2)
    except MailFailure as error:
        print('邮件发送失败或结果不明，请检查本地邮箱配置；没有输出授权码。',file=sys.stderr);sys.exit(2)
    except Exception:
        print('流程已停止，请检查本地配置、网络或会话。没有输出账号与授权码。',file=sys.stderr);sys.exit(3)
