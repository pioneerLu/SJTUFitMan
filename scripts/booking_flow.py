"""Deterministic one- or two-hour booking with local OpenCV verification; no LLM."""
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import base64
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from login_flow import FlowError, LoginBrowser, atomic_json, user_fingerprint
from session import HOST, ORIGIN, ROOT
from runtime import asset_path
from booking_feedback import confirm_feedback, identity_redactions

SCRIPTS = asset_path('scripts')
SNAPSHOT = (SCRIPTS / 'appointment_snapshot.js').read_text(encoding='utf-8')
CHECKOUT = (SCRIPTS / 'checkout_snapshot.js').read_text(encoding='utf-8')
CONFIRM = '/venue/personal/ConfirmOrder'
QUERY = '/venue/personal/queryOrder'
HISTORY = '/venue/personal/personalOrderlist'
READ_POSTS = {
    '/manage/venue/queryVenueById',
    '/manage/fieldDetail/queryFieldReserveSituationIsFull',
    '/manage/fieldDetail/queryFieldSituation', '/venue/personal/checkRule',
    '/venue/moreSetting/getOne', '/venue/notice/newStadiumNotice',
    '/venue/TbNewBizVenue/getPositionUser', '/captcha/get',
}
READ_GETS = {QUERY, HISTORY, '/system/user/currentUser', '/venue/notice/newStadiumNotice'}
ORDER_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
SIM_ID = 'SJTUFitMan-SIMULATED-NOT-A-REAL-ORDER'
# Explicit terminal states from the platform's orderStatusName mapping.
RELEASED_ORDER_STATES = {'2': '已取消', '8': '支付超时取消', '23': '系统取消', '99': '已过期'}


class BookingError(FlowError):
    pass


class SlotUnavailable(BookingError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def money(value):
    if isinstance(value, bool):
        raise BookingError('网站金额格式变化，已停止。')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BookingError('网站金额格式变化，已停止。') from None
    if not result.is_finite() or result < 0 or result.as_tuple().exponent < -2:
        raise BookingError('网站金额格式不正确，已停止。')
    return result


def hourly_periods(value):
    """A reservation is one hour or two consecutive hours on the same day."""
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):00-(?:[01]\d|2[0-3]):00', value):
        raise BookingError('请选择整点的1小时或连续2小时时段。')
    start, end = int(value[:2]), int(value[6:8])
    if end - start not in (1, 2):
        raise BookingError('请选择整点的1小时或连续2小时时段。')
    return tuple(f'{hour:02d}:00-{hour+1:02d}:00' for hour in range(start, end))


