"""Durable success notices and desktop retries use isolated files and fake SMTP."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import booking_flow as booking
import mail_notice as mail
import notification_service as service
import poller
from test_polling import Clock, mailbox, order, target_slot


class NoticeRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / '.local/mail/outbox.json'
        self.clock = Clock()
        self.sender = MagicMock(side_effect=lambda config, message, before: before() or True)
        original = mail.Outbox
        self.factory = lambda clock=None, **kwargs: original(self.path, clock or self.clock, self.sender, **kwargs)
        self.root_patch = patch.object(mail, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.box_patch = patch.object(mail, 'Outbox', side_effect=self.factory)
        self.box_patch.start()
        self.addCleanup(self.box_patch.stop)

    def journal(self, state='0', day='2026-10-18'):
        journal = booking.Journal(self.root / '.local/booking/journal.json')
        slot = target_slot(day=day)
        entry = journal.begin('account', slot, [])
        result = {**order(slot), 'state': state, 'status': '已支付' if state == '1' else '待付款'}
        journal.update(entry, 'confirmed', order=result)
        return journal

    def test_missing_settings_still_queue_success_and_later_send(self):
        self.assertEqual(mail.notify_payment('account', order(), None, self.clock), 'not_configured')
        self.sender.assert_not_called()
        self.assertEqual(self.factory().records[0]['phase'], 'queued')
        mail.service_outbox(mailbox(), clock=self.clock)
        self.assertEqual(self.factory().records[0]['phase'], 'sent')
        self.assertEqual(self.sender.call_count, 1)

    def test_paid_order_sends_success_without_a_request_to_pay_again(self):
        self.journal(state='1')
        mail.recover_confirmed_notices(mailbox(), self.clock)
        mail.service_outbox(mailbox(), clock=self.clock)
        message = self.sender.call_args.args[1]
        self.assertIn('预约成功', message['Subject'])
        self.assertNotIn('请付款', message['Subject'])
        self.assertIn('无需重复付款', message.get_content())

    def test_recover_older_success_without_login_or_current_date_restrictions(self):
        self.journal(day='2026-09-01')
        mail.recover_confirmed_notices(mailbox(), self.clock)
        mail.recover_confirmed_notices(mailbox(), self.clock)
        self.assertEqual(len(self.factory().records), 1)
        self.assertEqual(mail.service_outbox(mailbox(), clock=self.clock)['sent'], 1)
        self.assertIn('可能已支付、过期或取消', self.sender.call_args.args[1].get_content())

    def test_simulated_success_is_never_recovered(self):
        journal = self.journal()
        entry = journal.entries[0]
        journal.update(entry, 'confirmed', order={**entry['order'], 'simulated': True})
        mail.recover_confirmed_notices(mailbox(), self.clock)
        self.assertEqual(self.factory().records, [])
        self.sender.assert_not_called()

    def test_manual_retry_allows_fixed_mail_configuration_after_failure(self):
        self.sender.side_effect = mail.MailFailure('failed')
        mail.notify_payment('account', order(), mailbox(), self.clock)
        self.sender.side_effect = lambda config, message, before: before() or True
        self.clock.advance(60)
        self.assertEqual(mail.service_outbox(mailbox(), clock=self.clock)['failed'], 1)
        self.assertEqual(mail.service_outbox(mailbox(), clock=self.clock, retry_failed=True)['sent'], 1)

    def test_read_only_status_does_not_recover_another_live_sender(self):
        box = self.factory()
        item = box.enqueue('account', order(), mailbox())
        item.update(phase='sending', attempts=1)
        box.save()
        self.assertEqual(self.factory(recover=False).records[0]['phase'], 'sending')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['records'][0]['phase'], 'sending')
        self.assertEqual(self.factory().records[0]['phase'], 'unknown')

    def test_background_schedules_new_success_and_rate_limited_retry(self):
        self.journal()
        helper = service.NoticeService(self.root, self.root / 'config.toml', enabled=False)
        self.assertTrue(helper.has_work(self.clock()))
        self.sender.side_effect = mail.MailFailure('retryable')
        mail.notify_payment('account', order(), mailbox(), self.clock)
        self.assertFalse(helper.has_work(self.clock()))
        self.clock.advance(60)
        self.assertTrue(helper.has_work(self.clock()))
        self.sender.side_effect = lambda config, message, before: before() or True
        mail.service_outbox(mailbox(), clock=self.clock)
        self.assertFalse(helper.has_work(self.clock()))
        self.assertIn('已被邮件服务器接受', helper.status_text())

    def test_background_uses_only_mail_worker_and_never_order_worker(self):
        self.journal()
        helper = service.NoticeService(self.root, self.root / 'config.toml', enabled=False)
        completed = MagicMock(returncode=0, stdout='{"sent":1,"failed":0}', stderr='EXAMPLE-PRIVATE-ERROR')
        with patch.object(service, 'worker_command', return_value=['fake-worker', 'service-mail']) as command, \
                patch.object(service.subprocess, 'run', return_value=completed) as launched:
            helper.tick()
        command.assert_called_once_with('poller', '--settings', helper.config_path, 'service-mail')
        self.assertEqual(launched.call_args.args[0], ['fake-worker', 'service-mail'])
        self.assertNotIn('EXAMPLE', helper.status_text())
        self.assertEqual(helper.report, {'sent': 1, 'failed': 0})

    def test_disabled_desktop_service_cannot_start_a_worker(self):
        helper = service.NoticeService(self.root, enabled=False)
        helper.start()
        self.assertIsNone(helper.thread)
        self.assertTrue(helper.close())

    def test_background_recovers_interrupted_sender(self):
        box = self.factory()
        item = box.enqueue('account', order(), mailbox())
        item.update(phase='sending', attempts=1)
        box.save()
        helper = service.NoticeService(self.root, self.root / 'config.toml', enabled=False)
        self.assertTrue(helper.has_work(self.clock()))

    def test_smtp_lock_contention_does_not_lose_confirmed_notification(self):
        with patch.object(mail, 'ProcessLock', side_effect=booking.BookingError('另一个预约程序正在运行，当前程序已退出。')):
            self.assertEqual(mail.notify_payment('account', order(), mailbox(), self.clock), 'queued')
        self.sender.assert_not_called()

    def test_unknown_delivery_can_retry_without_exceeding_max_attempts(self):
        def ambiguous(config, message, before):
            before()
            raise OSError('after DATA')
        self.sender.side_effect = ambiguous
        mail.notify_payment('account', order(), mailbox(), self.clock)
        helper = service.NoticeService(self.root, self.root / 'config.toml', enabled=False)
        self.assertFalse(helper.has_work(self.clock()))
        for _ in range(2):
            self.clock.advance(60)
            self.assertTrue(helper.has_work(self.clock()))
            mail.service_outbox(mailbox(), clock=self.clock)
        self.clock.advance(60)
        self.assertFalse(helper.has_work(self.clock()))
        self.assertEqual(self.sender.call_count, 3)


if __name__ == '__main__':
    unittest.main()
