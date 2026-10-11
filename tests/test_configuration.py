"""Personal config initialization and updates use isolated directories."""
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import configuration as cfg


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        (self.root/'config').mkdir()
        self.example=self.root/'config/config.example.toml'
        self.example.write_bytes(cfg.EXAMPLE_PATH.read_bytes())

    def read(self,path):
        with path.open('rb') as stream: return tomllib.load(stream)

    def test_first_install_creates_only_private_copy_and_does_not_arm_tasks(self):
        target=cfg.ensure_config(self.root)
        data=self.read(target)
        self.assertEqual(data,self.read(self.example))
        self.assertFalse(data['polling']['enabled'])
        self.assertEqual(data['auth']['password'],'')
        self.assertEqual(data['mail']['password'],'')

    def test_initialization_does_not_touch_runtime_records_or_create_backups(self):
        session=self.root/'.local/auth/storage-state.json';session.parent.mkdir(parents=True)
        session.write_text('EXAMPLE-SESSION-ONLY',encoding='utf-8')
        cfg.ensure_config(self.root)
        self.assertFalse((self.root/'config/private-backups').exists())
        self.assertEqual(session.read_text(encoding='utf-8'),'EXAMPLE-SESSION-ONLY')

    def test_existing_unified_config_is_not_replaced_by_example(self):
        target=self.root/'config/config.toml'
        cfg.write_config({'auth':{'username':'already-local','password':'keep-this'}},target)
        before=target.read_bytes()
        cfg.ensure_config(self.root)
        self.assertEqual(target.read_bytes(),before)

    def test_invalid_current_config_is_not_replaced_and_error_contains_no_secret(self):
        target=self.root/'config/config.toml';target.write_text('[auth\npassword="EXAMPLE-SECRET"',encoding='utf-8')
        before=target.read_bytes()
        with self.assertRaises(cfg.ConfigError) as caught: cfg.ensure_config(self.root)
        self.assertNotIn('EXAMPLE-SECRET',str(caught.exception))
        self.assertEqual(target.read_bytes(),before)

    def test_credential_updates_preserve_mail_and_plan_and_do_not_leak_to_example(self):
        target=cfg.ensure_config(self.root)
        data=self.read(target);data['mail']['password']='mail-code';data['booking']['time_slots']=['18:00-20:00']
        cfg.write_config(data,target)
        cfg.update_config({'auth':{'username':'EXAMPLE-USER','password':' auth-code '}},target)
        saved=self.read(target)
        self.assertEqual(saved['mail']['password'],'mail-code')
        self.assertEqual(saved['booking']['time_slots'],['18:00-20:00'])
        self.assertEqual(saved['auth']['password'],' auth-code ')
        self.assertNotIn('auth-code',self.example.read_text(encoding='utf-8'))

    def test_interrupted_atomic_save_keeps_previous_configuration(self):
        target=cfg.ensure_config(self.root);before=target.read_bytes()
        with patch.object(cfg.os,'replace',side_effect=OSError('EXAMPLE-PRIVATE-TEXT')):
            with self.assertRaises(cfg.ConfigError) as caught:
                cfg.write_config({'auth':{'password':'another-secret'}},target)
        self.assertEqual(target.read_bytes(),before)
        self.assertNotIn('EXAMPLE',str(caught.exception))
        self.assertEqual(list(target.parent.glob('.config-*.tmp')),[])

    def test_nested_custom_settings_and_literal_password_roundtrip(self):
        data={'auth':{'password':' quotes " \\ tabs\t newline\n '},
            'custom':{'nested':{'flag':True,'dates':[datetime(2026,10,11,12)],'empty':[]}}}
        self.assertEqual(tomllib.loads(cfg.dumps_config(data)),data)


if __name__=='__main__': unittest.main()
