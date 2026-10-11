"""Local booking commands. Defaults never create a real order or make a payment."""
import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
from runtime import ensure_venv
if __name__=='__main__':
    ensure_venv()

from playwright.sync_api import sync_playwright
from booking_flow import BookingBrowser, BookingError, Journal, ProcessLock, Slot, candidates, confirmation_hash, hourly_periods
from login_flow import FlowError, atomic_json
from session import ROOT, load_config
from booking_options import website_options
from poll_settings import beijing_now
from configuration import CONFIG_PATH

PRIVATE = ROOT / '.local/booking'


def iso_date(value):
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError('日期请用 YYYY-MM-DD。') from None
    if result.isoformat() != value or result < beijing_now().date():
        raise argparse.ArgumentTypeError('日期必须是今天或将来的一天。')
    return value


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--config', type=Path, default=CONFIG_PATH)
    commands = result.add_subparsers(dest='command', required=True)
    choices = commands.add_parser('options', help='Read website date and time choices without creating an order.')
    choices.add_argument('--activity', default='健身房')
    choices.add_argument('--date', type=iso_date)
    for name in ('inspect', 'prepare', 'simulate'):
        command = commands.add_parser(name)
        command.add_argument('--activity', default='健身房' if name == 'inspect' else '台球')
        command.add_argument('--date', type=iso_date,
            default=(beijing_now().date() + timedelta(days=7 if name == 'inspect' else 1)).isoformat())
        command.add_argument('--time', required=name == 'prepare', default=None if name == 'inspect' else '12:00-13:00')
        command.add_argument('--field')
        command.add_argument('--headed', action='store_true')
        if name == 'simulate':
            command.add_argument('--scenario', choices=('success','verification','rejected','unknown'), default='success')
            command.add_argument('--manual-verification',action='store_true',help='Test human takeover with the final order requests mocked.')
    submit = commands.add_parser('submit', help='Create one real order after reviewing prepare output.')
    submit.add_argument('--confirm-reservation-sha256', required=True,
        help='Exact confirmation_sha256 printed by prepare; confirms this reservation and its terms.')
    submit.add_argument('--headed', action='store_true')
    submit.add_argument('--manual-timeout',type=int,default=300)
    commands.add_parser('reconcile', help='Read-only verification of an interrupted previous submission.')
    return result


def reconcile(browser, journal):
    results = []
    for entry in journal.entries:
        if entry['account'] != browser.account or entry['phase'] not in ('submitting', 'unknown', 'accepted', 'verification_required'):
            continue
        slot = Slot(**entry['slot'])
        identifiers = [entry['order_id']] if entry.get('order_id') else [
            str(x['pOrderid']) for x in browser.history() if str(x['pOrderid']) not in entry['baseline_ids']]
        matches = []
        from booking_flow import validate_order
        for identifier in identifiers:
            try:
                verified = validate_order(slot, browser.query_order(identifier))
                matches.append({'order_id': identifier, **verified})
            except BookingError:
                continue
        if len(matches) == 1:
            order = {'phase': 'confirmed', 'slot': asdict(slot), **matches[0],
                'simulated': False, 'real_order_created': True}
            journal.update(entry, 'confirmed', order_id=matches[0]['order_id'], order=order)
            results.append(order)
        else:
            # Absence from a finite recent-order page never proves that submission failed.
            results.append({'phase': 'unknown', 'matches': len(matches),
                'message': '未能唯一确认订单，继续禁止再次提交。请手动检查个人订单。'})
    return {'reconciliation': results, 'read_only': True}


