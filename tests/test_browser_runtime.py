"""Browser selection, startup fallback and private configuration boundaries."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import browser_runtime as runtime
import booking_flow
import login_flow
import login as login_ui
import poll_runner
import test_automatic_ui as form_fixtures
from configuration import read_config, ConfigError
from test_polling import Clock, mailbox
from test_waitlist import plan


def choices():
    return {kind:runtime.BrowserChoice(kind,Path('C:/Test Install')/runtime.EXECUTABLES[kind])
        for kind in ('chrome','edge')}


def config(preference='auto'):
    return {'browser':{'preferred':preference},'auth':{
        key:'.local/auth/'+key+'.json' for key in ('cookies_file','storage_state_file','session_storage_file')}}


class BrowserRuntimeTests(unittest.TestCase):
    def test_legacy_defaults_to_auto_and_bad_preferences_are_sanitized(self):
        self.assertEqual(runtime.preferred_browser({}),'auto')
        for bad in ('EXAMPLE-PRIVATE-SECRET',[],{},None,False):
            with self.assertRaises(runtime.BrowserError) as caught:
                runtime.preferred_browser({'browser':{'preferred':bad}})
            self.assertNotIn('EXAMPLE',str(caught.exception))

    def test_detects_edge_without_chrome_and_per_user_chrome(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(runtime,'registry_paths',return_value=[]):
            root=Path(temporary)
            edge=root/'Programs'/runtime.RELATIVE_PATHS['edge'];edge.parent.mkdir(parents=True);edge.touch()
            detected=runtime.detect_browsers({'PROGRAMFILES(X86)':str(root/'Programs')})
            self.assertEqual(tuple(detected),('edge',));self.assertEqual(detected['edge'].executable,edge)
            chrome=root/'User Apps'/runtime.RELATIVE_PATHS['chrome'];chrome.parent.mkdir(parents=True);chrome.touch()
            detected=runtime.detect_browsers({'PROGRAMFILES(X86)':str(root/'Programs'),'LOCALAPPDATA':str(root/'User Apps')})
            self.assertEqual(tuple(detected),('chrome','edge'))

    def test_registry_supports_custom_install_drives_without_exposing_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            edge=Path(temporary)/'Custom Install/msedge.exe';edge.parent.mkdir();edge.touch()
            with patch.object(runtime,'registry_paths',side_effect=lambda kind:[edge] if kind=='edge' else []):
                found=runtime.detect_browsers({})
            self.assertEqual(found['edge'].executable,edge)
            self.assertNotIn(str(edge),repr(found['edge']))

    def test_auto_prefers_chrome_and_explicit_edge_launches_only_edge(self):
        for preference,kind in (('auto','chrome'),('edge','edge')):
            pw=MagicMock()
            with patch.object(runtime,'detect_browsers',return_value=choices()):
                browser,selected=runtime.launch_browser(pw,config(preference),headless=True)
            self.assertEqual(selected.kind,kind)
            pw.chromium.launch.assert_called_once_with(channel=runtime.CHANNELS[kind],
                executable_path=str(choices()[kind].executable),headless=True,timeout=20000)
            self.assertIs(browser,pw.chromium.launch.return_value)

    def test_auto_edge_only_does_not_require_chrome(self):
        pw=MagicMock()
        with patch.object(runtime,'detect_browsers',return_value={'edge':choices()['edge']}):
            _,selected=runtime.launch_browser(pw,config())
        self.assertEqual(selected.kind,'edge');pw.chromium.launch.assert_called_once()

    def test_auto_can_fall_back_during_startup_before_any_site_request(self):
        pw=MagicMock();edge=MagicMock()
        pw.chromium.launch.side_effect=[RuntimeError('EXAMPLE-PRIVATE-TOKEN'),edge]
        with patch.object(runtime,'detect_browsers',return_value=choices()):
            browser,selected=runtime.launch_browser(pw,config())
        self.assertIs(browser,edge);self.assertEqual(selected.kind,'edge')
        self.assertEqual([call.kwargs['channel'] for call in pw.chromium.launch.call_args_list],['chrome','msedge'])
        edge.new_context.assert_not_called()

    def test_explicit_choice_does_not_silently_switch_or_echo_startup_errors(self):
        pw=MagicMock();pw.chromium.launch.side_effect=RuntimeError('EXAMPLE-PRIVATE-PASSWORD')
        with patch.object(runtime,'detect_browsers',return_value=choices()):
            with self.assertRaises(runtime.BrowserError) as caught:runtime.launch_browser(pw,config('edge'))
        pw.chromium.launch.assert_called_once();self.assertNotIn('EXAMPLE',str(caught.exception))

    def test_missing_explicit_browser_reports_error_without_launching_other_browser(self):
        pw=MagicMock()
        with patch.object(runtime,'detect_browsers',return_value={'chrome':choices()['chrome']}):
            with self.assertRaises(runtime.BrowserError):runtime.launch_browser(pw,config('edge'))
        pw.chromium.launch.assert_not_called()

    def test_no_browser_reports_installation_instead_of_repeated_startups(self):
        pw=MagicMock()
        with patch.object(runtime,'detect_browsers',return_value={}):
            with self.assertRaises(runtime.BrowserError) as caught:runtime.launch_browser(pw,config())
        self.assertIn('安装',str(caught.exception));pw.chromium.launch.assert_not_called()

    def test_login_and_reservation_use_same_shared_launcher(self):
        values=config('edge');pw=MagicMock();browser=MagicMock();context=MagicMock()
        context.cookies.return_value=[]
        with patch.object(login_flow,'launch_browser',return_value=(browser,choices()['edge'])) as launch:
            flow=login_flow.LoginBrowser(values,lambda *args:None)
            with patch.object(flow,'new_context',return_value=context),patch.object(flow,'ensure_login_form'):flow.start(pw)
            launch.assert_called_once_with(pw,values,headless=False)
            self.assertEqual(flow.browser_label,'Microsoft Edge')
            launch.reset_mock()
            booking=booking_flow.BookingBrowser(values)
            with patch.object(booking.login,'new_context',return_value=context),patch.object(booking.login,'current_user',
                    return_value={'code':0,'data':{'loginName':'example-account'}}):
                booking.start(pw,headless=True,load_appointment=False)
            launch.assert_called_once_with(pw,values,headless=True)
            self.assertEqual(booking.login.browser_label,'Microsoft Edge')

    def test_startup_failure_is_not_treated_as_an_authentication_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);clock=Clock();browser=MagicMock()
            browser.start.side_effect=runtime.BrowserError('未找到Chrome或Edge，请先安装其中一种浏览器。')
            with patch.multiple(poll_runner,ROOT=root,PRIVATE=root/'automation',STOP=root/'stop.json',
                    beijing_now=clock,publish=MagicMock()),patch.object(poll_runner,'load_waitlist_settings',return_value=(plan(),mailbox())), \
                    patch.object(poll_runner,'load_config',return_value=config()),patch.object(poll_runner,'service_outbox'), \
                    patch.object(poll_runner,'sync_playwright',return_value=MagicMock()),patch.object(poll_runner,'BookingBrowser',return_value=browser), \
                    patch.object(poll_runner,'restore_login') as restore:
                poll_runner.run_waitlist(root/'config.toml',SimpleNamespace(is_set=lambda:False))
            restore.assert_not_called();browser.start.assert_called_once();browser.close.assert_called_once()


class BrowserSelectionUiTests(unittest.TestCase):
    setUp=form_fixtures.AutomaticFormTests.setUp

    def tearDown(self):
        if hasattr(self,'local_login'):self.local_login.close()
        form_fixtures.AutomaticFormTests.tearDown(self)

    def make_login(self,guard=None):
        with patch.object(login_ui,'detect_browsers',return_value={'edge':choices()['edge']}):
            login=login_ui.LoginApp(read_config(self.path),auto_open=False,start_worker=False,window=self.window,
                parent=self.host,compact=True,config_path=self.path,activity_guard=guard)
        self.local_login=login
        return login

    def test_edge_selection_saves_only_browser_and_keeps_unsaved_password_in_memory(self):
        login=self.make_login();login.username.set('UNSAVED-EXAMPLE');login.password.set('UNSAVED-SECRET')
        login.browser_preference.set(runtime.LABELS['edge']);self.assertTrue(login.save_browser())
        saved=read_config(self.path)
        self.assertEqual(saved['browser']['preferred'],'edge')
        self.assertEqual(login.config['browser']['preferred'],'edge')
        for section in ('auth','mail','booking','waitlist','polling'):
            self.assertEqual(saved[section],self.before[section])
        self.assertIn('Microsoft Edge',login.browser_hint.get())
        self.assertNotIn('UNSAVED',login.status.get())
        self.assertTrue(login.commands.empty())

    def test_running_reservation_blocks_browser_changes(self):
        login=self.make_login(guard=lambda:False)
        login.browser_preference.set(runtime.LABELS['edge']);self.assertFalse(login.save_browser())
        self.assertEqual(read_config(self.path),self.before)
        self.assertEqual(login.browser_preference.get(),runtime.LABELS['auto'])

    def test_failed_save_keeps_current_preference_and_hides_exception_body(self):
        login=self.make_login();login.browser_preference.set(runtime.LABELS['edge'])
        with patch.object(login_ui,'update_config',side_effect=ConfigError('EXAMPLE-SECRET')):
            self.assertFalse(login.save_browser())
        self.assertEqual(login.browser_preference.get(),runtime.LABELS['auto'])
        self.assertNotIn('EXAMPLE',login.status.get());self.assertEqual(read_config(self.path),self.before)


if __name__=='__main__':unittest.main()
