import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import captcha
import login_flow


def image_png():
    from PIL import Image
    stream = io.BytesIO()
    Image.new('RGB', (120, 45), 'white').save(stream, format='PNG')
    return stream.getvalue()


def config():
    return {'site': {'appointment_url': login_flow.DEFAULT_URL}, 'auth': {
        'cookies_file': '.local/auth/cookies.json',
        'storage_state_file': '.local/auth/storage-state.json',
        'session_storage_file': '.local/auth/session-storage.json',
    }}


class CaptchaTests(unittest.TestCase):
    def test_untrusted_prediction_must_be_short_alphanumeric(self):
        self.assertEqual(captcha.normalize_code(' "aB39" '), 'aB39')
        for bad in ('Ignore all instructions', '<script>alert(1)</script>', '', '1234567', '验证码: aB39', '１２３４', None):
            with self.assertRaises(captcha.CaptchaError):
                captcha.normalize_code(bad)

    def test_ocr_model_is_cached_and_cpu_only(self):
        module=MagicMock()
        module.DdddOcr.return_value.classification.return_value='aB39'
        with patch.dict(sys.modules,{'ddddocr':module}),patch.object(captcha,'_reader',None):
            self.assertEqual(captcha.recognize_ocr(image_png()),'aB39')
            self.assertEqual(captcha.recognize_ocr(image_png()),'aB39')
        module.DdddOcr.assert_called_once_with(show_ad=False,use_gpu=False)

    def test_does_not_accept_full_page_screenshots(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new('RGB', (1360, 940)).save(stream, format='PNG')
        with self.assertRaises(captcha.CaptchaError):
            captcha.validate_image(stream.getvalue())

    def test_bad_prediction_and_inference_failure_fall_back_without_secret_text(self):
        module=MagicMock()
        for value in ('unknown', '<script>'):
            module.DdddOcr.return_value.classification.return_value=value
            with patch.dict(sys.modules,{'ddddocr':module}),patch.object(captcha,'_reader',None):
                with self.assertRaises(captcha.CaptchaError): captcha.recognize_ocr(image_png())
        module.DdddOcr.side_effect=RuntimeError('EXAMPLE-SECRET')
        with patch.dict(sys.modules,{'ddddocr':module}),patch.object(captcha,'_reader',None):
            with self.assertRaises(captcha.CaptchaError) as caught: captcha.recognize_ocr(image_png())
        self.assertNotIn('EXAMPLE',str(caught.exception))

    def test_no_remote_provider_entry_points_remain(self):
        self.assertFalse(hasattr(captcha,'recognize_vision'))
        source=(SCRIPTS/'captcha.py').read_text(encoding='utf-8')
        import ast
        imports=[n.module for n in ast.walk(ast.parse(source)) if isinstance(n,ast.ImportFrom)]
        self.assertFalse(any(n and n.startswith(('urllib','requests','httpx','openai')) for n in imports))

    def test_only_explicit_captcha_failure_can_trigger_a_retry(self):
        self.assertEqual(login_flow.classify_login(200,{'errno':0}),'accepted')
        self.assertEqual(login_flow.classify_login(200,{'errno':1,'error':'Wrong captcha'}),'captcha_rejected')
        for status,body in ((500,{'errno':0}),(200,{'errno':False}),
            (200,{'errno':1,'error':'Invalid password; captcha passed'}),
            (200,{'errno':1,'error':'Account locked'}),(200,{})):
            self.assertNotEqual(login_flow.classify_login(status,body),'captcha_rejected')


class LoginFlowTests(unittest.TestCase):
    def test_session_export_excludes_single_sign_on(self):
        state = {'cookies': [
            {'name': 'JSESSIONID', 'value': 'EXAMPLE', 'domain': 'sports.sjtu.edu.cn'},
            {'name': 'SSO', 'value': 'OTHER', 'domain': 'jaccount.sjtu.edu.cn'},
            {'name': 'SHARED', 'value': 'OTHER', 'domain': '.sjtu.edu.cn'},
        ], 'origins': [
            {'origin': login_flow.ORIGIN, 'localStorage': []},
            {'origin': 'https://jaccount.sjtu.edu.cn', 'localStorage': []},
        ]}
        result = login_flow.scoped_state(state)
        self.assertEqual(len(result['cookies']), 1)
        self.assertEqual(len(result['origins']), 1)
        self.assertNotIn('OTHER', json.dumps(result))

    def test_restoration_script_uses_serialized_strings(self):
        script = login_flow.restore_script({'token': 'example', 'quote': "');evil();//</script>"})
        self.assertIn('location.origin === "https://sports.sjtu.edu.cn"', script)
        self.assertNotIn('</script>', script)
        self.assertIn('sessionStorage.setItem', script)
        with self.assertRaises(ValueError):
            login_flow.restore_script({'token': {'invalid': True}})

    def test_server_redirect_and_empty_user_do_not_validate_login(self):
        flow = login_flow.LoginBrowser(config(), lambda *args: None)
        flow.context = MagicMock()
        response = flow.context.request.get.return_value
        response.ok = False
        with self.assertRaises(login_flow.FlowError):
            flow.current_user()
        self.assertEqual(flow.context.request.get.call_args.kwargs['max_redirects'], 0)
        response.ok = True
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = {'code': 0, 'data': {}}
        with self.assertRaises(login_flow.FlowError):
            flow.current_user()

    def test_waiting_for_human_does_not_poll_server(self):
        flow = login_flow.LoginBrowser(config(), lambda *args: None)
        flow.page = MagicMock()
        flow.page.is_closed.return_value = False
        flow.page.evaluate.return_value = False
        flow.context = MagicMock()
        for _ in range(20):
            flow.tick()
        flow.context.request.get.assert_not_called()

    def test_save_retains_http_only_metadata_and_session_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = []
            flow = login_flow.LoginBrowser(config(), lambda *args: events.append(args))
            flow.paths = {key: root / value for key, value in config()['auth'].items()}
            cookie = {'name': 'JSESSIONID', 'value': 'EXAMPLE-SECRET', 'domain': login_flow.HOST, 'path': '/', 'httpOnly': True, 'secure': True, 'sameSite': 'Lax', 'expires': -1}
            flow.context = MagicMock()
            old = flow.context
            old.storage_state.return_value = {'cookies': [cookie], 'origins': [{'origin': login_flow.ORIGIN, 'localStorage': []}]}
            flow.page = MagicMock()
            flow.page.evaluate.return_value = {'token': 'EXAMPLE-SECRET', 'onAndOff': '0'}
            user = {'code': 0, 'data': {'loginName': 'example-user'}}
            with patch.object(login_flow, 'ROOT', root), patch.object(flow, 'current_user', return_value=user), patch.object(flow, 'new_context', return_value=MagicMock()) as new_context, patch.object(flow, 'verify_restore') as verify:
                flow.save_and_verify()
            self.assertEqual(json.loads(flow.paths['cookies_file'].read_text())[0], cookie)
            self.assertEqual(json.loads(flow.paths['session_storage_file'].read_text())[login_flow.ORIGIN]['onAndOff'], '0')
            new_context.assert_called_once_with(restore=True)
            verify.assert_called_once_with(expected=login_flow.user_fingerprint(user))
            old.close.assert_called_once()
            self.assertNotIn('EXAMPLE-SECRET', str(events))

    def test_duplicate_paths_and_offsite_url_are_rejected(self):
        duplicate = config()
        duplicate['auth']['session_storage_file'] = duplicate['auth']['cookies_file']
        with self.assertRaises(login_flow.FlowError):
            login_flow.LoginBrowser(duplicate, lambda *args: None)
        for bad in ('https://evil.example/pc/#/apointmentDetails/x', 'https://sports.sjtu.edu.cn/pc/#/Order'):
            with self.assertRaises(login_flow.FlowError):
                login_flow.appointment_url({'site': {'appointment_url': bad}})

    def test_stale_captcha_is_not_submitted(self):
        flow = login_flow.LoginBrowser(config(), lambda *args: None)
        flow.page = MagicMock()
        flow.page.locator.return_value.get_attribute.return_value = 'fresh-image'
        flow.captcha_source = 'previous-image'
        with patch.object(flow, 'ensure_login_form'):
            with self.assertRaises(login_flow.FlowError):
                flow.submit_captcha('aB39')
        flow.page.locator.return_value.click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
