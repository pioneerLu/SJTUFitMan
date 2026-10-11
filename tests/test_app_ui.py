"""Unified window lifecycle and task coordination without website or mail actions."""
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from app_ui import FitnessApp
import poller_ui
import login as login_ui
from configuration import ConfigError
from test_selection_ui import prepared


class UnifiedAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        path=Path(self.temporary.name)/'reservation.toml'
        path.write_text((Path(__file__).resolve().parents[1]/'config/config.example.toml').read_text(encoding='utf-8'),encoding='utf-8')
        self.path=path
        self.settings_patch=patch.object(poller_ui,'SETTINGS_PATH',path);self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.window=tk.Tk();self.window.withdraw()
        self.app=FitnessApp(self.window,start_workers=False,config_loader=lambda:{'auth':{}})

    def tearDown(self):
        if not self.app.closed:
            for component in self.app.components.values():
                if hasattr(component,'pending'): component.pending=False
                if hasattr(component,'busy'): component.busy=False
            self.app.close()

    def test_plain_start_opens_only_home_without_any_worker(self):
        self.assertEqual(self.app.components,{})
        self.assertEqual(self.app.current_page,'home')
        self.assertEqual(self.window.title(),'交大健身房助手')
        self.assertEqual(len(self.app.home_buttons),4)

    def test_pages_reuse_one_window_and_preserve_selection(self):
        self.app.show_page('login');login=self.app.components['login']
        self.assertIs(login.root,self.window)
        self.assertIsNone(login.worker);self.assertTrue(login.commands.empty())
        self.app.show_page('booking');booking=self.app.components['booking']
        self.assertIs(booking.window,self.window)
        booking.duration.set('连续2小时');booking.change_duration();booking.period.set('18:00-20:00')
        self.app.show_page('waitlist');polling=self.app.components['polling']
        self.assertIs(polling.window,self.window);self.assertIsNone(polling.process)
        self.assertEqual(polling.tabs.select(),str(polling.waitlist_tab))
        polling.waitlist_vars['interval_seconds'].set('90')
        self.app.show_page('mail');self.assertIs(self.app.components['polling'],polling)
        self.assertEqual(polling.tabs.select(),str(polling.mail_tab))
        self.app.show_page('daily');self.assertEqual(polling.tabs.select(),str(polling.booking_tab))
        self.app.show_page('booking')
        self.assertIs(self.app.components['booking'],booking)
        self.assertEqual(booking.period.get(),'18:00-20:00')
        self.assertEqual(polling.waitlist_vars['interval_seconds'].get(),'90')
        self.assertEqual(self.window.title(),'交大健身房助手')
        self.assertFalse(any(isinstance(child,tk.Toplevel) for child in self.window.winfo_children()))

    def test_active_booking_blocks_login_polling_and_config_writes(self):
        for page in ('login','booking','daily'): self.app.show_page(page)
        booking=self.app.components['booking'];login=self.app.components['login'];polling=self.app.components['polling']
        old_config=self.path.read_bytes();booking.busy=True
        login.enqueue('open')
        self.assertTrue(login.commands.empty());self.assertFalse(login.pending)
        with patch.object(poller_ui.subprocess,'Popen') as launch:
            polling.launch('waitlist');launch.assert_not_called()
        with patch.object(login_ui,'update_config') as save:
            login.save_credentials();save.assert_not_called()
        self.assertFalse(polling.save_form());self.assertEqual(self.path.read_bytes(),old_config)
        self.assertIn('单次预约',polling.status.get())
        booking.busy=False

    def test_account_save_is_explicit_and_never_displays_credentials(self):
        with patch.object(login_ui,'update_config') as save:
            self.app.show_page('login');login=self.app.components['login']
            login.username.set('EXAMPLE-STUDENT');login.password.set('EXAMPLE-PRIVATE-PASSWORD')
            save.assert_not_called()
            login.pending=True;login.save_credentials();save.assert_not_called()
            login.pending=False
            save.return_value={'auth':{**login.credentials(),'cookies_file':'.local/auth/cookies.json'}}
            login.save_credentials()
            save.assert_called_once_with({'auth':login.credentials()},path=login.config_path)
            self.assertEqual(login.config['auth']['cookies_file'],'.local/auth/cookies.json')
            self.assertNotIn('EXAMPLE',login.status.get())
            save.side_effect=ConfigError('配置未保存，请检查本地文件权限。')
            login.save_credentials();self.assertNotIn('EXAMPLE',login.status.get())

    def test_page_switch_during_polling_keeps_job_running_and_blocks_new_booking(self):
        self.app.show_page('daily');polling=self.app.components['polling']
        polling.busy=True;polling.running_command='waitlist';polling.stop=MagicMock()
        self.app.show_page('booking');booking=self.app.components['booking']
        booking.run('inspect',booking.selection());self.assertFalse(booking.busy)
        self.app.show_page('home');self.assertTrue(polling.busy)
        self.app.refresh_status()
        self.assertEqual(str(self.app.stop_button.cget('state')),'normal')
        self.app.stop_polling();polling.stop.assert_called_once()
        polling.busy=False

    def test_close_requests_stop_and_waits_for_order_verification(self):
        self.app.show_page('waitlist');polling=self.app.components['polling']
        polling.busy=True;polling.running_command='waitlist';polling.stop=MagicMock()
        self.app.close()
        self.assertTrue(self.app.closing);self.assertFalse(self.app.closed)
        self.assertTrue(self.window.winfo_exists());polling.stop.assert_called_once()
        self.assertFalse(self.app.allow_action('polling'))
        self.app.close();polling.stop.assert_called_once()
        polling.busy=False
        self.app.close();self.assertTrue(self.app.closed)

    def test_closing_unsubmitted_terms_does_not_create_order(self):
        self.app.show_page('booking');booking=self.app.components['booking']
        booking.prepared=prepared();booking.run=MagicMock()
        booking.confirm();self.assertIsNotNone(booking.review)
        self.app.close()
        self.assertTrue(self.app.closed);booking.run.assert_not_called()

    def test_disposing_embedded_module_does_not_destroy_main_window(self):
        self.app.show_page('login');login=self.app.components['login']
        self.assertTrue(login.close());self.assertTrue(self.window.winfo_exists())
        self.app.show_page('booking');booking=self.app.components['booking']
        self.assertTrue(booking.close());self.assertTrue(self.window.winfo_exists())

    def test_failed_local_configuration_does_not_expose_secret_exception(self):
        self.app.config_loader=MagicMock(side_effect=ValueError('EXAMPLE-PRIVATE-SECRET'))
        self.app.show_page('login')
        self.assertNotIn('login',self.app.components);self.assertNotIn('login',self.app.frames)
        self.assertNotIn('EXAMPLE',self.app.task_status.get())
        self.assertEqual(self.app.current_page,'home')


if __name__=='__main__': unittest.main()
