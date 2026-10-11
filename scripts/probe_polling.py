"""Live read-only verification of two same-date refreshes; no booking or email."""
import json
from pathlib import Path
from urllib.parse import urlsplit
from runtime import ensure_venv
if __name__=='__main__': ensure_venv()
from playwright.sync_api import sync_playwright
from booking_flow import BookingBrowser, BookingError, ProcessLock
from login_flow import FlowError, LoginBrowser, atomic_json, load_config
from configuration import CONFIG_PATH
from poll_settings import beijing_now
from session import ROOT, HOST


def main(login_if_needed=False):
    with ProcessLock(ROOT/'.local/booking/run.lock'),sync_playwright() as pw:
        config=load_config(CONFIG_PATH)
        browser=BookingBrowser(config)
        try:
            try: browser.start(pw,headless=True)
            except FlowError:
                browser.close()
                if not login_if_needed: raise
                credentials=config.get('auth',{})
                if not credentials.get('username') or not credentials.get('password'): raise
                login=LoginBrowser(config,lambda *args:None)
                try:
                    login.start(pw,headless=True)
                    login.fill_credentials(credentials['username'],credentials['password'])
                    report=login.automatic_login(max_attempts=2)
                    atomic_json(ROOT/'.local/automation/login-recovery.json',report)
                    if not report['authenticated']: raise BookingError('请手动完成登录。')
                    login.save_and_verify()
                    report['session_restored']=json.loads((ROOT/'.local/auth/session-meta.json').read_text(encoding='utf-8'))['restored_and_verified']
                    atomic_json(ROOT/'.local/automation/login-recovery.json',report)
                finally: login.close()
                browser=BookingBrowser(config)
                browser.start(pw,headless=True)
            snapshot=browser.wait_ready()
            # Use an already visible date to test refresh semantics independently of the noon release.
            target=snapshot['dates'][-1]['isdate']
            browser.grid('健身房',target)
            responses=[]
            browser.page.on('response',lambda response:responses.append(response.status)
                if urlsplit(response.url).netloc==HOST and urlsplit(response.url).path=='/manage/fieldDetail/queryFieldSituation' else None)
            checks=[]
            for index in range(2):
                if index: browser.page.wait_for_timeout(20000)
                before=len(responses)
                fresh=browser.refresh_grid('健身房',target)
                checks.append({'date':fresh['selected_date']['isdate'],'activity':fresh['active_type']['isactiveName'],
                    'grid_requests':len(responses)-before,'http_statuses':responses[before:]})
            result={'checked_at':beijing_now().isoformat(),'read_only':True,'real_order_created':False,
                'email_sent':False,'interval_seconds':20,'checks':checks}
            if any(check['grid_requests']!=1 or check['http_statuses']!=[200] for check in checks):
                raise BookingError('余量刷新请求次数或响应不符合预期。')
            atomic_json(ROOT/'.local/automation/refresh-check.json',result)
            print(json.dumps(result,ensure_ascii=False,indent=2))
        finally: browser.close()


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    try: main('--login-if-needed' in sys.argv)
    except Exception as error:
        import traceback
        details={'exception_type':type(error).__name__,'frames':[
            {'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
            for frame in traceback.extract_tb(error.__traceback__)]}
        atomic_json(ROOT/'.local/automation/refresh-failure.json',details)
        print(json.dumps(details));raise SystemExit(2)