def terms_hash(checkout):
    content = {'title': checkout.get('terms_title'), 'text': checkout.get('terms_text')}
    if not all(isinstance(x, str) and x.strip() for x in content.values()):
        raise BookingError('未能读取完整预订须知。')
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def confirmation_hash(prepared):
    # Bind consent to the full reviewed reservation, not just site-wide terms.
    reviewed = {key: prepared.get(key) for key in (
        'schema_version', 'prepared_at', 'account', 'slot', 'terms_sha256')}
    return hashlib.sha256(json.dumps(reviewed, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Slot:
    venue_id: str
    venue_name: str
    activity: str
    date: str
    field_id: str
    field: str
    time: str
    price: str
    column: int
    row: int
    hourly_prices: tuple = ()

    def __post_init__(self):
        # JSON restores tuples as lists. Normalize them before comparison/hash/consent.
        object.__setattr__(self, 'hourly_prices', tuple(self.hourly_prices))

    def parts(self):
        periods = hourly_periods(self.time)
        prices = self.hourly_prices or ((self.price,) if len(periods) == 1 else ())
        if len(prices) != len(periods) or sum((money(price) for price in prices), Decimal('0')) != money(self.price):
            raise BookingError('预约各小时金额与合计不一致，已停止。')
        return tuple((period, f'{money(price):.2f}') for period, price in zip(periods, prices))

    def key(self):
        # Price/layout changes do not permit a second order for the same target.
        identity = [self.venue_id, self.activity, self.date, self.field_id, self.time]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def candidates(snapshot, time_range=None, field=None):
    if not snapshot or snapshot.get('loading') or snapshot.get('unpaid_order_blocks_booking'):
        raise BookingError('页面仍在加载，或有未支付订单阻止预约。')
    venue = snapshot['venue']
    activity = snapshot['active_type']['isactiveName']
    date = snapshot['selected_date']['isdate']
    wanted = hourly_periods(time_range) if time_range else None
    result = []
    for column in snapshot['slots']:
        index = column['field_index']
        metadata = snapshot['fields'][index]
        if field and metadata['name'] != field:
            continue
        for row, cell in enumerate(column['entries']):
            if cell.get('status') not in (0, '0') or not cell.get('can_select'):
                continue
            if wanted and cell['time_range'] != wanted[0]:
                continue
            selected = [cell]
            if wanted and len(wanted) == 2:
                if row + 1 >= len(column['entries']):
                    continue
                second = column['entries'][row + 1]
                if (second.get('time_range') != wanted[1] or second.get('status') not in (0, '0')
                        or not second.get('can_select')):
                    continue
                selected.append(second)
            prices = tuple(f"{money(part.get('price')):.2f}" for part in selected)
            price = sum((money(value) for value in prices), Decimal('0'))
            result.append(Slot(venue['venueId'], venue['venueName'], activity, date,
                metadata['fieldId'], metadata['name'], time_range or cell['time_range'],
                f'{price:.2f}', index, row, prices if len(prices) == 2 else ()))
    return sorted(result, key=lambda x: (x.time, x.field_id))


def validate_selection(slot, checkout):
    selected = checkout.get('selected', []) if checkout else []
    expected = dict(slot.parts())
    if len(selected) != len(expected):
        raise BookingError('选择数量与目标预约小时数不一致。')
    seen = set()
    for item in selected:
        period = item.get('scheduleTime')
        if (period not in expected or period in seen or item.get('subSiteId') != slot.field_id
                or item.get('subSitename') != slot.field or type(item.get('venueNum')) is not int
                or item['venueNum'] != 1 or money(item.get('venuePrice')) != money(expected[period])):
            raise BookingError('已选场地、时间或金额与目标不一致，已停止。')
        seen.add(period)
    if money(checkout.get('total')) != money(slot.price):
        raise BookingError('已选场地、时间或金额与目标不一致，已停止。')


def classify_confirm(status, body):
    # Only a well-formed documented response establishes the outcome.
    if status in (401, 403):
        return 'auth_required', None
    if status != 200 or not isinstance(body, dict):
        return 'unknown', None
    code = body.get('code')
    if type(code) is int and code == 0:
        identifier = body.get('data')
        if isinstance(identifier, str) and ORDER_ID.fullmatch(identifier):
            return 'accepted', identifier
        return 'unknown', None
    if type(code) is int and code == 1002:
        return 'verification_required', None
    if type(code) is int and code == 10001:
        return 'profile_required', None
    if type(code) is int:
        return 'rejected', None
    return 'unknown', None


def inspect_order(slot, payload):
    if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0 or not isinstance(payload.get('data'), dict):
        raise BookingError('订单详情未能确认，禁止再次提交。')
    order = payload['data']
    spaces = order.get('spaces')
    expected = dict(slot.parts())
    if not isinstance(spaces, list) or len(spaces) != len(expected):
        raise BookingError('订单场地数量不匹配，禁止再次提交。')
    seen = set()
    for space in spaces:
        period = space.get('scheduleTime')
        if (period not in expected or period in seen or space.get('scheduleDate') != slot.date
                or space.get('subSitename') != slot.field
                or (space.get('subSiteId') is not None and space['subSiteId'] != slot.field_id)
                or space.get('venueName') != slot.activity
                or money(space.get('venuePrice')) != money(expected[period])):
            raise BookingError('服务器订单与预约目标不匹配，禁止再次提交。')
        seen.add(period)
    if order.get('venueName') != slot.venue_name or money(order.get('countPrice')) != money(slot.price):
        raise BookingError('服务器订单与预约目标不匹配，禁止再次提交。')
    state = str(order.get('orderStateId'))
    if state not in ('0', '9', '1') and state not in RELEASED_ORDER_STATES:
        raise BookingError('订单状态尚不能确认有效或已失效，请在体育平台核查。')
    return {'state': state, 'status': RELEASED_ORDER_STATES.get(state,
                '待付款' if state in ('0', '9') else '已支付'),
            'released': state in RELEASED_ORDER_STATES,
            'amount_due': f"{money(order.get('price')):.2f}"}


def validate_order(slot, payload):
    verified = inspect_order(slot, payload)
    if verified.pop('released'):
        raise BookingError('订单未处于可确认的待付款或已支付状态。')
    return verified


def reservation_released(entry):
    """Only an exact old order's verified terminal state releases duplicate protection."""
    current = entry.get('current_order') or {}
    identifier = (entry.get('order') or {}).get('order_id') or entry.get('order_id')
    return (bool(identifier) and current.get('order_id') == identifier
        and current.get('released') is True and current.get('state') in RELEASED_ORDER_STATES)


class ProcessLock(AbstractContextManager):
    """OS lock survives process crashes without leaving a stale lockfile gate."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise BookingError('另一个预约程序正在运行，当前程序已退出。') from None
        return self

    def __exit__(self, *exc):
        if self.stream:
            if os.name == 'nt':
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.entries = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('schema_version') != 1 or not isinstance(data.get('entries'), list):
                raise BookingError('预约记录格式无法识别，已停止。')
            self.entries = data['entries']

    def assert_can_submit(self, account, slot):
        for entry in self.entries:
            if entry['account'] != account:
                continue
            if entry['phase'] in ('submitting', 'unknown', 'verification_required', 'accepted'):
                raise BookingError('上次提交结果尚未核实，请先检查个人订单；禁止再次下单。')
            if entry['phase'] == 'confirmed' and not reservation_released(entry):
                previous = Slot(**entry['slot'])
                same_field = (previous.venue_id, previous.activity, previous.date, previous.field_id) == (slot.venue_id, slot.activity, slot.date, slot.field_id)
                if same_field and set(hourly_periods(previous.time)) & set(hourly_periods(slot.time)):
                    raise BookingError('该目标或其中一个小时已有已确认订单，禁止重复下单。')

    def refresh_order(self, browser, entry):
        if entry['account'] != browser.account or entry['phase'] != 'confirmed':
            raise BookingError('不能核查其他账号或尚未确认的订单。')
        order = entry.get('order') or {}
        if order.get('simulated') is not False or order.get('real_order_created') is not True:
            raise BookingError('旧记录尚不能确认为真实订单，请在体育平台核查。')
        if not order.get('slot') or Slot(**order['slot']).key() != Slot(**entry['slot']).key():
            raise BookingError('旧订单目标记录不一致，请在体育平台核查。')
        identifier = order.get('order_id') or entry.get('order_id')
        if not isinstance(identifier, str) or not ORDER_ID.fullmatch(identifier):
            raise BookingError('旧订单缺少有效订单号，请在体育平台核查。')
        if entry.get('order_id') and entry['order_id'] != identifier:
            raise BookingError('旧订单号不一致，请在体育平台核查。')
        current = {'order_id': identifier, **inspect_order(Slot(**entry['slot']), browser.query_order(identifier)),
            'checked_at': now()}
        # Preserve the original success event for durable mail recovery.
        self.update(entry, 'confirmed', current_order=current)
        return current

    def begin(self, account, slot, baseline_ids):
        self.assert_can_submit(account, slot)
        entry = {'account': account, 'slot_key': slot.key(), 'slot': asdict(slot),
                 'phase': 'submitting', 'created_at': now(), 'baseline_ids': baseline_ids}
        self.entries.append(entry)
        self.save()
        return entry

    def update(self, entry, phase, **values):
        entry.update(phase=phase, updated_at=now(), **values)
        self.save()

    def save(self):
        atomic_json(self.path, {'schema_version': 1, 'entries': self.entries})


class NetworkGate:
    """One initial submission and at most one verified continuation; mocks never forward."""
    def __init__(self, mode, before_send=None):
        self.mode = mode
        self.before_send = before_send
        self.armed = False
        self.attempts = 0
        self.slot = None
        self.events = []
        self.simulation_scenario = 'success'
        self.verification_active = False
        self.captcha_image_hash = None
        self.captcha_token_hash = None
        self.authorized_token_hash = None
        self.checked_tokens = set()
        self.captcha_checks = 0
        self.captcha_limit = 3
        self.verified_token_hash = None
        self.retry_armed = False
        self.before_verified_send = None
        self.manual_mode = False
        self.rate_limited = False
        self.auth_failed = False

    def observe_response(self, response):
        """Retain hashes only. A genuine successful check permits one verified submission."""
        parts = urlsplit(response.url)
        if parts.netloc==HOST:
            if response.status==429: self.rate_limited=True
            if response.status in (401,403): self.auth_failed=True
        if parts.scheme != 'https' or parts.netloc != HOST or response.status != 200:
            return
        try:
            if parts.path == '/captcha/get':
                body = response.json()
                self.captcha_token_hash = self.captcha_image_hash = None
                self.authorized_token_hash = None
                self.verified_token_hash = None
                if body.get('repCode') == '0000':
                    data = body['repData']
                    self.captcha_token_hash = hashlib.sha256(data['token'].encode()).hexdigest()
                    raw = [base64.b64decode(data[k], validate=True) for k in ('originalImageBase64','jigsawImageBase64')]
                    self.captcha_image_hash = hashlib.sha256(b''.join(raw)).hexdigest()
                    if self.manual_mode and self.verification_active:
                        self.authorized_token_hash = self.captcha_token_hash
            elif parts.path == '/captcha/check':
                body = response.json()
                token = response.request.post_data_json['token']
                key = hashlib.sha256(token.encode()).hexdigest()
                if body.get('repCode') == '0000' and key in self.checked_tokens:
                    self.verified_token_hash = key
        except Exception:
            # No malformed response can create authorization.
            return

    def authorize_captcha(self, image_hash):
        from captcha import CaptchaError
        if (not self.verification_active or not self.captcha_token_hash
                or self.captcha_image_hash != image_hash or self.captcha_checks >= self.captcha_limit
                or self.captcha_token_hash in self.checked_tokens):
            raise CaptchaError('验证码已变化或达到尝试上限，转为手动处理。')
        self.authorized_token_hash = self.captcha_token_hash

    def authorize_manual_captcha(self):
        if self.captcha_image_hash:
            self.authorize_captcha(self.captcha_image_hash)

    def simulated_order(self):
        slot = self.slot
        return {'code': 0, 'data': {'venueName': slot.venue_name, 'campusName': '模拟订单',
            'images': '', 'week': datetime.fromisoformat(slot.date).weekday() + 1,
            'spaces': [{'venueName': slot.activity, 'scheduleDate': slot.date,
                'scheduleTime': period, 'subSitename': slot.field, 'subSiteId': slot.field_id,
                'venuePrice': price} for period, price in slot.parts()], 'orderStateId': '0', 'maxNum': 1,
            'countPrice': slot.price, 'actPrice': '0', 'price': slot.price,
            'sumMoney': '0', 'arrears': '0', 'isEntourage': 1, 'accompanyings': []}}

    def __call__(self, route):
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path
        if path == '/captcha/check':
            try:
                data = request.post_data_json
                key = hashlib.sha256(data['token'].encode()).hexdigest()
                valid = (parts.scheme == 'https' and parts.netloc == HOST and request.method == 'POST'
                    and self.verification_active and data.get('captchaType') == 'blockPuzzle'
                    and key == self.authorized_token_hash and key not in self.checked_tokens
                    and self.captcha_checks < self.captcha_limit)
            except Exception:
                valid = False
            if not valid:
                self.events.append({'blocked':path})
                route.abort()
                return
            self.captcha_checks += 1
            self.checked_tokens.add(key)
            self.authorized_token_hash = None
            self.events.append({'verification_check':path})
            route.continue_()
            return
        if path == CONFIRM:
            verified_retry = (self.attempts == 1 and self.retry_armed and self.verified_token_hash
                and bool(request.headers.get('tid')) and self.before_verified_send is not None)
            if (self.mode == 'prepare' or parts.scheme != 'https' or parts.netloc != HOST
                    or request.method != 'POST' or not (self.armed or verified_retry)
                    or (self.attempts and not verified_retry) or self.attempts >= 2
                    or (self.mode == 'submit' and self.before_send is None)):
                self.events.append({'blocked': path})
                route.abort()
                return
            self.armed = False
            self.retry_armed = False
            self.attempts += 1
            # Persist intent BEFORE the browser is allowed to send anything.
            if verified_retry:
                self.before_verified_send()
            elif self.before_send:
                self.before_send()
            if self.mode == 'simulate':
                self.events.append({'mocked': path})
                responses = {
                    'success': (200, {'code': 0, 'data': SIM_ID}),
                    'verification': (200, {'code': 1002, 'msg': '模拟额外验证码'}),
                    'rejected': (200, {'code': 1003, 'msg': '模拟时段已被预订'}),
                    'unknown': (502, {'msg': '模拟结果不明'}),
                }
                status, body = responses['success' if verified_retry else self.simulation_scenario]
                route.fulfill(status=status, content_type='application/json', body=json.dumps(body))
            else:
                self.events.append({'forwarded': path})
                route.continue_()
            return
        if self.mode == 'simulate' and path == QUERY:
            self.events.append({'mocked': path})
            route.fulfill(status=200, content_type='application/json', body=json.dumps(self.simulated_order()))
            return
        # Never open a payment route, change/cancel an order, or modify a profile.
        if ('/payment/' in path or path == '/venue/personal/orderImmediatelyPC'
                or (request.resource_type in ('xhr', 'fetch') and (parts.netloc != HOST or parts.scheme != 'https'))
                or (request.resource_type in ('xhr', 'fetch') and request.method == 'GET' and path not in READ_GETS)
                or (request.method not in ('GET', 'HEAD', 'OPTIONS') and path not in READ_POSTS)):
            self.events.append({'blocked': path})
            route.abort()
            return
        route.continue_()


class BookingBrowser:
    def __init__(self, config, mode='prepare', manual_timeout=300):
        if mode not in ('prepare', 'simulate', 'submit'):
            raise BookingError('未知运行模式。')
        self.mode = mode
        self.login = LoginBrowser(config, lambda *args: None)
        self.gate = NetworkGate(mode)
        self.page = None
        self.account = None
        self.manual_timeout = manual_timeout
        self.headless = True
        self.slider_auto = True
        self.feedback_redactions = identity_redactions(config)

    def start(self, playwright, headless=True, load_appointment=True):
        self.headless = headless
        self.login.launch(playwright, headless=headless)
        self.login.context = self.login.new_context(restore=True)
        user = self.login.current_user()
        self.account = user_fingerprint(user)
        self.feedback_redactions.extend(identity_redactions(user))
        self.feedback_redactions.extend(cookie['value'] for cookie in self.login.context.cookies())
        self.page = self.login.page = self.login.context.new_page()
        self.page.route('**/*', self.gate)
        self.page.on('response', self.gate.observe_response)
        if not load_appointment:
            return
        self.page.goto(self.login.url, wait_until='domcontentloaded', timeout=45000)
        self.page.wait_for_function('(' + SNAPSHOT + ')()?.slots?.length > 0', timeout=30000)
        self.wait_ready()

    def wait_ready(self):
        self.page.wait_for_function('(() => { const s=(' + SNAPSHOT + ')(); return s && !s.loading && s.dates.length && s.slots.length; })()', timeout=25000)
        return self.page.evaluate(SNAPSHOT)

    def activity_grid(self, activity):
        snapshot = self.wait_ready()
        if activity not in [x['name'] for x in snapshot['types']]:
            raise BookingError('场馆内没有指定项目。')
        if snapshot['active_type']['isactiveName'] != activity:
            with self.page.expect_response(lambda r: urlsplit(r.url).path == '/manage/fieldDetail/queryFieldSituation', timeout=25000):
                self.page.get_by_role('tab', name=activity, exact=True).click()
            snapshot = self.wait_ready()
        if snapshot['active_type']['isactiveName'] != activity:
            raise BookingError('网站当前项目与目标不一致。')
        return snapshot

    def grid(self, activity, date):
        snapshot = self.activity_grid(activity)
        day = next((x for x in snapshot['dates'] if x['isdate'] == date), None)
        if not day:
            raise BookingError('目标日期尚未出现在网站可预约日期中。')
        if snapshot['selected_date']['isdate'] != date:
            with self.page.expect_response(lambda r: urlsplit(r.url).path == '/manage/fieldDetail/queryFieldSituation', timeout=25000):
                self.page.get_by_role('tab', name=day['viewStr'], exact=True).click()
            snapshot = self.wait_ready()
        if snapshot['active_type']['isactiveName'] != activity or snapshot['selected_date']['isdate'] != date:
            raise BookingError('网站当前项目或日期与目标不一致。')
        return snapshot

    def refresh_grid(self, activity, date):
        """Click the date tab again: the original weekClick issues one fresh grid query."""
        if not self.page.url.startswith(ORIGIN+'/pc/'):
            self.page.goto(self.login.url,wait_until='domcontentloaded',timeout=45000)
        snapshot=self.wait_ready()
        if date not in [day['isdate'] for day in snapshot['dates']]:
            self.page.reload(wait_until='domcontentloaded',timeout=45000)
            snapshot=self.wait_ready()
            if date not in [day['isdate'] for day in snapshot['dates']]: return None
        before=snapshot['active_type']['isactiveName']==activity and snapshot['selected_date']['isdate']==date
        snapshot=self.grid(activity,date)
        if before:
            day=next(day for day in snapshot['dates'] if day['isdate']==date)
            with self.page.expect_response(lambda r:urlsplit(r.url).path=='/manage/fieldDetail/queryFieldSituation',timeout=25000):
                self.page.get_by_role('tab',name=day['viewStr'],exact=True).click()
            snapshot=self.wait_ready()
        if snapshot['active_type']['isactiveName']!=activity or snapshot['selected_date']['isdate']!=date:
            raise BookingError('刷新后项目或日期不一致，已停止。')
        return snapshot

    def stage(self, slot):
        snapshot = self.grid(slot.activity, slot.date)
        available = candidates(snapshot, slot.time, slot.field)
        fresh = next((x for x in available if x.field_id == slot.field_id), None)
        if fresh is None or fresh.parts() != slot.parts():
            raise SlotUnavailable('目标时段已不可选，或金额已经变化。')
        cells = self.page.locator('.inner-seat-wrapper .inner-seat')
        if cells.count() != sum(len(c['entries']) for c in snapshot['slots']):
            raise BookingError('网站时段表格结构变化，请重新检查。')
        for offset, (period, price) in enumerate(slot.parts()):
            cells.nth((fresh.row + offset) * len(snapshot['fields']) + fresh.column).click()
        validate_selection(slot, self.page.evaluate(CHECKOUT))
        with self.page.expect_response(lambda r: urlsplit(r.url).path == '/venue/moreSetting/getOne', timeout=20000):
            self.page.get_by_role('button', name='立即下单', exact=True).click()
        self.page.locator('.el-dialog:visible').wait_for(state='visible')
        checkout = self.page.evaluate(CHECKOUT)
        validate_selection(slot, checkout)
        prepared = {'schema_version': 1, 'prepared_at': now(), 'account': self.account,
            'slot': asdict(slot), 'terms_sha256': terms_hash(checkout),
            'terms_title': checkout['terms_title'], 'terms_text': checkout['terms_text']}
        prepared['confirmation_sha256'] = confirmation_hash(prepared)
        return prepared

    def history(self):
        response = self.login.context.request.get(ORIGIN + HISTORY,
            params={'pageNo': 1, 'pageSize': 50}, timeout=15000, max_redirects=0)
        if response.status != 200:
            raise BookingError('个人订单查询失败，已停止下单。')
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get('records'), list):
            raise BookingError('个人订单数据格式变化，已停止下单。')
        records = body['records']
        if any(not isinstance(r, dict) or 'orderstateid' not in r or not ORDER_ID.fullmatch(str(r.get('pOrderid', ''))) for r in records):
            raise BookingError('个人订单关键字段缺失，已停止下单。')
        return records

    def check_no_pending(self):
        records = self.history()
        if any(str(x['orderstateid']) in ('0', '9') for x in records):
            raise BookingError('存在待付款订单，请先在个人订单处理。')
        return [str(x['pOrderid']) for x in records]

    def query_order(self, order_id):
        if not ORDER_ID.fullmatch(order_id):
            raise BookingError('订单号格式无法识别。')
        response = self.login.context.request.get(ORIGIN + QUERY,
            params={'orderId': order_id}, timeout=15000, max_redirects=0)
        if response.status != 200:
            raise BookingError('订单详情查询失败，禁止再次提交。')
        return response.json()

    def verify_and_continue(self, slot, prepared, journal, entry):
        """One automatic drag, then bounded human takeover in the same browser."""
        from slider import drag_once
        self.gate.verification_active = True
        def record_verified_intent():
            if getattr(self,'before_submit',None): self.before_submit()
            validate_selection(slot,self.page.evaluate(CHECKOUT))
            if terms_hash(self.page.evaluate(CHECKOUT)) != prepared['terms_sha256']:
                raise BookingError('验证期间须知变化，已停止。')
            grid=self.page.evaluate(SNAPSHOT)
            if grid['selected_date']['isdate']!=slot.date or grid['active_type']['isactiveName']!=slot.activity:
                raise BookingError('验证期间预约目标变化，已停止。')
            if journal is not None:
                journal.update(entry,'submitting',verified_submission=True)
        self.gate.before_verified_send=record_verified_intent
        self.gate.retry_armed=True
        finals=[]
        def final_response(response):
            if urlsplit(response.url).path==CONFIRM:
                finals.append(response)
        self.page.on('response',final_response)
        result=drag_once(self.page,self.gate) if self.slider_auto else {
            'phase':'manual_required','server_verified':False,'reason':'本次测试只验证手动接管。'}
        result['automatic_server_verified']=result['server_verified']
        atomic_json(ROOT/'.local/captcha/last-slider.json',result)
        if result['server_verified'] is not True:
            if self.headless or not self.manual_timeout:
                self.gate.retry_armed=False
                self.page.remove_listener('response',final_response)
                self.gate.verification_active=False
                atomic_json(ROOT/'.local/captcha/manual-status.json',{'waiting':False,'updated_at':now()})
                return None,result
            # A failed check refreshes automatically. Wait before accepting manual input.
            if self.slider_auto: self.page.wait_for_timeout(20000)
            self.gate.manual_mode=True
            try: self.gate.authorize_manual_captcha()
            except Exception: pass
            atomic_json(ROOT/'.local/captcha/manual-status.json',{
                'waiting':True,'message':'请在打开的浏览器窗口手动拖动滑块，最多等待5分钟。',
                'timeout_seconds':self.manual_timeout,'updated_at':now()})
        deadline=time.monotonic()+(self.manual_timeout if self.gate.manual_mode else 25)
        try:
            while time.monotonic()<deadline and not self.page.is_closed():
                if finals:
                    result['manual_used']=self.gate.manual_mode
                    if self.gate.manual_mode:
                        result['manual_server_verified']=bool(self.gate.verified_token_hash)
                        result['server_verified']=bool(self.gate.verified_token_hash)
                        result['phase']='passed' if result['server_verified'] else 'manual_required'
                    atomic_json(ROOT/'.local/captcha/last-slider.json',result)
                    return finals[0],result
                self.page.wait_for_timeout(250)
            return None,result
        finally:
            self.page.remove_listener('response',final_response)
            self.gate.manual_mode=False
            self.gate.retry_armed=False
            self.gate.verification_active=False
            atomic_json(ROOT/'.local/captcha/manual-status.json',{'waiting':False,'updated_at':now()})

    def commit(self, slot, prepared, journal=None, before_submit=None):
        if self.mode not in ('simulate', 'submit'):
            raise BookingError('检查模式不能提交订单。')
        checkout = self.page.evaluate(CHECKOUT)
        validate_selection(slot, checkout)
        if not checkout['dialog_visible'] or terms_hash(checkout) != prepared['terms_sha256']:
            raise BookingError('提交前条款发生变化，需重新查看。')
        entry = None
        if self.mode == 'submit':
            if journal is None:
                raise BookingError('缺少持久预约记录，已停止。')
            if user_fingerprint(self.login.current_user()) != self.account:
                raise BookingError('服务器账号发生变化，请重新登录。')
            baseline = self.check_no_pending()
            journal.assert_can_submit(self.account, slot)
            def record_intent():
                nonlocal entry
                if before_submit: before_submit()
                entry = journal.begin(self.account, slot, baseline)
            self.gate.before_send = record_intent
        dialog = self.page.locator('.el-dialog:visible')
        # Element UI hides its native checkbox; click its visible, observed label.
        if not checkout['checked']:
            dialog.locator('.el-checkbox__label').filter(has_text='本人已认真阅读、充分了解并自愿接受上述规定。').click()
        self.page.wait_for_function('(' + CHECKOUT + ')()?.checked === true')
        self.gate.slot = slot
        self.before_submit=before_submit
        if before_submit: before_submit()
        self.gate.armed = True
        try:
            with self.page.expect_response(lambda r: urlsplit(r.url).path == CONFIRM, timeout=55000) as pending:
                dialog.get_by_role('button', name='提交订单', exact=True).click()
            response = pending.value
            try:
                body = response.json()
            except Exception:
                body = None
            phase, identifier = classify_confirm(response.status, body)
            feedback = confirm_feedback(response.status, body, self.feedback_redactions)
            if entry is not None:
                journal.update(entry, phase, order_id=identifier, server_feedback=feedback)
            verification=None
            if phase=='verification_required':
                response,verification=self.verify_and_continue(slot,prepared,journal,entry)
                if response is not None:
                    try: body=response.json()
                    except Exception: body=None
                    phase,identifier=classify_confirm(response.status,body)
                    feedback=confirm_feedback(response.status,body,self.feedback_redactions)
                    if entry is not None: journal.update(entry,phase,order_id=identifier,server_feedback=feedback)
                elif self.gate.attempts>=2:
                    phase='unknown'
                    feedback=None
                    if entry is not None: journal.update(entry,phase,server_feedback=None)
            if phase != 'accepted':
                return {'phase': phase, 'slot': asdict(slot), 'simulated': self.mode == 'simulate',
                    'real_order_created': False if self.mode == 'simulate' else None, 'network': self.gate.events,
                    'verification':verification,'manual_required':phase=='verification_required',
                    'server_feedback':feedback}
            self.page.wait_for_url('**/pc/#/orderDetails/0', timeout=20000)
            self.page.wait_for_function("(" + CHECKOUT + ")()?.order?.venueName != null", timeout=20000)
            payload = self.gate.simulated_order() if self.mode == 'simulate' else self.query_order(identifier)
            verified = validate_order(slot, payload)
            visible = self.page.evaluate(CHECKOUT)
            if visible.get('component') != 'orderDetails' or str(visible['order']['orderStateId']) != verified['state']:
                raise BookingError('页面订单状态与服务器不一致。')
            output = {'phase': 'confirmed', 'order_id': identifier, 'slot': asdict(slot), **verified,
                'simulated': self.mode == 'simulate', 'real_order_created': self.mode == 'submit',
                'network': self.gate.events, 'confirmed_at': now()}
            if verification is not None: output['verification']=verification
            if entry is not None:
                journal.update(entry, 'confirmed', order=output)
            return output
        except Exception:
            if entry is not None:
                journal.update(entry, 'unknown')
            raise BookingError('未能完成订单核实。请检查个人订单；程序不会重复下单。') from None
        finally:
            self.gate.armed = False
            self.gate.retry_armed = False
            self.gate.verification_active = False

    def close(self):
        self.login.close()
