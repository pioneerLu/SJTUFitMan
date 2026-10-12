"""A rejection must reach the user and durable journal without retaining secrets."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from booking_feedback import confirm_feedback, failure_detail, identity_redactions
import booking_flow as flow
from test_booking_flow import slot


class FeedbackTests(unittest.TestCase):
    def test_preserves_actual_website_reason_and_business_code(self):
        detail = confirm_feedback(200, {'code': 1003, 'msg': '超过每日预约次数限制',
                                        'data': {'token': 'PRIVATE-PAYLOAD'}})
        self.assertEqual(detail, {'http_status': 200, 'code': 1003, 'message': '超过每日预约次数限制'})
        self.assertIn('错误码 1003', failure_detail({'server_feedback': detail}))
        self.assertEqual(failure_detail({'phase': 'rejected'}), '')

    def test_diagnostics_remove_known_identity_password_session_and_mail(self):
        private = identity_redactions({'auth': {'username': 'PRIVATE-USER', 'password': 'short-pass'},
                                      'data': {'loginName': 'OTHER-USER', 'realName': '张小明'}})
        detail = confirm_feedback(200, {'code': 7,
            'msg': '<b>不允许预约</b> PRIVATE-USER OTHER-USER 张小明 short-pass '
                   'person@example.com 12345678901 token=abc https://example.com/?secret=abc\n'
                   + 'A' * 32}, private)
        message = detail['message']
        self.assertIn('不允许预约', message)
        for value in (*private, 'person@example.com', '12345678901', 'token=abc', 'secret=abc', 'A' * 32):
            self.assertNotIn(value, message)
        self.assertNotIn('\n', message)
        self.assertLessEqual(len(message), 240)

    def test_unknown_and_success_payloads_never_export_order_ids_or_nested_data(self):
        for body in (None, '<html>', {'code': False, 'msg': {'secret': 'PRIVATE'}},
                     {'code': 0, 'data': 'PRIVATE-ORDER', 'msg': 'PRIVATE'}):
            self.assertNotIn('PRIVATE', str(confirm_feedback(200, body)))
        self.assertEqual(confirm_feedback(403, {'message': '请重新登录'})['message'], '请重新登录')

    def test_rejected_commit_persists_the_actual_failure_and_captcha_retry_failure(self):
        for verification in (False, True, 'lost_response'):
            with self.subTest(verification=verification), tempfile.TemporaryDirectory() as temporary:
                browser = object.__new__(flow.BookingBrowser)
                browser.mode = 'submit'
                browser.account = flow.user_fingerprint({'data': {'loginName': 'fixture-account'}})
                browser.login = MagicMock()
                browser.login.current_user.return_value = {'data': {'loginName': 'fixture-account'}}
                browser.page = MagicMock()
                browser.feedback_redactions = ['PRIVATE-PASSWORD']
                checkout = {'dialog_visible': True, 'checked': True, 'terms_title': '须知',
                            'terms_text': '预约须知', 'total': '8.00', 'selected': [
                    {'subSiteId': 'field2', 'subSitename': '八球台2', 'scheduleTime': '12:00-13:00',
                     'venueNum': 1, 'venuePrice': '8.00'}]}
                browser.page.evaluate.return_value = checkout
                browser.check_no_pending = MagicMock(return_value=[])
                browser.gate = flow.NetworkGate('submit')
                initial = MagicMock(status=200)
                initial.json.return_value = {'code': 1002 if verification else 1003,
                                             'msg': '需要验证' if verification else '本时段已超过预约次数'}
                browser.page.expect_response.return_value.__enter__.return_value.value = initial
                button = browser.page.locator.return_value.get_by_role.return_value
                button.click.side_effect = lambda: browser.gate.before_send()
                final = MagicMock(status=200)
                final.json.return_value = {'code': 1004, 'msg': '验证码后仍被拒绝'}
                def verified_result(*args):
                    browser.gate.attempts = 2
                    return (None if verification == 'lost_response' else final, {'server_verified': True})
                browser.verify_and_continue = MagicMock(side_effect=verified_result)
                journal = flow.Journal(Path(temporary) / 'journal.json')
                result = browser.commit(slot(), {'terms_sha256': flow.terms_hash(checkout)}, journal)
                expected_phase = 'unknown' if verification == 'lost_response' else 'rejected'
                self.assertEqual(result['phase'], expected_phase)
                if verification == 'lost_response':
                    self.assertIsNone(result['server_feedback'])
                    self.assertEqual(failure_detail(result), '')
                else:
                    expected_code = 1004 if verification else 1003
                    self.assertEqual(result['server_feedback']['code'], expected_code)
                stored = flow.Journal(journal.path).entries[0]
                self.assertEqual(stored['server_feedback'], result['server_feedback'])
                self.assertEqual(stored['phase'], expected_phase)
                self.assertFalse(browser.gate.armed)


if __name__ == '__main__':
    unittest.main()
