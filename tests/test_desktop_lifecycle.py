"""Regressions for Tcl-only popdowns and driver/worker shutdown, without site requests."""
from contextlib import contextmanager
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import MagicMock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'scripts'))
from desktop_worker import ScrollableFrame
import login
from configuration import read_config
from login_flow import LoginBrowser
from playwright_runtime import sync_playwright


class ScrollWheelTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry('440x260+40+40')
        self.frame = ScrollableFrame(self.root)
        self.frame.pack(fill='both', expand=True)
        self.combo = ttk.Combobox(self.frame.content, values=tuple(str(i) for i in range(30)))
        self.combo.pack()
        self.label = ttk.Label(self.frame.content, text='Fixture scroll area')
        self.label.pack()
        ttk.Frame(self.frame.content, height=1200).pack()
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def event_over(self, path, delta=-120):
        return SimpleNamespace(x_root=int(self.root.tk.call('winfo', 'rootx', path)) + 8,
            y_root=int(self.root.tk.call('winfo', 'rooty', path)) + 8, delta=delta)

    def wheel_over(self, widget):
        # Keep ordinary hit testing independent of other foreground Windows apps.
        native = self.root.tk
        class HitTestDispatcher:
            def call(self, *args):
                if args[:2] == ('winfo', 'containing'): return str(widget)
                return native.call(*args)
            def __getattr__(self, name):
                return getattr(native, name)
        dispatcher = HitTestDispatcher()
        with patch.object(self.frame, 'tk', dispatcher), patch.object(self.frame.canvas, 'tk', dispatcher):
            self.frame.wheel(self.event_over(str(widget)))

    def test_tcl_only_combobox_popdown_does_not_raise_or_scroll_page(self):
        self.root.tk.call('ttk::combobox::Post', str(self.combo))
        self.root.update()
        popup = str(self.root.tk.call('ttk::combobox::PopdownWindow', str(self.combo))) + '.f.l'
        self.assertEqual(self.root.tk.call('winfo', 'class', popup), 'Listbox')
        # The real Tcl dropdown is absent from Tkinter's children mapping.
        # Check that mapping directly: a CI runner may have another window on top.
        with self.assertRaises(KeyError):
            self.frame.canvas.nametowidget(popup)
        before = self.frame.canvas.yview()
        self.wheel_over(popup)
        self.assertEqual(self.frame.canvas.yview(), before)
        self.root.tk.call('ttk::combobox::Unpost', str(self.combo))

    def test_page_descendant_still_scrolls(self):
        before = self.frame.canvas.yview()[0]
        self.wheel_over(self.label)
        self.assertGreater(self.frame.canvas.yview()[0], before)

    def test_closed_combobox_keeps_its_own_wheel_behavior(self):
        before = self.frame.canvas.yview()
        self.wheel_over(self.combo)
        self.assertEqual(self.frame.canvas.yview(), before)

    def test_text_keeps_its_own_scrolling(self):
        text = tk.Text(self.frame.content, height=2)
        text.pack(before=self.label)
        self.root.update()
        before = self.frame.canvas.yview()
        self.wheel_over(text)
        self.assertEqual(self.frame.canvas.yview(), before)

    def test_destroy_removes_only_this_frames_binding(self):
        sibling = ScrollableFrame(self.root)
        own_binding, sibling_binding = self.frame.wheel_binding, sibling.wheel_binding
        self.frame.destroy()
        script = self.root.bind('<MouseWheel>')
        self.assertNotIn(own_binding, script)
        self.assertIn(sibling_binding, script)
        sibling.destroy()
        self.assertNotIn(sibling_binding, self.root.bind('<MouseWheel>'))


