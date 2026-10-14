"""Expired unpaid orders release reservations only after exact server confirmation."""
import copy
from dataclasses import asdict, replace
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import booking_flow as booking
import booking as cli
import mail_notice as mail
import poll_runner as runner
from test_polling import Browser, Clock, mailbox, order, settings, snapshot, target_slot
from test_waitlist import plan


def payload(slot, state):
    gate = booking.NetworkGate('simulate'); gate.slot = slot
    result = gate.simulated_order()
    result['data']['orderStateId'] = state
    return result


class OrderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.clock = Clock()
        self.journal = booking.Journal(self.root / 'journal.json')
        self.store = runner.WaitlistStore(self.root / 'waitlist.json')
        self.notifier = MagicMock(return_value='sent')
        stop = patch.object(runner, 'STOP', self.root / 'stop.json')
        stop.start(); self.addCleanup(stop.stop)

    def confirmed(self, slot=None, identifier='OLD-ORDER', account=Browser.account):
        selected = slot or target_slot()
        entry = self.journal.begin(account, selected, [])
        self.journal.update(entry, 'confirmed', order_id=identifier,
            order={**order(selected), 'order_id': identifier, 'status': '待付款'})
        return entry

    def run_task(self, state='8', config=None, saved_task=True, response=None):
        config = config or plan()
        browser = Browser(self.clock, [snapshot(('18:00-19:00', '19:00-20:00'))])
        browser.query_order = MagicMock(return_value=response or payload(target_slot(), state))
        if saved_task:
            record = self.store.task(browser.account, config)
            self.store.update(record, phase='confirmed', order=self.journal.entries[0]['order'])
        original_commit = browser.commit
        def commit(*args, **kwargs):
            result = original_commit(*args, **kwargs)
            result['order_id'] = 'NEW-ORDER'
            self.journal.update(self.journal.entries[-1], 'confirmed', order=result)
            return result
        browser.commit = commit
        flow = runner.WaitlistRunner(config, mailbox(), store=self.store, clock=self.clock,
            wait=self.clock.advance, notifier=self.notifier, stop=runner.StopSignal(clock=self.clock),
            status=MagicMock())
        result = flow.run_task(browser, self.journal)
        return browser, result

    def test_unpaid_expiry_and_explicit_cancellation_allow_a_new_order_after_restart(self):
        for state in ('8', '2', '23', '99'):
            with self.subTest(state=state):
                self.journal = booking.Journal(self.root / (state + '-journal.json'))
                self.store = runner.WaitlistStore(self.root / (state + '-tasks.json'))
                old = self.confirmed()
                browser, result = self.run_task(state)
                browser.query_order.assert_called_once_with('OLD-ORDER')
                self.assertEqual((result['phase'], browser.commits), ('confirmed', 1))
                self.assertEqual(result['order']['order_id'], 'NEW-ORDER')
                self.assertTrue(booking.reservation_released(old))
                self.assertEqual(old['order']['state'], '0')
                self.assertEqual(len(self.journal.entries), 2)

    def test_pending_and_paid_orders_keep_the_duplicate_guard(self):
        for state in ('0', '9', '1'):
            with self.subTest(state=state):
                self.journal = booking.Journal(self.root / (state + '-journal.json'))
                self.store = runner.WaitlistStore(self.root / (state + '-tasks.json'))
                old = self.confirmed()
                browser, result = self.run_task(state)
                self.assertEqual((result['phase'], browser.commits, browser.read_times), ('confirmed', 0, []))
                self.assertEqual(result['order']['state'], state)
                self.assertFalse(booking.reservation_released(old))
                with self.assertRaises(booking.BookingError): self.journal.assert_can_submit(browser.account, target_slot())

    def test_appeal_refund_unknown_or_malformed_order_never_unlocks(self):
        for response in (payload(target_slot(), '3'), payload(target_slot(), '13'),
                         payload(target_slot(), '15'), payload(target_slot(), '404'),
                         {'code': 0, 'data': None}, {'code': 401}):
            with self.subTest(response=response):
                self.journal = booking.Journal(self.root / ('bad-' + str(id(response)) + '.json'))
                self.store = runner.WaitlistStore(self.root / ('task-' + str(id(response)) + '.json'))
                old = self.confirmed()
                browser, result = self.run_task(response=response)
                self.assertEqual((result['phase'], browser.commits, browser.read_times), ('attention', 0, []))
                self.assertFalse(booking.reservation_released(old))
                self.assertEqual(len(self.journal.entries), 1)

    def test_expired_response_for_a_different_date_or_hour_cannot_unlock(self):
        for changed in (replace(target_slot(), date='2026-10-19'), replace(target_slot(), time='19:00-20:00')):
            with self.subTest(slot=changed):
                self.journal = booking.Journal(self.root / (changed.date + changed.time[:2] + '.json'))
                self.store = runner.WaitlistStore(self.root / (changed.date + changed.time[:2] + '-tasks.json'))
                old = self.confirmed()
                browser, result = self.run_task(response=payload(changed, '8'))
                self.assertEqual((result['phase'], browser.commits), ('attention', 0))
                self.assertFalse(booking.reservation_released(old))

    def test_cancelled_hour_can_be_replaced_by_a_two_hour_order_without_old_task_key(self):
        old = self.confirmed()
        browser, result = self.run_task(config=plan(period='18:00-20:00'), saved_task=False)
        self.assertEqual((result['phase'], browser.commits), ('confirmed', 1))
        self.assertEqual(result['order']['slot']['time'], '18:00-20:00')
        self.assertTrue(booking.reservation_released(old))

    def test_partial_two_hour_cancellation_does_not_unlock(self):
        selected = replace(target_slot(), time='18:00-20:00', price='4.00', hourly_prices=('2.00', '2.00'))
        old = self.confirmed(selected)
        browser, result = self.run_task(config=plan(period='18:00-20:00'), response=payload(target_slot(), '8'))
        self.assertEqual((result['phase'], browser.commits), ('attention', 0))
        self.assertFalse(booking.reservation_released(old))

    def test_released_marker_is_durable_but_wrong_order_id_is_not_trusted(self):
        old = self.confirmed()
        browser = SimpleNamespace(account=Browser.account, query_order=MagicMock(return_value=payload(target_slot(), '8')))
        self.journal.refresh_order(browser, old)
        restored = booking.Journal(self.journal.path)
        restored.assert_can_submit(browser.account, target_slot())
        restored.entries[0]['current_order']['order_id'] = 'OTHER-ORDER'
        with self.assertRaises(booking.BookingError): restored.assert_can_submit(browser.account, target_slot())

    def test_other_account_cannot_release_a_confirmed_order(self):
        old = self.confirmed()
        browser = SimpleNamespace(account='other-account', query_order=MagicMock())
        with self.assertRaises(booking.BookingError): self.journal.refresh_order(browser, old)
        browser.query_order.assert_not_called()
        self.assertFalse(booking.reservation_released(old))

    def test_failure_then_user_restart_rechecks_without_resetting_cooldown(self):
        old = self.confirmed()
        browser, result = self.run_task(response={'code': 0, 'data': None})
        self.assertEqual(result['reason'], 'previous_order_check_failed')
        self.store.update(result, checks=4, next_check_at=(self.clock() + timedelta(seconds=120)).isoformat())
        browser, result = self.run_task(saved_task=False)
        self.assertEqual((result['phase'], result['checks'], browser.commits), ('confirmed', 5, 1))
        self.assertEqual(browser.read_times[0], Clock()() + timedelta(seconds=120))

    def test_schedule_waits_before_rechecking_old_order(self):
        self.confirmed()
        start = self.clock() + timedelta(minutes=2)
        browser, result = self.run_task(config=plan(start_mode='scheduled', start_at=start.isoformat()))
        self.assertGreaterEqual(browser.read_times[0], start)

    def test_expired_task_never_queries_an_old_order_or_submits(self):
        self.confirmed(); self.clock.moment = plan().expires_at()
        browser, result = self.run_task()
        browser.query_order.assert_not_called()
        self.assertEqual((result['phase'], browser.commits), ('expired', 0))

    def test_exact_reconcile_marks_expiry_without_changing_original_notice(self):
        old = self.confirmed()
        original = copy.deepcopy(old['order'])
        browser = SimpleNamespace(account=Browser.account, query_order=MagicMock(return_value=payload(target_slot(), '8')))
        result = cli.reconcile(browser, self.journal)['reconciliation'][0]
        self.assertEqual(result['phase'], 'released')
        self.assertEqual(old['order'], original)
        box = mail.Outbox(self.root / 'outbox.json', clock=self.clock, sender=MagicMock())
        box.enqueue(browser.account, old['order'], mailbox())
        newer = {**original, 'order_id': 'NEW-ORDER'}
        box.enqueue(browser.account, newer, mailbox())
        self.assertEqual(len(box.records), 2)

    def test_mail_recovery_does_not_resurrect_released_task_success(self):
        old = self.confirmed()
        browser = SimpleNamespace(account=Browser.account, query_order=MagicMock(return_value=payload(target_slot(), '8')))
        self.journal.refresh_order(browser, old)
        daily_store = runner.DailyStore(self.root / 'days.json')
        record = daily_store.day(browser.account, target_slot().date)
        runner.recover_payment_mail(settings(), mailbox(), daily_store, self.journal, self.clock, self.root / 'outbox.json')
        self.assertEqual(record['phase'], 'polling')
        self.assertEqual(len(mail.Outbox(self.root / 'outbox.json', self.clock).records), 1)

    def test_daily_mode_rechecks_expired_order_and_can_resume(self):
        old = self.confirmed()
        store = runner.DailyStore(self.root / 'days.json')
        record = store.day(Browser.account, target_slot().date)
        store.update(record, phase='confirmed', order=old['order'])
        browser = Browser(self.clock, [snapshot()]); browser.order_state = '8'
        flow = runner.DailyRunner(settings(), mailbox(), store=store, clock=self.clock,
            wait=self.clock.advance, notifier=self.notifier, stop=runner.StopSignal(clock=self.clock), status=MagicMock())
        result = flow.run_day(browser, self.journal, self.clock().date())
        self.assertEqual((result['phase'], browser.commits), ('confirmed', 1))


if __name__ == '__main__': unittest.main()
