import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import booking_flow as flow


def slot():
    return flow.Slot('venue', '学生中心', '台球', '2026-10-11', 'field2', '八球台2',
        '12:00-13:00', '8.00', 1, 5)


def route(path=flow.CONFIRM, method='POST', resource='xhr', host=flow.HOST):
    result = MagicMock()
    result.request.url = 'https://' + host + path
    result.request.method = method
    result.request.resource_type = resource
    return result


class BookingTests(unittest.TestCase):
    def test_price_is_exact_and_does_not_accept_nan_or_invalid_format(self):
        self.assertEqual(flow.money('8.00'), flow.money(8))
        self.assertEqual(flow.money('10001.00'), flow.Decimal('10001.00'))
        for value in ('NaN', 'Infinity', '-0.01', '0.001', None, True):
            with self.subTest(value=value), self.assertRaises(flow.BookingError):
                flow.money(value)

    def test_only_documented_success_with_valid_id_establishes_acceptance(self):
        self.assertEqual(flow.classify_confirm(200, {'code': 0, 'data': 'order-123'}), ('accepted', 'order-123'))
        for status, body in ((500, {'code': 0, 'data': 'order'}), (200, {'code': 0, 'data': {}}),
                             (200, {'code': False, 'data': 'order'}), (200, '<html>'),
                             (200, {'code': 0, 'data': 'bad\r\nheader'})):
            self.assertEqual(flow.classify_confirm(status, body)[0], 'unknown')
        self.assertEqual(flow.classify_confirm(200, {'code': 1002})[0], 'verification_required')
        self.assertEqual(flow.classify_confirm(401, {})[0], 'auth_required')

    def test_simulation_cannot_forward_final_request_and_blocks_second_attempt(self):
        gate = flow.NetworkGate('simulate')
        gate.armed = True
        first = route()
        gate(first)
        first.continue_.assert_not_called()
        first.fulfill.assert_called_once()
        self.assertEqual(json.loads(first.fulfill.call_args.kwargs['body'])['data'], flow.SIM_ID)
        gate.armed = True
        second = route()
        gate(second)
        second.abort.assert_called_once()
        self.assertEqual(gate.attempts, 1)

    def test_read_only_mode_does_not_submit_even_if_button_clicked(self):
        gate = flow.NetworkGate('prepare')
        gate.armed = True
        request = route()
        gate(request)
        request.abort.assert_called_once()
        request.continue_.assert_not_called()

    def test_live_gate_requires_a_persisted_intent_callback(self):
        gate = flow.NetworkGate('submit')
        gate.armed = True
        request = route()
        gate(request)
        request.abort.assert_called_once()
        request.continue_.assert_not_called()

    def test_intent_is_durable_before_forwarding_and_storage_failure_blocks_send(self):
        events = []
        gate = flow.NetworkGate('submit', before_send=lambda: events.append('persisted'))
        gate.armed = True
        request = route()
        request.continue_.side_effect = lambda: events.append('sent')
        gate(request)
        self.assertEqual(events, ['persisted', 'sent'])
        gate = flow.NetworkGate('submit', before_send=MagicMock(side_effect=OSError('disk full')))
        gate.armed = True
        request = route()
        with self.assertRaises(OSError):
            gate(request)
        request.continue_.assert_not_called()

    def test_payment_profile_cancellation_and_external_xhr_are_blocked(self):
        gate = flow.NetworkGate('submit')
        for request in (route('/venue/personal/orderImmediatelyPC'), route('/venue/personal/cancel'),
                        route('/profile/update'), route('/payment/pay/pay.action', 'GET'),
                        route('/v1/responses', 'POST', host='api.openai.com')):
            gate(request)
            request.abort.assert_called_once()
            request.continue_.assert_not_called()
        query = route('/manage/fieldDetail/queryFieldSituation')
        gate(query)
        query.continue_.assert_called_once()

    def test_response_verification_rejects_wrong_date_price_slot_and_status(self):
        gate = flow.NetworkGate('simulate')
        gate.slot = slot()
        payload = gate.simulated_order()
        self.assertEqual(flow.validate_order(slot(), payload)['status'], '待付款')
        variants = []
        for key, value in (('scheduleDate', '2026-10-12'), ('venuePrice', '9.00'),
                           ('subSitename', '八球台1'), ('venueName', '健身房')):
            changed = copy.deepcopy(payload)
            changed['data']['spaces'][0][key] = value
            variants.append(changed)
        changed = copy.deepcopy(payload)
        changed['data']['orderStateId'] = '8'
        variants.append(changed)
        for value in variants:
            with self.assertRaises(flow.BookingError):
                flow.validate_order(slot(), value)

    def test_selection_must_match_field_id_and_one_hour(self):
        checkout = {'total': '8.00', 'selected': [{'subSiteId': 'field2', 'subSitename': '八球台2',
            'scheduleTime': '12:00-13:00', 'venueNum': 1, 'venuePrice': '8'}]}
        flow.validate_selection(slot(), checkout)
        checkout['selected'].append(checkout['selected'][0])
        with self.assertRaises(flow.BookingError):
            flow.validate_selection(slot(), checkout)

    def test_restart_after_crash_or_unknown_result_forbids_another_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'journal.json'
            journal = flow.Journal(path)
            entry = journal.begin('account', slot(), [])
            for state in ('submitting', 'unknown', 'verification_required', 'accepted'):
                journal.update(entry, state)
                restored = flow.Journal(path)
                with self.assertRaises(flow.BookingError):
                    restored.assert_can_submit('account', slot())
                other = flow.Slot('venue', '学生中心', '台球', '2026-10-12', 'field2', '八球台2', '13:00-14:00', '8', 1, 6)
                with self.assertRaises(flow.BookingError):
                    restored.assert_can_submit('account', other)

    def test_confirmed_target_stays_unique_even_if_price_or_layout_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = flow.Journal(Path(temporary) / 'journal.json')
            entry = journal.begin('account', slot(), [])
            journal.update(entry, 'confirmed')
            changed = flow.Slot('venue', '学生中心', '台球', '2026-10-11', 'field2', '八球台2', '12:00-13:00', '9', 2, 5)
            with self.assertRaises(flow.BookingError):
                journal.assert_can_submit('account', changed)
            journal.assert_can_submit('different-account', slot())

    def test_os_lock_prevents_two_processes_and_releases_normally(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'run.lock'
            with flow.ProcessLock(path):
                with self.assertRaises(flow.BookingError):
                    with flow.ProcessLock(path):
                        pass
            with flow.ProcessLock(path):
                pass

    def test_terms_change_requires_fresh_confirmation(self):
        original = {'terms_title': '预订须知', 'terms_text': '规则版本一'}
        changed = {**original, 'terms_text': '规则版本二'}
        self.assertNotEqual(flow.terms_hash(original), flow.terms_hash(changed))
        with self.assertRaises(flow.BookingError):
            flow.terms_hash({'terms_title': '须知', 'terms_text': ''})

    def test_confirmation_is_bound_to_target_and_price_not_just_shared_terms(self):
        from dataclasses import asdict
        original = {'schema_version': 1, 'prepared_at': 'time1', 'account': 'account1',
            'slot': asdict(slot()), 'terms_sha256': 'shared-terms'}
        original_hash = flow.confirmation_hash(original)
        for key, value in (('date', '2026-10-12'), ('field_id', 'field1'), ('price', '9.00')):
            changed = copy.deepcopy(original)
            changed['slot'][key] = value
            self.assertNotEqual(flow.confirmation_hash(changed), original_hash)
        changed = {**original, 'account': 'account2'}
        self.assertNotEqual(flow.confirmation_hash(changed), original_hash)


if __name__ == '__main__':
    unittest.main()