class LoginWorkerShutdownTests(unittest.TestCase):
    def run_worker(self, actions, flow=None):
        commands, events = queue.Queue(), queue.Queue()
        for action in actions: commands.put(action)
        with patch.object(login, 'sync_playwright') as driver, \
                patch.object(login, 'LoginBrowser', return_value=flow), patch.object(login, 'atomic_json'):
            login.browser_worker({}, commands, events)
        return driver, list(events.queue)

    def test_close_without_login_never_starts_driver(self):
        driver, events = self.run_worker([('quit', None)])
        driver.assert_not_called()

    def test_missing_page_action_does_not_start_driver(self):
        driver, events = self.run_worker([('capture', None), ('quit', None)])
        driver.assert_not_called()
        self.assertIn(('error', '请先打开登录页。'), events)

    def test_failed_browser_start_is_closed_before_driver_exit(self):
        flow = MagicMock()
        flow.browser = None
        flow.start.side_effect = RuntimeError('EXAMPLE-PRIVATE-DETAIL')
        lifecycle = []
        @contextmanager
        def driver():
            try: yield MagicMock()
            finally: lifecycle.append('driver closed')
        flow.close.side_effect = lambda: lifecycle.append('browser closed')
        commands, events = queue.Queue(), queue.Queue()
        commands.put(('open', None)); commands.put(('quit', None))
        with patch.object(login, 'sync_playwright', driver), patch.object(login, 'LoginBrowser', return_value=flow), \
                patch.object(login, 'atomic_json'):
            login.browser_worker({}, commands, events)
        self.assertEqual(lifecycle, ['browser closed', 'driver closed'])
        self.assertNotIn('PRIVATE', str(list(events.queue)))

    def test_disconnected_browser_is_closed_before_references_are_dropped(self):
        flow = MagicMock()
        flow.browser.is_connected.return_value = False
        driver, events = self.run_worker([('open', None), ('quit', None)], flow)
        flow.close.assert_called_once()
        self.assertTrue(any('浏览器已关闭' in str(event) for event in events))

    def test_close_clears_references_even_if_browser_close_raises(self):
        flow = LoginBrowser(read_config(PROJECT / 'config/config.example.toml'), lambda *args: None)
        flow.browser = MagicMock()
        flow.context = MagicMock(); flow.page = MagicMock()
        flow.browser.close.side_effect = RuntimeError('fixture close failure')
        with self.assertRaises(RuntimeError): flow.close()
        self.assertIsNone(flow.browser); self.assertIsNone(flow.context); self.assertIsNone(flow.page)
        flow.close()

    def test_real_driver_repeated_immediate_close_has_no_unhandled_tasks(self):
        script = """
import gc, sys
sys.path.insert(0, 'scripts')
from playwright_runtime import sync_playwright
for index in range(10):
    with sync_playwright() as driver:
        pass
gc.collect()
print('clean driver shutdown')
"""
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script], cwd=PROJECT,
            capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('clean driver shutdown', result.stdout)
        for marker in ('Task was destroyed', 'Future exception was never retrieved', 'TargetClosedError', 'Traceback'):
            self.assertNotIn(marker, result.stderr)

    def test_readiness_failure_remains_visible_and_exits_manager(self):
        manager = MagicMock()
        manager.__enter__.return_value.request.new_context.side_effect = RuntimeError('fixture startup failure')
        with patch('playwright.sync_api.sync_playwright', return_value=manager):
            with self.assertRaisesRegex(RuntimeError, 'fixture startup failure'):
                with sync_playwright(): pass
        manager.__exit__.assert_called_once()


class LoginWindowShutdownTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk(); self.root.withdraw()
        self.host = ttk.Frame(self.root)
        self.ui = login.LoginApp({}, auto_open=False, start_worker=False, window=self.root, parent=self.host)

    def tearDown(self):
        self.ui.worker = None
        self.ui.pending = False
        self.ui.close()
        self.root.destroy()

    def test_shutdown_waits_without_blocking_or_queuing_duplicate_quit(self):
        self.ui.worker = MagicMock()
        self.ui.worker.is_alive.return_value = True
        self.assertFalse(self.ui.close())
        self.assertFalse(self.ui.closed)
        self.assertTrue(self.ui.outer.winfo_exists())
        self.assertFalse(self.ui.close())
        self.ui.worker.join.assert_not_called()
        self.assertEqual(list(self.ui.commands.queue), [('quit', None)])
        self.ui.enqueue('open')
        self.assertEqual(list(self.ui.commands.queue), [('quit', None)])
        self.ui.worker.is_alive.return_value = False
        self.assertTrue(self.ui.close())
        self.assertFalse(self.ui.outer.winfo_exists())

    def test_done_during_shutdown_does_not_reenable_controls(self):
        self.ui.worker = MagicMock()
        self.ui.worker.is_alive.return_value = True
        self.ui.close()
        self.ui.events.put(('done', None))
        self.root.after_cancel(self.ui.poll_id)
        self.ui.poll_events()
        self.assertEqual(str(self.ui.browser_choice.cget('state')), 'disabled')

    def test_real_idle_worker_exits_before_window_is_destroyed(self):
        with patch.object(login, 'sync_playwright') as driver:
            self.ui.worker = threading.Thread(target=login.browser_worker,
                args=({}, self.ui.commands, self.ui.events), daemon=True)
            self.ui.worker.start()
            deadline = time.monotonic() + 3
            while not self.ui.close():
                self.root.update()
                if time.monotonic() >= deadline: self.fail('Login worker did not stop')
                time.sleep(.01)
            self.assertFalse(self.ui.worker.is_alive())
            driver.assert_not_called()


if __name__ == '__main__': unittest.main()
