"""Browser operations for the local login helper. Secrets never go to logs."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from session import ROOT, ORIGIN, HOST, authenticated_response, load_config, session_path, storage_values
from runtime import asset_path
from captcha_image import NATIVE_IMAGE_JS, decode_native_image
from browser_runtime import launch_browser

DEFAULT_URL = ORIGIN + '/pc/#/apointmentDetails/1/d784ad7c-cb24-4282-afd6-a67aec68c675/%25E5%2585%25A8%25E9%2583%25A8/0'
LOGIN_HOST = 'jaccount.sjtu.edu.cn'
LOGIN_FORM_READY = """(host) => {
    if (location.protocol !== 'https:' || location.hostname !== host) return false;
    return ['input-login-user', 'input-login-pass'].every(id => {
        const field = document.getElementById(id);
        if (!field || field.disabled || field.readOnly) return false;
        const box = field.getBoundingClientRect();
        return box.width > 0 && box.height > 0 && getComputedStyle(field).visibility !== 'hidden';
    });
}"""


class FlowError(Exception):
    pass


def classify_login(status, body):
    if status!=200 or not isinstance(body,dict) or type(body.get('errno')) is not int:
        return 'unknown'
    if body['errno']==0:
        return 'accepted'
    error=str(body.get('error','')).lower()
    captcha=any(word in error for word in ('captcha','验证码'))
    failure=any(word in error for word in ('wrong','incorrect','invalid','error','expired','错误','不正确','失效'))
    credentials=any(word in error for word in ('password','account','密码','账号','账户'))
    return 'captcha_rejected' if captcha and failure and not credentials else 'stopped'


def appointment_url(config):
    url = config.get('site', {}).get('appointment_url', DEFAULT_URL)
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.netloc != HOST or parts.path != '/pc/'
            or not parts.fragment.startswith('/apointmentDetails/') or parts.query):
        raise FlowError('预约地址必须是交大体育平台的预约详情页。')
    return url


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def scoped_state(state):
    # Do not persist the jAccount SSO session. Save the sports site only.
    return {
        'cookies': [c for c in state.get('cookies', []) if c.get('domain', '').lstrip('.') == HOST],
        'origins': [o for o in state.get('origins', []) if o.get('origin') == ORIGIN],
    }


def restore_script(values):
    checked = storage_values(values)
    serialized = json.dumps(checked, ensure_ascii=True).replace('<', '\\u003c')
    return '(() => { if (location.origin === ' + json.dumps(ORIGIN) + ') { const saved = ' + serialized + "; for (const [key,value] of Object.entries(saved)) sessionStorage.setItem(key,value); } })();"


def user_fingerprint(user):
    return hashlib.sha256(user['data']['loginName'].encode('utf-8')).hexdigest()


class LoginBrowser:
    def __init__(self, config, emit):
        self.config = config
        self.emit = emit
        self.browser = self.context = self.page = None
        self.browser_label = '浏览器'
        self.browser_kind = None
        self.saved = False
        self.last_confirmation = None
        self.last_probe = 0
        self.attempts = 0
        self.last_submission = 0
        self.captcha_source = None
        self.captcha_digest = None
        self.paths = {key: session_path(config, key) for key in (
            'cookies_file', 'storage_state_file', 'session_storage_file')}
        if len(set(self.paths.values())) != len(self.paths):
            raise FlowError('会话保存的三个文件路径不能相同。')
        self.url = appointment_url(config)

    def launch(self, playwright, headless=False):
        self.browser, choice = launch_browser(playwright, self.config, headless=headless)
        self.browser_label = choice.label
        self.browser_kind = choice.kind
        self.emit('status', '已启动' + self.browser_label + '独立窗口。')

    def start(self, playwright, restore=False, headless=False):
        self.launch(playwright, headless=headless)
        self.context = self.new_context(restore)
        self.page = self.context.new_page()
        self.page.goto(self.url, wait_until='domcontentloaded', timeout=45000)
        if restore:
            self.verify_restore()
            return
        school = self.page.get_by_role('button', name='校内人员登录', exact=True)
        try:
            school.wait_for(state='visible', timeout=12000)
            school.click()
        except Exception:
            if urlsplit(self.page.url).hostname != LOGIN_HOST and not self.client_logged_in():
                raise FlowError('未找到校内登录入口，请检查网站连接后重新打开。') from None
        self.emit('status', '正在等待jAccount账号密码登录页加载……')
        self.ensure_login_form()
        self.emit('status', '登录页已打开。可直接在浏览器里输入账号、密码和图片验证码。')

    def new_context(self, restore=False):
        options = dict(viewport={'width': 1360, 'height': 940}, locale='zh-CN', timezone_id='Asia/Shanghai')
        sessions = None
        if restore:
            if not all(p.exists() for p in self.paths.values()):
                raise FlowError('还没有完整的已保存会话，请先完成一次登录。')
            state = json.loads(self.paths['storage_state_file'].read_text(encoding='utf-8-sig'))
            if scoped_state(state) != state:
                raise FlowError('会话包含其他站点的数据，请重新保存本网站会话。')
            options['storage_state'] = state
            stored = json.loads(self.paths['session_storage_file'].read_text(encoding='utf-8-sig'))
            sessions = storage_values(stored.get(ORIGIN, {}))
        context = self.browser.new_context(**options)
        context.set_default_timeout(10000)
        if sessions is not None:
            context.add_init_script(restore_script(sessions))
        return context

    def ensure_login_form(self):
        if not self.page or self.page.is_closed():
            raise FlowError('请先打开 jAccount 的账号密码登录页。')
        parts = urlsplit(self.page.url)
        if parts.scheme != 'https' or parts.hostname not in (HOST, LOGIN_HOST):
            raise FlowError('请先打开 jAccount 的账号密码登录页。')
        try:
            # The school's entry may redirect asynchronously; Edge's cold page
            # load can also exceed the previous five-second field timeout.
            self.page.wait_for_function(LOGIN_FORM_READY, arg=LOGIN_HOST, timeout=30000)
        except Exception:
            raise FlowError('等待jAccount登录输入框超时或页面已关闭。请检查网络后重新打开登录页，或在浏览器手动登录。') from None

    def fill_credentials(self, username, password):
        self.ensure_login_form()
        if not username.strip() or not password:
            raise FlowError('请在本地填写账号和密码，或直接在浏览器填写。')
        self.page.locator('#input-login-user').fill(username.strip())
        self.page.locator('#input-login-pass').fill(password)
        self.emit('status', '账号密码已填入 jAccount；没有写入日志。')

    def invalidate_captcha(self):
        self.captcha_source = self.captcha_digest = None
        self.emit('code', '')
        self.emit('image', None)

    def read_captcha(self):
        self.ensure_login_form()
        image = self.page.locator('#captcha-img')
        image.wait_for(state='visible', timeout=5000)
        try:
            self.page.wait_for_function("() => {const e=document.querySelector('#captcha-img');return e && e.complete && e.naturalWidth > 0;}", timeout=5000)
            return decode_native_image(image.evaluate(NATIVE_IMAGE_JS))
        except Exception:
            raise FlowError('验证码原图无法读取，请刷新图片或在浏览器手动登录。') from None

    def capture_captcha(self):
        self.invalidate_captcha()
        source, image_bytes = self.read_captcha()
        self.captcha_source = source
        self.captcha_digest = hashlib.sha256(image_bytes).hexdigest()
        self.emit('image', image_bytes)
        return image_bytes

    def refresh_captcha(self):
        self.ensure_login_form()
        self.invalidate_captcha()
        image = self.page.locator('#captcha-img')
        image.evaluate("""image => {
            const state = image.__sjtuFitRefresh = {loaded: false};
            image.addEventListener('load', () => state.loaded = true, {once: true});
        }""")
        image.click()
        self.page.locator('#input-login-captcha').fill('')
        try:
            self.page.wait_for_function("""() => {
                const image = document.querySelector('#captcha-img');
                return image && image.__sjtuFitRefresh?.loaded && image.complete && image.naturalWidth > 0;
            }""", timeout=10000)
            self.capture_captcha()
        except Exception:
            raise FlowError('新验证码尚未加载，请重试刷新或在浏览器手动登录。') from None
        self.emit('status', '已显示新的完整验证码，可手动输入或点击本地识别。')

    def submit_captcha(self, code):
        from captcha import normalize_code
        self.ensure_login_form()
        if self.attempts >= 3:
            raise FlowError('本次助手已提交 3 次，请稍后手动登录。')
        if time.monotonic() - self.last_submission < 15:
            raise FlowError('请等待至少 15 秒再尝试下一次登录。')
        if self.captcha_source and self.page.locator('#captcha-img').get_attribute('src') != self.captcha_source:
            self.invalidate_captcha()
            raise FlowError('浏览器中的验证码已变化，请点击“显示当前图片”后重新输入。')
        if not self.captcha_source or not self.captcha_digest:
            raise FlowError('请先点击“显示当前图片”，查看完整验证码后再输入。')
        if self.captcha_digest:
            source, image_bytes = self.read_captcha()
            if source != self.captcha_source or hashlib.sha256(image_bytes).hexdigest() != self.captcha_digest:
                self.invalidate_captcha()
                raise FlowError('浏览器中的验证码已变化，请点击“显示当前图片”后重新输入。')
        if not self.page.locator('#input-login-user').input_value().strip() or not self.page.locator('#input-login-pass').input_value():
            raise FlowError('浏览器里还没有填写完整账号和密码。')
        self.page.locator('#input-login-captcha').fill(normalize_code(code))
        self.attempts += 1
        self.last_submission = time.monotonic()
        self.page.locator('#submit-password-button').click()
        self.emit('status', '已提交本次登录，等待网站结果。失败时请核对浏览器提示；不会自动重试。')

    def automatic_login(self, max_attempts=2):
        """Retry only an explicit CAPTCHA rejection. Never retry credentials or unknown results."""
        from captcha import CaptchaError, recognize_ocr
        if not 1 <= max_attempts <= 2:
            raise FlowError('自动登录最多尝试2次。')
        report = {'schema_version':1,'kind':'login','engine':'ddddocr-1.6.1-cpu','browser':self.browser_kind,
            'started_at':datetime.now(timezone.utc).isoformat(),'authenticated':False,'attempts':[],
            'manual_required':True}
        for index in range(max_attempts):
            if self.attempts >= 3:
                break
            delay = max(0,20-(time.monotonic()-self.last_submission))
            if delay:
                self.emit('status','等待登录尝试间隔，请稍候。')
                self.page.wait_for_timeout(round(delay*1000))
            started=time.monotonic()
            try:
                image=self.capture_captcha()
                code=recognize_ocr(image)
                self.emit('code',code)
                self.emit('status','本地OCR已识别，正在提交本次登录。')
                with self.page.expect_response(lambda r:urlsplit(r.url).hostname==LOGIN_HOST and urlsplit(r.url).path.endswith('/ulogin'),timeout=20000) as pending:
                    self.submit_captcha(code)
                response=pending.value
                try:
                    body=response.json()
                except Exception:
                    # Navigation may discard an XHR body after a successful redirect.
                    self.page.wait_for_function('(origin)=>location.origin===origin&&Boolean(sessionStorage.getItem("token"))',arg=ORIGIN,timeout=15000)
                    self.current_user()
                    body={'errno':0}
                outcome=classify_login(response.status,body)
                success=outcome=='accepted'
                rejected_captcha=outcome=='captcha_rejected'
                attempt={'index':index+1,'captcha_passed':True if success else False if rejected_captcha else None,
                    'result':'accepted' if success else 'captcha_rejected' if rejected_captcha else 'stopped',
                    'duration_ms':round((time.monotonic()-started)*1000)}
                report['attempts'].append(attempt)
                if success:
                    self.page.wait_for_function('(origin)=>location.origin===origin&&Boolean(sessionStorage.getItem("token"))',arg=ORIGIN,timeout=30000)
                    self.current_user()
                    report['authenticated']=True
                    report['manual_required']=False
                    break
                if not rejected_captcha:
                    break
            except CaptchaError:
                report['attempts'].append({'index':index+1,'captcha_passed':None,'result':'ocr_invalid'})
                break
            except Exception as problem:
                import traceback
                diagnostics={'index':index+1,'captcha_passed':None,'result':'unknown',
                    'error_type':type(problem).__name__, 'locations':[{'function':x.name,'line':x.lineno} for x in traceback.extract_tb(problem.__traceback__) if Path(x.filename).parent==Path(__file__).parent]}
                if report['attempts'] and report['attempts'][-1]['index']==index+1:
                    report['attempts'][-1]['session_result']='unknown'
                else: report['attempts'].append(diagnostics)
                break
        report['completed_attempts']=len(report['attempts'])
        report['captcha_passed']=sum(x['captcha_passed'] is True for x in report['attempts'])
        report['server_known_attempts']=sum(x['captcha_passed'] is not None for x in report['attempts'])
        report['captcha_pass_rate']=report['captcha_passed']/report['server_known_attempts'] if report['server_known_attempts'] else None
        atomic_json(ROOT/'.local/captcha/last-login.json',report)
        if report['manual_required']:
            self.invalidate_captcha()
            try: self.capture_captcha()
            except Exception: pass
            self.emit('manual-captcha', None)
        self.emit('status','自动登录成功，正在保存会话。' if report['authenticated'] else '自动登录已停止，请在当前浏览器手动登录。')
        return report

    def client_logged_in(self):
        return bool(self.page and not self.page.is_closed() and self.page.evaluate(
            '(origin) => location.origin === origin && Boolean(sessionStorage.getItem("token"))', ORIGIN))

    def current_user(self, context=None):
        context = context or self.context
        response = context.request.get(ORIGIN + '/system/user/currentUser', timeout=15000, max_redirects=0)
        if not response.ok or 'json' not in response.headers.get('content-type', '').lower():
            raise FlowError('服务器未确认会话有效，请重新登录。')
        result = response.json()
        if not authenticated_response(result):
            raise FlowError('服务器未确认会话有效，请重新登录。')
        return result

    def tick(self):
        if not self.page or self.page.is_closed():
            return
        # Local DOM observation only: do not send current-user requests while waiting for login.
        if self.saved or not self.client_logged_in() or time.monotonic() - self.last_probe < 15:
            return
        self.last_probe = time.monotonic()
        self.save_and_verify()

    def save_and_verify(self):
        result = self.current_user()
        state = scoped_state(self.context.storage_state())
        values = self.page.evaluate('() => Object.fromEntries(Object.entries(sessionStorage))')
        if not values.get('token'):
            raise FlowError('登录已完成，但页面登录状态尚未初始化。')
        if not any(c.get('name') == 'JSESSIONID' for c in state['cookies']):
            raise FlowError('没有获取到本网站完整会话 Cookie，请重新登录。')
        atomic_json(self.paths['cookies_file'], state['cookies'])
        atomic_json(self.paths['storage_state_file'], state)
        atomic_json(self.paths['session_storage_file'], {ORIGIN: values})
        self.last_confirmation = user_fingerprint(result)
        self.saved = True
        atomic_json(ROOT / '.local/auth/session-meta.json', {
            'schema_version': 1, 'confirmed_at': datetime.now(timezone.utc).isoformat(),
            'restored_and_verified': False, 'user_fingerprint': self.last_confirmation,
        })
        self.emit('status', '服务器已确认登录，会话已保存。正在用全新浏览器会话验证恢复。')
        old = self.context
        self.context = self.new_context(restore=True)
        self.page = self.context.new_page()
        try:
            self.page.goto(self.url, wait_until='domcontentloaded', timeout=45000)
            self.verify_restore(expected=self.last_confirmation)
        except Exception:
            self.context.close()
            self.context = old
            self.page = old.pages[-1]
            self.emit('status', '会话文件已保存，但全新会话恢复验证尚未通过。可再次点击“检查已保存会话”。')
            return
        old.close()

    def verify_restore(self, expected=None):
        result = self.current_user()
        if expected is not None and user_fingerprint(result) != expected:
            raise FlowError('恢复后的账号与刚才登录账号不一致，已停止检查。')
        self.saved = True
        self.page.wait_for_function('(origin) => location.origin === origin && Boolean(sessionStorage.getItem("token"))', arg=ORIGIN)
        self.emit('status', '保存的会话已通过服务器验证。正在只读检查健身房预约页。')
        self.inspect()
        atomic_json(ROOT / '.local/auth/session-meta.json', {
            'schema_version': 1, 'confirmed_at': datetime.now(timezone.utc).isoformat(),
            'restored_and_verified': True, 'user_fingerprint': user_fingerprint(result),
        })

    def inspect(self):
        snapshot_js = asset_path('scripts/appointment_snapshot.js').read_text(encoding='utf-8')
        self.page.wait_for_selector('#apointmentDetails', timeout=20000)
        deadline = time.monotonic() + 25
        data = None
        while time.monotonic() < deadline:
            data = self.page.evaluate(snapshot_js)
            if data and data['venue'].get('venueName') and data['types'] and not data['loading'] and data['dates']:
                break
            self.page.wait_for_timeout(500)
        if not data or not data['venue'].get('venueName') or not data['types']:
            raise FlowError('预约详情数据尚未加载，请稍后再次检查。')
        gym = next((item for item in data['types'] if item.get('name') == '健身房'), None)
        if not gym:
            raise FlowError('当前场馆没有“健身房”类型，请核对预约链接。')
        if data['active_type'].get('isactiveName') != '健身房':
            self.page.get_by_role('tab', name='健身房', exact=True).click()
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                data = self.page.evaluate(snapshot_js)
                if data and data['active_type'].get('isactiveName') == '健身房' and data['dates'] and not data['loading']:
                    break
                self.page.wait_for_timeout(500)
            if not data or data['active_type'].get('isactiveName') != '健身房' or data['loading']:
                raise FlowError('健身房时段尚未加载，请稍后再次检查。')
        # Only navigate date/type tabs; never select a slot or submit an order.
        newest = max(data['dates'], key=lambda item: item.get('isdate') or '', default=None)
        if newest and data['selected_date'].get('isdate') != newest['isdate']:
            self.page.get_by_role('tab', name=newest['viewStr'], exact=True).click()
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                data = self.page.evaluate(snapshot_js)
                if data and data['selected_date'].get('isdate') == newest['isdate'] and not data['loading']:
                    break
                self.page.wait_for_timeout(500)
            if not data or data['selected_date'].get('isdate') != newest['isdate'] or data['loading']:
                raise FlowError('目标日期时段尚未加载，请稍后再次检查。')
        data['captured_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(ROOT / '.local/inspection/appointment.json', data)
        self.page.locator('#apointmentDetails').screenshot(path=str(ROOT / '.local/inspection/appointment.png'))
        self.emit('inspection', data)
        self.emit('status', '登录与会话恢复成功，已保存实际预约页资料。浏览器保持打开，可查看日期和时段。')

    def close(self):
        browser = self.browser
        self.browser = self.context = self.page = None
        if browser:
            browser.close()
