"""Frozen workers and release privacy use isolated paths and dummy data."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import runtime
from build_release import distribution_issues
import desktop_entry


class PackagingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)

    def test_frozen_worker_never_uses_an_external_python_or_a_source_script(self):
        executable=self.root/'SJTUFitMan-worker.exe';executable.touch()
        with patch.multiple(runtime,FROZEN=True,ROOT=self.root):
            runtime.ensure_venv()
            command=runtime.worker_command('booking','--config',self.root/'config/config.toml','--help')
            self.assertEqual(command,[str(executable),'--worker','booking','--config',
                                      str(self.root/'config/config.toml'),'--help'])
        self.assertNotIn('python.exe',' '.join(command))
        self.assertNotIn('booking.py',' '.join(command))

    def test_source_worker_keeps_using_only_project_venv(self):
        executable=self.root/'.venv/Scripts/python.exe';executable.parent.mkdir(parents=True);executable.touch()
        with patch.multiple(runtime,FROZEN=False,ROOT=self.root):
            command=runtime.worker_command('poller','check')
        self.assertEqual(command,[str(executable),str(self.root/'scripts/poller.py'),'check'])

    def test_frozen_assets_are_separate_from_writable_user_data(self):
        embedded=self.root/'_internal'
        with patch.multiple(runtime,ROOT=self.root,BUNDLE_ROOT=embedded,FROZEN=True):
            self.assertEqual(runtime.asset_path('config/config.example.toml'),embedded/'config/config.example.toml')
            self.assertEqual(runtime.ROOT/'config/config.toml',self.root/'config/config.toml')

    def test_missing_worker_is_an_actionable_local_error(self):
        with patch.multiple(runtime,FROZEN=True,ROOT=self.root):
            with self.assertRaisesRegex(RuntimeError,'重新解压'): runtime.worker_command('poller','check')

    def test_release_rejects_runtime_records_and_credentials_in_generated_files(self):
        (self.root/'_internal').mkdir()
        (self.root/'_internal/code.bin').write_bytes(b'EXAMPLE-PRIVATE-CODE')
        (self.root/'config').mkdir();(self.root/'config/config.toml').write_text('[auth]',encoding='utf-8')
        (self.root/'.local').mkdir();(self.root/'.local/order.json').write_text('{}',encoding='utf-8')
        issues=distribution_issues(self.root,{b'EXAMPLE-PRIVATE-CODE'})
        self.assertEqual(issues,['.local','.local/order.json','_internal/code.bin','config/config.toml'])
        self.assertNotIn('EXAMPLE-PRIVATE-CODE',str(issues))

    def test_invalid_worker_dispatch_never_opens_the_ui(self):
        output=io.StringIO()
        with contextlib.redirect_stderr(output),patch.object(desktop_entry.multiprocessing,'freeze_support'):
            result=desktop_entry.main(['--worker','invalid'])
        self.assertEqual(result,2)
        self.assertIn('未知的后台模块',output.getvalue())


if __name__=='__main__': unittest.main()
