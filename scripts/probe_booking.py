"""Inspect one available non-gym selection up to the terms dialog; never submit."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from login_flow import LoginBrowser, atomic_json
from session import ROOT, load_config
from configuration import CONFIG_PATH

SNAPSHOT = (Path(__file__).parent / 'appointment_snapshot.js').read_text(encoding='utf-8')
CHECKOUT = (Path(__file__).parent / 'checkout_snapshot.js').read_text(encoding='utf-8')
READS = {
    '/manage/venue/queryVenueById',
    '/manage/fieldDetail/queryFieldReserveSituationIsFull',
    '/manage/fieldDetail/queryFieldSituation', '/venue/personal/checkRule',
    '/venue/moreSetting/getOne', '/venue/notice/newStadiumNotice',
    '/system/user/currentUser', '/system/user/getUserInfo',
}


def main():
    flow = LoginBrowser(load_config(CONFIG_PATH), lambda *args: None)
    events = []
    with sync_playwright() as pw:
        try:
            flow.browser = pw.chromium.launch(channel='chrome', headless=True)
            flow.context = flow.new_context(restore=True)
            flow.current_user()
            page = flow.page = flow.context.new_page()
            def guard(route):
                request = route.request
                path = urlsplit(request.url).path
                if request.method not in ('GET', 'HEAD', 'OPTIONS') and path not in READS:
                    events.append({'blocked': path})
                    route.abort()
                else:
                    route.continue_()
            page.route('**/*', guard)
            page.goto(flow.url, wait_until='domcontentloaded', timeout=45000)
            page.wait_for_function('(' + SNAPSHOT + ')()?.slots?.length > 0', timeout=30000)
            observations = []
            selected = None
            # One future date per activity; stop as soon as a suitable slot is found.
            date = (datetime.now() + timedelta(days=1)).date().isoformat()
            for name in ('台球', '舞蹈', '排练厅', '钢琴', '交谊厅'):
                snapshot = page.evaluate(SNAPSHOT)
                if name not in [x['name'] for x in snapshot['types']]:
                    continue
                if snapshot['active_type']['isactiveName'] != name:
                    with page.expect_response(lambda r: urlsplit(r.url).path == '/manage/fieldDetail/queryFieldSituation', timeout=25000):
                        page.get_by_role('tab', name=name, exact=True).click()
                snapshot = page.evaluate(SNAPSHOT)
                day = next((d for d in snapshot['dates'] if d['isdate'] == date), None)
                if not day:
                    continue
                if snapshot['selected_date']['isdate'] != date:
                    with page.expect_response(lambda r: urlsplit(r.url).path == '/manage/fieldDetail/queryFieldSituation', timeout=25000):
                        page.get_by_role('tab', name=day['viewStr'], exact=True).click()
                page.wait_for_function('!(' + SNAPSHOT + ')()?.loading')
                snapshot = page.evaluate(SNAPSHOT)
                candidates = []
                for column in snapshot['slots']:
                    for row, cell in enumerate(column['entries']):
                        if cell['can_select'] and cell['time_range'] >= '12:00':
                            candidates.append({'column': column['field_index'], 'row': row,
                                'field': column['field_name'], 'time': cell['time_range'], 'price': cell['price']})
                observations.append({'activity': name, 'date': date, 'available': len(candidates)})
                if not candidates:
                    page.wait_for_timeout(1500)
                    continue
                candidate = min(candidates, key=lambda x: (float(x['price']), x['time'], x['column']))
                cells = page.locator('.inner-seat-wrapper .inner-seat')
                expected = sum(len(x['entries']) for x in snapshot['slots'])
                if cells.count() != expected:
                    raise RuntimeError('Grid structure changed')
                cells.nth(candidate['row'] * len(snapshot['fields']) + candidate['column']).click()
                chosen = page.evaluate(CHECKOUT)
                if len(chosen['selected']) != 1 or chosen['selected'][0]['scheduleTime'] != candidate['time'] or chosen['selected'][0]['subSitename'] != candidate['field']:
                    raise RuntimeError('Selection mismatch')
                with page.expect_response(lambda r: urlsplit(r.url).path == '/venue/moreSetting/getOne', timeout=20000):
                    page.get_by_role('button', name='立即下单', exact=True).click()
                dialog = page.locator('.el-dialog:visible')
                dialog.wait_for(state='visible')
                selected = {'activity': name, 'date': date, **candidate, 'checkout': page.evaluate(CHECKOUT)}
                output = ROOT / '.local/inspection'
                output.mkdir(parents=True, exist_ok=True)
                dialog.screenshot(path=str(output / 'checkout-terms.png'))
                selected['dialog_controls'] = {
                    'buttons': dialog.get_by_role('button').all_text_contents(),
                    'checkbox_labels': dialog.locator('.el-checkbox__label').all_text_contents(),
                }
                break
            report = {'captured_at': datetime.now().astimezone().isoformat(), 'observations': observations,
                'candidate': selected, 'blocked_writes': events, 'real_order_created': False}
            atomic_json(ROOT / '.local/inspection/checkout-probe.json', report)
            print(json.dumps(report, ensure_ascii=False))
            return 0
        finally:
            flow.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
