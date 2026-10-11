"""Critical verification, retry, and no-duplicate-write boundaries."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import cv2
import numpy as np
import captcha
import booking_flow as booking
import login_flow
import slider


def png(image): return cv2.imencode('.png',image)[1].tobytes()


def fixture(ambiguous=False):
    rng=np.random.default_rng(10)
    bg=rng.integers(40,220,(155,310,3),dtype=np.uint8)
    original=bg.copy()
    alpha=np.zeros((40,40),np.uint8)
    cv2.rectangle(alpha,(3,3),(36,36),255,-1)
    cv2.circle(alpha,(20,3),8,255,-1)
    piece=np.zeros((155,47,4),np.uint8)
    piece[60:100,3:43,:3]=original[60:100,160:200]
    piece[60:100,3:43,3]=alpha
    bg[60:100,160:200]=(bg[60:100,160:200]*.6).astype(np.uint8)
    if ambiguous: bg[60:100,80:120]=bg[60:100,160:200]
    return png(bg),png(piece)


def route(path,headers=None):
    r=MagicMock()
    r.request.url='https://'+booking.HOST+path
    r.request.method='POST'
    r.request.resource_type='xhr'
    r.request.headers=headers or {}
    return r


def challenge(gate,token='EXAMPLE-TOKEN'):
    bg,piece=fixture()
    response=MagicMock()
    response.url='https://'+booking.HOST+'/captcha/get'
    response.status=200
    response.json.return_value={'repCode':'0000','repData':{'token':token,
        'originalImageBase64':base64.b64encode(bg).decode(),'jigsawImageBase64':base64.b64encode(piece).decode()}}
    gate.observe_response(response)
    return gate.captcha_image_hash


class SliderTests(unittest.TestCase):
    def test_texture_matching_corrects_transparent_offset_and_darkened_gap(self):
        bg,piece=fixture()
        match=slider.locate_gap(bg,piece)
        self.assertAlmostEqual(match.x,157,delta=1)
        self.assertEqual(match.background_width,310)

    def test_ambiguous_or_flat_images_require_a_human(self):
        with self.assertRaises(captcha.CaptchaError): slider.locate_gap(*fixture(ambiguous=True))
        bg=png(np.zeros((155,310,3),np.uint8))
        with self.assertRaises(captcha.CaptchaError): slider.locate_gap(bg,fixture()[1])

    def test_invalid_or_huge_dimensions_do_not_reach_drag(self):
        for raw in (b'not-a-png',b'a'*600000,png(np.zeros((900,900,3),np.uint8))):
            with self.assertRaises(captcha.CaptchaError): slider.decode(raw)

    def test_localization_failure_does_not_submit_or_send_coordinates(self):
        page,gate=MagicMock(),MagicMock()
        with (patch.object(slider,'read_challenge',return_value=(None,b'',b'','hash')),
             patch.object(slider,'locate_gap',side_effect=captcha.CaptchaError('转手动'))):
            result=slider.drag_once(page,gate)
        self.assertEqual(result['phase'],'manual_required')
        page.mouse.down.assert_not_called()
        gate.authorize_captcha.assert_not_called()


class CaptchaGateTests(unittest.TestCase):
    def test_check_requires_current_challenge_and_explicit_authorization(self):
        gate=booking.NetworkGate('prepare')
        gate.verification_active=True
        fingerprint=challenge(gate)
        r=route('/captcha/check')
        r.request.post_data_json={'token':'EXAMPLE-TOKEN','captchaType':'blockPuzzle','pointJson':'ENCRYPTED'}
        gate(r)
        r.abort.assert_called_once()
        gate.authorize_captcha(fingerprint)
        allowed=route('/captcha/check')
        allowed.request.post_data_json=r.request.post_data_json
        gate(allowed)
        allowed.continue_.assert_called_once()
        repeat=route('/captcha/check');repeat.request.post_data_json=r.request.post_data_json
        gate(repeat);repeat.abort.assert_called_once()

    def test_refresh_revokes_old_solution_and_credential_hashes_are_not_logged(self):
        gate=booking.NetworkGate('prepare');gate.verification_active=True
        fingerprint=challenge(gate)
        gate.authorize_captcha(fingerprint)
        challenge(gate,'NEW-TOKEN')
        r=route('/captcha/check')
        r.request.post_data_json={'token':'EXAMPLE-TOKEN','captchaType':'blockPuzzle'}
        gate(r);r.abort.assert_called_once()
        self.assertNotIn('TOKEN',json.dumps(gate.events))

    def test_no_secondary_submission_without_server_verification_and_header(self):
        for verified,header in ((None,{'tid':'x'}),('hash',{})):
            gate=booking.NetworkGate('submit',before_send=MagicMock())
            gate.attempts=1;gate.retry_armed=True;gate.verified_token_hash=verified
            gate.before_verified_send=MagicMock()
            r=route(booking.CONFIRM,header)
            gate(r);r.abort.assert_called_once()
            r.continue_.assert_not_called()

    def test_verified_submission_is_durable_once_and_cannot_trigger_a_third_write(self):
        actions=[]
        gate=booking.NetworkGate('submit',before_send=MagicMock())
        gate.attempts=1;gate.retry_armed=True;gate.verified_token_hash='hash'
        gate.before_verified_send=lambda:actions.append('persisted')
        r=route(booking.CONFIRM,{'tid':'ENCRYPTED'})
        r.continue_.side_effect=lambda:actions.append('forwarded')
        gate(r)
        self.assertEqual(actions,['persisted','forwarded'])
        gate.retry_armed=True
        third=route(booking.CONFIRM,{'tid':'ENCRYPTED'});gate(third)
        third.abort.assert_called_once()

    def test_check_limit_never_automatically_sends_extra_checks(self):
        gate=booking.NetworkGate('prepare');gate.verification_active=True
        challenge(gate)
        gate.captcha_checks=gate.captcha_limit
        with self.assertRaises(captcha.CaptchaError): gate.authorize_captcha(gate.captcha_image_hash)

    def test_refresh_revokes_previous_success_and_failed_response_cannot_restore_it(self):
        gate=booking.NetworkGate('submit')
        gate.verified_token_hash='old-proof'
        challenge(gate)
        self.assertIsNone(gate.verified_token_hash)
        response=MagicMock()
        response.status=200;response.url='https://'+booking.HOST+'/captcha/check'
        response.request.post_data_json={'token':'EXAMPLE-TOKEN'}
        response.json.return_value={'repCode':'9999'}
        gate.observe_response(response)
        self.assertIsNone(gate.verified_token_hash)

    def test_disk_failure_before_verified_continuation_blocks_the_request(self):
        gate=booking.NetworkGate('submit',before_send=MagicMock())
        gate.attempts=1;gate.retry_armed=True;gate.verified_token_hash='hash'
        gate.before_verified_send=MagicMock(side_effect=OSError('disk full'))
        r=route(booking.CONFIRM,{'tid':'ENCRYPTED'})
        with self.assertRaises(OSError): gate(r)
        r.continue_.assert_not_called()


class AutomaticLoginTests(unittest.TestCase):
    def make_flow(self,bodies):
        config={'auth':{k:'.local/auth/'+k+'.json' for k in ('cookies_file','storage_state_file','session_storage_file')}}
        flow=login_flow.LoginBrowser(config,lambda *args:None)
        flow.page=MagicMock()
        pendings=[]
        for body in bodies:
            pending=MagicMock();pending.value.status=200;pending.value.json.return_value=body
            pending.__enter__.return_value=pending
            pendings.append(pending)
        flow.page.expect_response.side_effect=pendings
        flow.capture_captcha=MagicMock(return_value=b'png')
        flow.submit_captcha=MagicMock()
        flow.current_user=MagicMock()
        return flow

    def test_captcha_rejection_is_retried_once_but_password_error_is_not(self):
        cases=[([{'errno':1,'error':'Wrong captcha'},{'errno':0}],True,2),
               ([{'errno':1,'error':'Wrong password'}],False,1),
               ([{'errno':1,'error':'Wrong captcha'},{'errno':1,'error':'Wrong captcha'}],False,2)]
        for bodies,success,attempts in cases:
            flow=self.make_flow(bodies)
            with patch.object(captcha,'recognize_ocr',return_value='aB39'),patch.object(login_flow,'atomic_json'):
                result=flow.automatic_login()
            self.assertEqual(result['authenticated'],success)
            self.assertEqual(flow.submit_captcha.call_count,attempts)
            self.assertEqual(result['manual_required'],not success)

    def test_network_uncertainty_is_never_retried_and_logs_no_exception_body(self):
        flow=self.make_flow([{'errno':0}])
        flow.page.expect_response.side_effect=RuntimeError('EXAMPLE-PASSWORD')
        with patch.object(captcha,'recognize_ocr',return_value='aB39'),patch.object(login_flow,'atomic_json'):
            result=flow.automatic_login()
        self.assertEqual(flow.page.expect_response.call_count,1)
        self.assertTrue(result['manual_required'])
        self.assertNotIn('EXAMPLE',json.dumps(result))


if __name__=='__main__': unittest.main()
