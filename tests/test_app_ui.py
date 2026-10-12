"""Three-page navigation and task isolation, using fixture configs and no real workers."""
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from app_ui import FitnessApp
from configuration import read_config, write_config, ConfigError
import desktop_worker
import login as login_ui
from test_polling import mailbox


class UnifiedAppTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'config.toml'
        self.path.write_text((Path(__file__).resolve().parents[1] / 'config/config.example.toml').read_text(encoding='utf-8'), encoding='utf-8')
        config = read_config(self.path); config['mail'] = asdict(mailbox()); write_config(config, self.path)
        self.window = tk.Tk(); self.window.withdraw()
        self.app = FitnessApp(self.window, start_workers=False, config_path=self.path, records_root=self.root)

    def tearDown(self):
        if not self.app.closed:
            for component in self.app.components.values():
                if hasattr(component, 'login'): component.login.pending = False; component.mail.busy = False
                elif hasattr(component, 'busy'): component.busy = False
            self.app.close()

    def test_plain_start_has_only_three_entries_and_no_order_or_mail_worker(self):
        self.assertEqual(tuple(self.app.PAGES.values()), ('自动预约', '记录', '设置'))
        self.assertEqual(self.app.current_page, 'auto')
        self.assertEqual(set(self.app.components), {'auto'})
        self.assertIsNone(self.app.components['auto'].process)
        self.assertIsNone(self.app.mail_service.thread)
        self.assertNotIn('booking', self.app.PAGES)

    def test_navigation_reuses_window_and_preserves_plan_drafts(self):
        automatic = self.app.components['auto']
        automatic.variables['interval_seconds'].set('90')
        automatic.time_picker.duration.set('连续2小时'); automatic.time_picker.refresh_choices()
        automatic.time_picker.selected.set('18:00-20:00')
        self.app.show_page('records'); records = self.app.components['records']
        self.app.show_page('settings'); settings = self.app.components['settings']
        self.assertIs(settings.login.root, self.window); self.assertIsNone(settings.login.worker)
        self.app.show_page('auto')
        self.assertIs(self.app.components['auto'], automatic)
        self.assertEqual(automatic.variables['interval_seconds'].get(), '90')
        self.assertEqual(automatic.time_picker.get(), ['18:00-20:00'])
        self.assertIsNone(records.process); self.assertFalse(settings.pending); self.assertFalse(settings.busy)
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in self.window.winfo_children()))

    def test_running_reservation_blocks_login_and_settings_writes(self):
        self.app.show_page('settings'); settings = self.app.components['settings']
        automatic = self.app.components['auto']
        automatic.busy = True; automatic.running_command = 'waitlist'
        before = self.path.read_bytes()
        settings.login.enqueue('open')
        self.assertFalse(settings.login.pending); self.assertTrue(settings.login.commands.empty())
        self.assertFalse(settings.mail.save())
        with patch.object(login_ui, 'update_config') as save:
            settings.login.save_credentials(); save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn('自动预约', settings.status.get())
        automatic.busy = False

    def test_page_switch_keeps_reservation_running_and_blocks_second_worker(self):
        automatic = self.app.components['auto']
        automatic.busy = True; automatic.running_command = 'run'; automatic.stop = MagicMock()
        self.app.show_page('records')
        with patch.object(desktop_worker.subprocess, 'Popen') as launched:
            self.app.components['records'].reconcile(); launched.assert_not_called()
        self.app.refresh_status()
        self.assertEqual(str(self.app.stop_button.cget('state')), 'normal')
        self.app.stop_polling(); automatic.stop.assert_called_once()
        self.assertTrue(automatic.busy); automatic.busy = False

    def test_account_save_remains_explicit_and_private(self):
        self.app.show_page('settings'); login = self.app.components['settings'].login
        with patch.object(login_ui, 'update_config') as save:
            login.username.set('EXAMPLE-STUDENT'); login.password.set('EXAMPLE-PRIVATE-PASSWORD')
            save.assert_not_called()
            save.return_value = {'auth': login.credentials()}
            login.save_credentials()
            save.assert_called_once_with({'auth': login.credentials()}, path=self.path)
            self.assertNotIn('EXAMPLE', login.status.get())

    def test_compact_login_exposes_automatic_and_manual_actions(self):
        self.app.show_page('settings'); login = self.app.components['settings'].login
        labels = [button.cget('text') for button in login.buttons]
        self.assertIn('自动登录', labels); self.assertIn('浏览器手动登录', labels)
        self.assertNotIn('填入浏览器', labels)
        self.assertFalse(login.more_login.get())
        self.assertEqual(login.advanced_login.winfo_manager(), '')

    def test_close_stops_polling_and_waits_for_order_verification(self):
        automatic = self.app.components['auto']
        automatic.busy = True; automatic.running_command = 'waitlist'; automatic.stop = MagicMock()
        self.app.close()
        self.assertTrue(self.app.closing); self.assertFalse(self.app.closed)
        automatic.stop.assert_called_once()
        self.app.close(); automatic.stop.assert_called_once()
        automatic.busy = False; self.app.close(); self.assertTrue(self.app.closed)

    def test_close_waits_for_current_mail_delivery(self):
        self.app.mail_service.close = MagicMock(return_value=False)
        self.app.close(); self.assertFalse(self.app.closed)
        self.app.mail_service.close.return_value = True
        self.app.close(); self.assertTrue(self.app.closed)

    def test_close_waits_for_login_browser_worker_to_exit(self):
        self.app.show_page('settings')
        login = self.app.components['settings'].login
        login.worker = MagicMock()
        login.worker.is_alive.return_value = True
        self.app.close()
        self.assertFalse(self.app.closed)
        self.assertTrue(self.window.winfo_exists())
        login.worker.join.assert_not_called()
        login.worker.is_alive.return_value = False
        self.app.close()
        self.assertTrue(self.app.closed)

    def test_failed_account_config_cannot_expose_private_exception(self):
        self.app.config_loader = MagicMock(side_effect=ValueError('EXAMPLE-PRIVATE-SECRET'))
        self.app.show_page('settings')
        self.assertNotIn('settings', self.app.components)
        self.assertNotIn('EXAMPLE', self.app.task_status.get())
        self.assertEqual(self.app.current_page, 'auto')


if __name__ == '__main__': unittest.main()
