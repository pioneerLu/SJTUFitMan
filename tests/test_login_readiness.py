"""Delayed jAccount navigation must finish before credentials are written."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from configuration import read_config
import login_flow


class LoginReadinessTests(unittest.TestCase):
    def flow(self, url):
        config = read_config(Path(__file__).resolve().parents[1] / 'config/config.example.toml')
        flow = login_flow.LoginBrowser(config, lambda *args: None)
        flow.page = MagicMock()
        flow.page.is_closed.return_value = False
        flow.page.url = url
        return flow

    def test_sports_to_jaccount_redirect_waits_before_writing_credentials(self):
        flow = self.flow(login_flow.DEFAULT_URL)
        actions = []
        def ready(*args, **kwargs):
            actions.append('login form ready')
            flow.page.url = 'https://jaccount.sjtu.edu.cn/jaccount/login'
        flow.page.wait_for_function.side_effect = ready
        flow.page.locator.return_value.fill.side_effect = lambda value: actions.append('filled')
        flow.fill_credentials('EXAMPLE-STUDENT', 'EXAMPLE-PASSWORD')
        self.assertEqual(actions, ['login form ready', 'filled', 'filled'])
        flow.page.wait_for_function.assert_called_once_with(login_flow.LOGIN_FORM_READY,
            arg=login_flow.LOGIN_HOST, timeout=30000)

    def test_existing_login_page_still_waits_for_both_editable_fields(self):
        flow = self.flow('https://jaccount.sjtu.edu.cn/jaccount/login')
        flow.ensure_login_form()
        flow.page.wait_for_function.assert_called_once_with(login_flow.LOGIN_FORM_READY,
            arg=login_flow.LOGIN_HOST, timeout=30000)
        self.assertIn('input-login-pass', login_flow.LOGIN_FORM_READY)
        self.assertIn('field.disabled', login_flow.LOGIN_FORM_READY)
        self.assertIn('field.readOnly', login_flow.LOGIN_FORM_READY)

    def test_timeout_never_fills_or_submits_and_does_not_expose_private_diagnostics(self):
        flow = self.flow(login_flow.DEFAULT_URL)
        flow.page.wait_for_function.side_effect = TimeoutError('EXAMPLE-PRIVATE-PASSWORD')
        with self.assertRaises(login_flow.FlowError) as error:
            flow.fill_credentials('EXAMPLE-STUDENT', 'EXAMPLE-PASSWORD')
        self.assertIn('输入框超时', str(error.exception))
        self.assertNotIn('EXAMPLE', str(error.exception))
        flow.page.locator.assert_not_called()

    def test_offsite_and_non_https_pages_never_receive_credentials(self):
        for url in ('https://other.example/login', 'http://jaccount.sjtu.edu.cn/login',
                    'https://jaccount.sjtu.edu.cn.other.example/login'):
            flow = self.flow(url)
            with self.subTest(url=url), self.assertRaises(login_flow.FlowError):
                flow.fill_credentials('EXAMPLE-STUDENT', 'EXAMPLE-PASSWORD')
            flow.page.wait_for_function.assert_not_called()
            flow.page.locator.assert_not_called()

    def test_closed_page_never_starts_a_wait_or_writes_credentials(self):
        flow = self.flow(login_flow.DEFAULT_URL)
        flow.page.is_closed.return_value = True
        with self.assertRaises(login_flow.FlowError):
            flow.fill_credentials('EXAMPLE-STUDENT', 'EXAMPLE-PASSWORD')
        flow.page.wait_for_function.assert_not_called()
        flow.page.locator.assert_not_called()

    def test_entry_does_not_report_ready_until_form_has_loaded(self):
        flow = self.flow(login_flow.DEFAULT_URL)
        actions = []
        flow.emit = lambda kind, value: actions.append(value)
        context = MagicMock(); context.new_page.return_value = flow.page
        with patch.object(flow, 'launch'), patch.object(flow, 'new_context', return_value=context), \
                patch.object(flow, 'ensure_login_form', side_effect=lambda: actions.append('READY')):
            flow.start(MagicMock())
        self.assertTrue(actions[0].startswith('正在等待'))
        self.assertEqual(actions[1], 'READY')
        self.assertTrue(actions[2].startswith('登录页已打开'))


if __name__ == '__main__': unittest.main()
