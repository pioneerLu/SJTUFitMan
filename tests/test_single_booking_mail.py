"""Single reservations use fake browsers and SMTP, including missed-notice recovery."""
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import booking as cli
import booking_flow as flow
import mail_notice as mail
from poll_settings import BEIJING, SettingsError
from test_polling import Clock, mailbox, order, target_slot


class SingleBookingMailTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.result_path = self.root / 'last-order.json'
        self.config_path = self.root / 'config.toml'
        self.clock = Clock()

    def test_success_is_saved_before_smtp_and_mail_phase_persisted(self):
        def notify(account, result, settings):
            saved = json.loads(self.result_path.read_text(encoding='utf-8'))
            self.assertEqual(saved['phase'], 'confirmed')
            self.assertNotIn('mail_phase', saved)
            self.assertEqual(account, 'account')
            return 'sent'
        with patch.object(cli, 'load_mail_settings', return_value=mailbox()), \
                patch.object(cli, 'notify_payment', side_effect=notify) as sender:
            result = cli.save_result(order(), self.result_path, 'account', self.config_path)
        self.assertEqual(result['mail_phase'], 'sent')
        self.assertEqual(json.loads(self.result_path.read_text(encoding='utf-8')), json.loads(json.dumps(result)))
        sender.assert_called_once()

    def test_mail_failure_never_changes_booking_success_or_exposes_secrets(self):
        for outcome in ('failed', 'unknown', 'retryable', OSError('EXAMPLE-PRIVATE-ERROR')):
            with self.subTest(outcome=type(outcome).__name__):
                sender = MagicMock(side_effect=outcome) if isinstance(outcome, Exception) else MagicMock(return_value=outcome)
                with patch.object(cli, 'load_mail_settings', return_value=mailbox()), \
                        patch.object(cli, 'notify_payment', sender):
                    result = cli.save_result(order(), self.result_path, 'account', self.config_path)
                self.assertEqual(result['phase'], 'confirmed')
                self.assertEqual(result['mail_phase'], 'failed' if isinstance(outcome, Exception) else outcome)
                self.assertNotIn('EXAMPLE-PRIVATE-ERROR', self.result_path.read_text(encoding='utf-8'))

    def test_missing_mail_settings_do_not_block_booking(self):
        with patch.object(cli, 'load_mail_settings', side_effect=SettingsError('no mail')), \
                patch.object(cli, 'notify_payment', return_value='not_configured') as sender:
            result = cli.save_result(order(), self.result_path, 'account', self.config_path)
        self.assertEqual((result['phase'], result['mail_phase']), ('confirmed', 'not_configured'))
        sender.assert_called_once_with('account', result, None)

    def test_simulation_and_unconfirmed_results_never_load_mail_or_send(self):
        for changes in ({'simulated': True}, {'real_order_created': False}, {'phase': 'unknown'},
                        {'phase': 'rejected'}, {'phase': 'verification_required'}):
            with self.subTest(changes=changes), patch.object(cli, 'load_mail_settings') as settings, \
                    patch.object(cli, 'notify_payment') as sender:
                cli.save_result({**order(), **changes}, self.result_path, 'account', self.config_path)
                settings.assert_not_called()
                sender.assert_not_called()

    def test_paid_order_also_sends_a_success_notice(self):
        with patch.object(cli, 'load_mail_settings', return_value=mailbox()) as settings, \
                patch.object(cli, 'notify_payment', return_value='sent') as sender:
            result = cli.save_result({**order(), 'state': '1'}, self.result_path, 'account', self.config_path)
        self.assertEqual(result['mail_phase'], 'sent')
        settings.assert_called_once_with(self.config_path)
        sender.assert_called_once()

    def test_shared_notification_is_deduplicated_after_restart(self):
        sender = MagicMock(side_effect=lambda settings, message, before: before() or True)
        original = mail.Outbox
        def restored(clock=None):
            return original(self.root / 'outbox.json', clock, sender)
        with patch.object(mail, 'ROOT', self.root), patch.object(mail, 'Outbox', side_effect=restored):
            self.assertEqual(mail.notify_payment('account', order(), mailbox(), self.clock), 'sent')
            self.assertEqual(mail.notify_payment('account', order(), mailbox(), self.clock), 'sent')
        self.assertEqual(sender.call_count, 1)

    def test_submit_command_actually_calls_notification_only_after_commit(self):
        slot = target_slot()
        prepared = {'schema_version': 1, 'account': 'account', 'slot': asdict(slot),
                    'terms_sha256': 'terms', 'prepared_at': self.clock().isoformat()}
        prepared['confirmation_sha256'] = flow.confirmation_hash(prepared)
        (self.root / 'prepared.json').write_text(json.dumps(prepared), encoding='utf-8')
        browser = MagicMock(account='account')
        browser.stage.return_value = prepared
        browser.commit.return_value = {**order(), 'status': '待付款'}
        with patch.object(cli, 'PRIVATE', self.root), patch.object(cli, 'BookingBrowser', return_value=browser), \
                patch.object(cli, 'load_config', return_value={}), patch.object(cli, 'sync_playwright'), \
                patch.object(cli, 'beijing_now', return_value=datetime(2026, 10, 11, tzinfo=BEIJING)), \
                patch.object(cli, 'load_mail_settings', return_value=mailbox()), \
                patch.object(cli, 'notify_payment', return_value='sent') as sender, redirect_stdout(io.StringIO()):
            result = cli.main(['submit', '--confirm-reservation-sha256', prepared['confirmation_sha256']])
        self.assertEqual(result, 0)
        browser.commit.assert_called_once()
        sender.assert_called_once()
        self.assertEqual(json.loads(self.result_path.read_text(encoding='utf-8'))['mail_phase'], 'sent')

    def reconcile_fixture(self, state='0', journal_path=None):
        browser = MagicMock(account='account')
        slot = target_slot()
        journal = flow.Journal(journal_path or self.root / 'journal.json')
        entry = journal.begin('account', slot, [])
        journal.update(entry, 'confirmed', order=order())
        browser.query_order.return_value = {'code': 0, 'data': {
            'venueName': slot.venue_name, 'countPrice': slot.price, 'price': slot.price, 'orderStateId': state,
            'spaces': [{'scheduleTime': slot.time, 'scheduleDate': slot.date, 'subSitename': slot.field,
                        'subSiteId': slot.field_id, 'venueName': slot.activity, 'venuePrice': slot.price}]}}
        return browser, journal

    def test_reconcile_old_confirmed_order_verifies_current_server_state(self):
        for state in ('0', '1'):
            with self.subTest(state=state):
                browser, journal = self.reconcile_fixture(state, self.root / f'journal-{state}.json')
                result = cli.reconcile(browser, journal)['reconciliation'][0]
                self.assertEqual((result['phase'], result['state']), ('confirmed', state))
                self.assertIn('confirmed_at', result)
                browser.query_order.assert_called_once_with(order()['order_id'])
                browser.commit.assert_not_called()

    def test_reconcile_expired_order_does_not_create_payment_notice(self):
        browser, journal = self.reconcile_fixture('8')
        result = cli.reconcile(browser, journal)['reconciliation'][0]
        self.assertEqual(result['phase'], 'released')
        self.assertIn('支付超时取消', result['message'])
        self.assertTrue(journal.entries[0]['current_order']['released'])
        browser.commit.assert_not_called()

    def test_reconcile_command_sends_for_verified_old_order_without_resubmitting(self):
        browser, journal = self.reconcile_fixture()
        with patch.object(cli, 'PRIVATE', self.root), patch.object(cli, 'BookingBrowser', return_value=browser), \
                patch.object(cli, 'load_config', return_value={}), patch.object(cli, 'sync_playwright'), \
                patch.object(cli, 'load_mail_settings', return_value=mailbox()), \
                patch.object(cli, 'notify_payment', return_value='sent') as sender, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['reconcile']), 0)
        sender.assert_called_once()
        browser.commit.assert_not_called()
        self.assertEqual(json.loads(self.result_path.read_text(encoding='utf-8'))['mail_phase'], 'sent')


if __name__ == '__main__':
    unittest.main()
