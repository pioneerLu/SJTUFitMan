"""Publishing must exclude private files, including accidental force-adds."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import check_release
import configuration


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root/'config').mkdir()
        (self.root/'scripts').mkdir()
        (self.root/'config/config.example.toml').write_bytes(configuration.EXAMPLE_PATH.read_bytes())
        (self.root/'.gitignore').write_bytes((configuration.ROOT/'.gitignore').read_bytes())

    def test_clean_project_without_git_is_safe_and_does_not_initialize_config(self):
        issues, count, repository = check_release.check_release(self.root)
        self.assertEqual(issues, [])
        self.assertGreater(count, 0)
        self.assertFalse(repository)
        self.assertFalse((self.root/'config/config.toml').exists())

    def test_reports_only_filename_for_leaked_local_code_and_escaped_password(self):
        password = 'EXAMPLE-password-with-"-quotes'
        configuration.write_config({'auth': {'password': password},
                                    'mail': {'password': 'EXAMPLE-MAIL-CODE'}},
                                   self.root/'config/config.toml')
        source = self.root/'scripts/accidental.py'
        source.write_text('value = '+repr('EXAMPLE-MAIL-CODE'), encoding='utf-8')
        other = self.root/'scripts/another.py'
        other.write_text('value = '+check_release.json.dumps(password), encoding='utf-8')
        issues, _, _ = check_release.check_release(self.root)
        self.assertEqual(len(issues), 2)
        self.assertIn('scripts/accidental.py', str(issues))
        self.assertNotIn(password, str(issues))
        self.assertNotIn('EXAMPLE-MAIL-CODE', str(issues))

    def test_personal_fields_in_public_template_are_rejected_without_printing_values(self):
        data = configuration.read_config(self.root/'config/config.example.toml')
        data['mail']['recipient'] = 'EXAMPLE-PRIVATE@example.test'
        configuration.write_config(data, self.root/'config/config.example.toml')
        issues, _, _ = check_release.check_release(self.root)
        self.assertTrue(issues)
        self.assertNotIn('EXAMPLE-PRIVATE', str(issues))

    @unittest.skipUnless(shutil.which('git'), 'Git not installed')
    def test_cleaning_worktree_does_not_hide_a_secret_already_in_index(self):
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True, capture_output=True)
        configuration.write_config({'auth': {'password': 'EXAMPLE-STAGED-SECRET'}},
                                   self.root/'config/config.toml')
        source = self.root/'scripts/accidental.py'
        source.write_text('password="EXAMPLE-STAGED-SECRET"', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'add', '--', 'scripts/accidental.py'],
                       check=True, capture_output=True)
        source.write_text('password=""', encoding='utf-8')
        issues, _, _ = check_release.check_release(self.root)
        self.assertEqual(issues, ['暂存区发现与本地个人配置相同的内容：scripts/accidental.py'])
        self.assertNotIn('EXAMPLE-STAGED-SECRET', str(issues))

    @unittest.skipUnless(shutil.which('git'), 'Git not installed')
    def test_ignored_runtime_is_safe_but_force_added_private_config_is_rejected(self):
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True, capture_output=True)
        configuration.ensure_config(self.root)
        runtime = self.root/'.local/auth/storage-state.json'
        runtime.parent.mkdir(parents=True)
        runtime.write_text('EXAMPLE-SESSION', encoding='utf-8')
        issues, _, repository = check_release.check_release(self.root)
        self.assertTrue(repository)
        self.assertEqual(issues, [])
        subprocess.run(['git', '-C', str(self.root), 'add', '-f', '--', 'config/config.toml'],
                       check=True, capture_output=True)
        issues, _, _ = check_release.check_release(self.root)
        self.assertIn('私有文件进入分享清单：config/config.toml', issues)


if __name__ == '__main__':
    unittest.main()