def main(argv=None):
    options = parser().parse_args(argv)
    if hasattr(options,'manual_timeout') and not 0<=options.manual_timeout<=300:
        raise BookingError('手动等待时间须在0～300秒之间。')
    if hasattr(options, 'time') and options.time:
        hourly_periods(options.time)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    with ProcessLock(PRIVATE / 'run.lock'), sync_playwright() as pw:
        browser = BookingBrowser(load_config(options.config),
            mode='submit' if options.command == 'submit' else 'simulate' if options.command == 'simulate' else 'prepare',
            manual_timeout=getattr(options,'manual_timeout',300))
        try:
            browser.gate.simulation_scenario = getattr(options, 'scenario', 'success')
            browser.slider_auto=not getattr(options,'manual_verification',False)
            try:
                browser.start(pw, headless=not (options.command=='submit' or getattr(options,'headed',False)), load_appointment=options.command != 'reconcile')
            except FlowError:
                raise BookingError('登录会话已失效。请在“交大健身房助手”的“登录与会话”页重新登录并保存会话，再同步网站选项或预约。') from None
            if options.command == 'reconcile':
                print(json.dumps(reconcile(browser, Journal(PRIVATE / 'journal.json')), ensure_ascii=False, indent=2))
                return 0
            if options.command == 'options':
                snapshot = browser.activity_grid(options.activity)
                available_dates = [day['isdate'] for day in snapshot['dates'] if day['isdate'] >= beijing_now().date().isoformat()]
                if not available_dates:
                    raise BookingError('网站当前没有可选择的预约日期。')
                selected = options.date if options.date in available_dates else available_dates[-1]
                snapshot = browser.grid(options.activity, selected)
                result = website_options(snapshot, beijing_now().date())
                # Only safe choices are cached; signed date identifiers are omitted.
                atomic_json(PRIVATE / 'options.json', result)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if options.command == 'submit':
                path = PRIVATE / 'prepared.json'
                if not path.exists():
                    raise BookingError('请先运行 prepare 查看具体预约与须知。')
                saved = json.loads(path.read_text(encoding='utf-8'))
                if (saved.get('schema_version') != 1 or saved.get('account') != browser.account
                        or saved.get('confirmation_sha256') != confirmation_hash(saved)
                        or options.confirm_reservation_sha256 != saved.get('confirmation_sha256')):
                    raise BookingError('账号或确认的须知版本不一致，请重新准备。')
                slot = Slot(**saved['slot'])
                iso_date(slot.date)
                journal = Journal(PRIVATE / 'journal.json')
                journal.assert_can_submit(browser.account, slot)
                fresh = browser.stage(slot)
                if fresh['terms_sha256'] != saved['terms_sha256']:
                    raise BookingError('网站预订须知已变化，请重新查看 prepare 输出。')
                result = browser.commit(slot, fresh, journal)
                output_path = PRIVATE / 'last-order.json'
            else:
                snapshot = browser.grid(options.activity, options.date)
                slots = candidates(snapshot, options.time, options.field)
                if options.command == 'inspect':
                    result = {'activity': options.activity, 'date': options.date,
                        'available': len(slots), 'slots': [asdict(x) for x in slots], 'real_order_created': False}
                    result['options'] = website_options(snapshot, beijing_now().date())
                    output_path = PRIVATE / 'availability.json'
                else:
                    if not slots:
                        raise BookingError('指定项目、日期、时段与场地没有可选余量。')
                    slot = slots[0]
                    prepared = browser.stage(slot)
                    if options.command == 'prepare':
                        browser.check_no_pending()
                        result = prepared
                        result['real_order_created'] = False
                        output_path = PRIVATE / 'prepared.json'
                        browser.page.locator('.el-dialog:visible').screenshot(path=str(PRIVATE / 'prepared-terms.png'))
                    else:
                        result = browser.commit(slot, prepared)
                        output_path = PRIVATE / ('simulation.json' if options.scenario == 'success' else 'simulation-' + options.scenario + '.json')
            atomic_json(output_path, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print('本地结果：' + str(output_path))
            if result.get('phase') == 'confirmed':
                browser.page.locator('#orderDetails').screenshot(path=str(PRIVATE / ('simulation-order.png' if options.command == 'simulate' else 'order.png')))
            return 0 if result.get('phase', 'confirmed') == 'confirmed' else 2
        finally:
            browser.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    try:
        sys.exit(main())
    except BookingError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
    except (Exception, KeyboardInterrupt) as error:
        # Browser/library exception strings may contain tokens/headers. Never print them.
        import traceback
        frames = traceback.extract_tb(error.__traceback__)
        atomic_json(PRIVATE / 'failure.json', {'error_type': type(error).__name__,
            'locations': [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                          for f in frames if Path(f.filename).parent == Path(__file__).parent]})
        print('流程已停止。请检查网络、会话或本地结果；没有输出账号与会话信息。', file=sys.stderr)
        sys.exit(3)
