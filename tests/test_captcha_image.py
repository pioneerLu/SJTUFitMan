"""Regression coverage for full native pixels, readable previews and stale CAPTCHA input."""
import base64
import hashlib
import io
from pathlib import Path
import sys
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from PIL import Image
from captcha import CaptchaError
from captcha_image import captcha_preview, decode_native_image
from login import LoginApp
from login_flow import FlowError, LoginBrowser


def png(size=(110, 40), color='white'):
    image = Image.new('RGB', size, color)
    # Edge markers must survive the preview; a crop would remove them.
    for x in range(image.width):
        image.putpixel((x, 0), (255, 0, 0))
        image.putpixel((x, image.height - 1), (0, 0, 255))
    stream = io.BytesIO(); image.save(stream, format='PNG')
    return stream.getvalue()


def snapshot(image, source='captcha?example=1'):
    return {'source': source, 'png': 'data:image/png;base64,' + base64.b64encode(image).decode()}


class CaptchaImageTests(unittest.TestCase):
    def test_preview_enlarges_small_image_without_removing_edges(self):
        preview = captcha_preview(png())
        self.assertEqual(preview.size, (330, 120))
        self.assertGreater(preview.getpixel((150, 0))[0], 200)
        self.assertGreater(preview.getpixel((150, 119))[2], 200)

    def test_large_preview_keeps_aspect_ratio_and_bounds(self):
        self.assertEqual(captcha_preview(png((600, 300))).size, (280, 140))

    def test_transparent_image_has_white_preview_background(self):
        stream = io.BytesIO(); Image.new('RGBA', (110, 40), (0, 0, 0, 0)).save(stream, format='PNG')
        self.assertEqual(captcha_preview(stream.getvalue()).getpixel((20, 20)), (255, 255, 255))

    def test_native_export_preserves_pixels_and_rejects_invalid_payloads(self):
        original = png()
        self.assertEqual(decode_native_image(snapshot(original)), ('captcha?example=1', original))
        for bad in (None, {}, {'source': 'secret', 'png': '<script>'},
                    {'source': 'secret', 'png': 'data:image/png;base64,!!!'}, snapshot(b'bad-image')):
            with self.assertRaises(CaptchaError) as caught: decode_native_image(bad)
            self.assertNotIn('secret', str(caught.exception))

    def make_flow(self):
        events = []
        config = {'auth': {key: '.local/auth/' + key + '.json' for key in (
            'cookies_file', 'storage_state_file', 'session_storage_file')}}
        flow = LoginBrowser(config, lambda *event: events.append(event))
        flow.page = MagicMock()
        flow.ensure_login_form = MagicMock()
        return flow, events

    def test_capture_uses_full_native_image_instead_of_rendered_screenshot(self):
        flow, events = self.make_flow()
        native = png()
        flow.page.locator.return_value.evaluate.return_value = snapshot(native)
        self.assertEqual(flow.capture_captcha(), native)
        flow.page.locator.return_value.screenshot.assert_not_called()
        self.assertEqual(flow.captcha_digest, hashlib.sha256(native).hexdigest())
        self.assertEqual(events[-1], ('image', native))
        self.assertIn(('code', ''), events)

    def test_refresh_waits_for_new_image_and_replaces_preview_without_submitting(self):
        flow, events = self.make_flow()
        fresh = png(color='green')
        image = flow.page.locator.return_value
        image.evaluate.side_effect = [None, snapshot(fresh)]
        flow.captcha_source = 'old'; flow.captcha_digest = 'old-digest'
        flow.refresh_captcha()
        image.click.assert_called_once()
        self.assertEqual(events[-2], ('image', fresh))
        self.assertLess(events.index(('image', None)), events.index(('image', fresh)))
        self.assertEqual(flow.attempts, 0)
        flow.page.request.get.assert_not_called()

    def test_failed_refresh_drops_old_preview_and_answer(self):
        flow, events = self.make_flow()
        flow.captcha_source = 'old'; flow.captcha_digest = 'old-digest'
        flow.page.wait_for_function.side_effect = RuntimeError('private')
        with self.assertRaises(FlowError) as caught: flow.refresh_captcha()
        self.assertIsNone(flow.captcha_source); self.assertIsNone(flow.captcha_digest)
        self.assertIn(('image', None), events); self.assertIn(('code', ''), events)
        self.assertNotIn('private', str(caught.exception))

    def test_same_url_with_changed_pixels_cannot_submit_stale_answer(self):
        flow, events = self.make_flow()
        flow.captcha_source = 'same-source'
        flow.captcha_digest = hashlib.sha256(png()).hexdigest()
        flow.page.locator.return_value.get_attribute.return_value = 'same-source'
        flow.read_captcha = MagicMock(return_value=('same-source', png(color='green')))
        with self.assertRaises(FlowError): flow.submit_captcha('aB39')
        flow.page.locator.return_value.click.assert_not_called()
        self.assertEqual(flow.attempts, 0)
        self.assertIn(('image', None), events)
        with self.assertRaises(FlowError): flow.submit_captcha('aB39')
        flow.page.locator.return_value.click.assert_not_called()

    def test_current_native_image_can_be_manually_submitted_after_capture(self):
        flow, events = self.make_flow()
        native = png()
        image = flow.page.locator.return_value
        image.evaluate.return_value = snapshot(native)
        image.get_attribute.return_value = 'captcha?example=1'
        image.input_value.return_value = 'EXAMPLE-LOCAL-VALUE'
        flow.capture_captcha()
        flow.submit_captcha('aB39')
        image.click.assert_called_once()
        self.assertEqual(flow.attempts, 1)
        self.assertNotIn('EXAMPLE-LOCAL-VALUE', str(events))


class CaptchaPreviewUiTests(unittest.TestCase):
    def setUp(self):
        self.window = tk.Tk(); self.window.withdraw()
        self.login = LoginApp({}, auto_open=False, start_worker=False, window=self.window,
            parent=self.window, compact=True)

    def tearDown(self):
        self.login.pending = False
        self.login.close(); self.window.destroy()

    def drain(self):
        self.window.after_cancel(self.login.poll_id)
        self.login.poll_events(); self.window.update_idletasks()

    def test_native_image_is_enlarged_and_refresh_removes_old_photo_and_code(self):
        self.login.events.put(('image', png()))
        self.drain()
        self.assertEqual((self.login.image.width(), self.login.image.height()), (330, 120))
        self.login.code.set('aB39')
        self.login.events.put(('code', '')); self.login.events.put(('image', None))
        self.drain()
        self.assertIsNone(self.login.image); self.assertEqual(self.login.code.get(), '')
        self.assertEqual(self.login.image_label.cget('image'), '')

    def test_manual_fallback_opens_helper_without_triggering_a_request(self):
        self.login.events.put(('manual-captcha', None)); self.drain()
        self.assertTrue(self.login.more_login.get())
        self.assertEqual(self.login.advanced_login.winfo_manager(), 'pack')
        self.assertTrue(self.login.commands.empty())


if __name__ == '__main__': unittest.main()
