"""Bounded live CAPTCHA checks. All real order writes remain blocked."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

from playwright_runtime import sync_playwright

from booking_flow import BookingBrowser, ProcessLock
from login_flow import LoginBrowser, atomic_json, load_config
from configuration import CONFIG_PATH
from probe_slider import SHOW
from session import ROOT
from runtime import ensure_venv
from slider import VERSION, drag_once, read_challenge

PRIVATE = ROOT / '.local/captcha'


def slider_benchmark(options):
    report = {'schema_version':1,'kind':'slider','algorithm':VERSION,
        'started_at':datetime.now(timezone.utc).isoformat(),'planned_samples':options.samples,
        'interval_seconds':options.interval,'real_order_created':False,'samples':[]}
    with ProcessLock(ROOT/'.local/booking/run.lock'), sync_playwright() as pw:
        b=BookingBrowser(load_config(CONFIG_PATH))
        try:
            b.start(pw,headless=not options.headed)
            b.gate.verification_active=True
            b.gate.captcha_limit=options.samples
            b.page.evaluate(SHOW)
            last_started=0
            for index in range(options.samples):
                wait=max(0,options.interval-(time.monotonic()-last_started))
                if wait:
                    b.page.wait_for_timeout(round(wait*1000))
                last_started=time.monotonic()
                if index:
                    b.page.evaluate(SHOW)
                    with b.page.expect_response(lambda r:r.url.split('?')[0].endswith('/captcha/get'),timeout=15000):
                        b.page.locator('.verifybox:visible .verify-refresh').click()
                popup,bg,part,_=read_challenge(b.page)
                sample_dir=PRIVATE/'slider-samples'/f'{index+1:02}'
                sample_dir.mkdir(parents=True,exist_ok=True)
                (sample_dir/'background.png').write_bytes(bg)
                (sample_dir/'piece.png').write_bytes(part)
                result=drag_once(b.page,b.gate)
                result['index']=index+1
                report['samples'].append(result)
                report['completed_samples']=len(report['samples'])
                report['passed']=sum(x['server_verified'] is True for x in report['samples'])
                report['pass_rate']=report['passed']/report['completed_samples']
                report['server_checks']=b.gate.captcha_checks
                report['network']=b.gate.events
                atomic_json(PRIVATE/'slider-benchmark.json',report)
                print(json.dumps({'sample':index+1,'passed':result['server_verified'],
                    'reason':result['reason'],'match':result.get('match')},ensure_ascii=False),flush=True)
                # Stop rather than probe further on server throttling or uncertain network state.
                if result.get('rep_code') in ('6201','6202','6203') or result['server_verified'] is None:
                    break
        finally:
            b.close()
    print(json.dumps({k:v for k,v in report.items() if k not in ('samples','network')},ensure_ascii=False))


def login_benchmark(options):
    config=load_config(CONFIG_PATH)
    if not config.get('auth',{}).get('username') or not config.get('auth',{}).get('password'):
        print('请先在本地config/config.toml填写账号密码。');return 2
    report={'schema_version':1,'kind':'login','engine':'ddddocr-1.6.1-cpu',
        'started_at':datetime.now(timezone.utc).isoformat(),'planned_samples':options.samples,
        'interval_seconds':options.interval,'samples':[]}
    with ProcessLock(PRIVATE/'login.lock'),sync_playwright() as pw:
        last_started=0
        for index in range(options.samples):
            delay=max(0,options.interval-(time.monotonic()-last_started))
            if delay: time.sleep(delay)
            last_started=time.monotonic()
            b=LoginBrowser(config,lambda *args:None)
            try:
                b.start(pw,headless=not options.headed)
                b.fill_credentials(config['auth']['username'],config['auth']['password'])
                result=b.automatic_login(max_attempts=2)
                if result['authenticated']:
                    b.save_and_verify()
                    result['session_restored']=json.loads((ROOT/'.local/auth/session-meta.json').read_text(encoding='utf-8'))['restored_and_verified']
                report['samples'].append(result)
                report['completed_samples']=len(report['samples'])
                report['passed']=sum(x['authenticated'] is True for x in report['samples'])
                report['pass_rate']=report['passed']/report['completed_samples']
                report['first_try_passed']=sum(x['authenticated'] and len(x['attempts'])==1 for x in report['samples'])
                report['first_try_rate']=report['first_try_passed']/report['completed_samples']
                atomic_json(PRIVATE/'login-benchmark.json',report)
                print(json.dumps({'sample':index+1,'authenticated':result['authenticated'],
                    'attempts':result['attempts'],'session_restored':result.get('session_restored')},ensure_ascii=False),flush=True)
                if not result['authenticated']: break
            finally:
                b.close()
    print(json.dumps({k:v for k,v in report.items() if k!='samples'},ensure_ascii=False))
    return 0 if report['passed']==report['completed_samples'] else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=('slider','login'))
    parser.add_argument('--samples',type=int,default=10)
    parser.add_argument('--interval',type=int,default=20)
    parser.add_argument('--headed',action='store_true')
    options=parser.parse_args()
    if not 1<=options.samples<=10 or options.interval<20:
        parser.error('每次最多10个样本，至少间隔20秒。')
    if options.kind=='login' and options.samples>5:
        parser.error('登录实测每次最多5次；请用 --samples 5。')
    PRIVATE.mkdir(parents=True,exist_ok=True)
    return slider_benchmark(options) if options.kind=='slider' else login_benchmark(options)


if __name__=='__main__':
    ensure_venv()
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        sys.exit(main())
    except Exception as error:
        import traceback
        atomic_json(PRIVATE/'benchmark-failure.json',{'error_type':type(error).__name__,
            'locations':[{'file':Path(x.filename).name,'function':x.name,'line':x.lineno} for x in traceback.extract_tb(error.__traceback__) if Path(x.filename).parent==Path(__file__).parent]})
        print('实测已停止，请检查本地状态；没有输出账号与会话。')
        sys.exit(3)
