import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from dashboard.updates import Updater, safe_path


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout.decode().strip()


def manifest(root):
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '.git' not in p.parts and p.name != 'dashboard-update-manifest.json')
    (root / 'dashboard-update-manifest.json').write_text(json.dumps({'version': 1, 'files': sorted(names + ['dashboard-update-manifest.json'])}))


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.remote, self.local = self.base / 'remote', self.base / 'local'
        self.remote.mkdir()
        git(self.remote, 'init', '-b', 'main')
        git(self.remote, 'config', 'core.autocrlf', 'false')
        git(self.remote, 'config', 'user.name', 'Test')
        git(self.remote, 'config', 'user.email', 'test@example.invalid')
        for name in ('dashboard/main.py', 'manager/main.py', 'modules/plex/main.py'):
            p = self.remote / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('# initial\n')
        (self.remote / '.gitignore').write_text('data/\n.env\n')
        manifest(self.remote)
        git(self.remote, 'add', '.')
        git(self.remote, 'commit', '-m', 'initial')
        subprocess.run(['git', '-c', 'core.autocrlf=false', 'clone', str(self.remote), str(self.local)], check=True, capture_output=True)
        git(self.local, 'config', 'core.autocrlf', 'false')
        git(self.local, 'config', 'user.name', 'Test')
        git(self.local, 'config', 'user.email', 'test@example.invalid')
        git(self.remote, 'checkout', '-b', 'test/dashboard')
        (self.remote / 'dashboard/main.py').write_text('# updated\n')
        (self.remote / 'dashboard/extra.py').write_text('# new\n')
        manifest(self.remote)
        git(self.remote, 'add', '.')
        git(self.remote, 'commit', '-m', 'new version')
        self.patch = patch('dashboard.updates.REPOSITORY', str(self.remote))
        self.patch.start()
        self.updater = Updater(self.local)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_branch_preview_apply_backup_and_return_to_main(self):
        (self.local / 'manager/.env').write_text('secret=private\n')
        self.assertEqual(self.updater.branches(), ['main', 'test/dashboard'])
        plan = self.updater.prepare('test/dashboard')
        self.assertEqual((self.local / 'dashboard/main.py').read_text(), '# initial\n')
        self.assertTrue(any(p['change'] == 'ajout' for p in plan['files']))
        result = self.updater.apply(plan['id'])
        self.assertEqual(git(self.local, 'branch', '--show-current'), 'test/dashboard')
        self.assertEqual((self.local / 'manager/.env').read_text(), 'secret=private\n')
        with zipfile.ZipFile(self.local / result['backup']) as archive:
            self.assertEqual(archive.read('code/dashboard/main.py'), b'# initial\n')
            self.assertEqual(archive.read('configuration/manager/.env'), b'secret=private\n')
        rollback = self.updater.prepare('main')
        self.updater.apply(rollback['id'])
        self.assertFalse((self.local / 'dashboard/extra.py').exists())
        self.assertEqual((self.local / 'manager/.env').read_text(), 'secret=private\n')

    def test_dirty_repository_and_changed_preview_are_refused(self):
        (self.local / 'dashboard/main.py').write_text('# local\n')
        with self.assertRaises(ValueError): self.updater.prepare('test/dashboard')
        git(self.local, 'checkout', '--', 'dashboard/main.py')
        plan = self.updater.prepare('test/dashboard')
        (self.local / 'dashboard/main.py').write_text('# local\n')
        with self.assertRaises(ValueError): self.updater.apply(plan['id'])

    def test_local_branch_commits_are_not_overwritten(self):
        git(self.local, 'checkout', '-b', 'test/dashboard')
        (self.local / 'dashboard/main.py').write_text('# local commit\n')
        git(self.local, 'add', '.')
        git(self.local, 'commit', '-m', 'local')
        plan = self.updater.prepare('test/dashboard')
        with self.assertRaisesRegex(ValueError, 'commits'): self.updater.apply(plan['id'])
        self.assertEqual((self.local / 'dashboard/main.py').read_text(), '# local commit\n')

    def test_branch_without_compatible_dashboard_is_refused(self):
        git(self.remote, 'checkout', '-b', 'legacy')
        git(self.remote, 'rm', 'dashboard/main.py')
        manifest(self.remote)
        git(self.remote, 'add', '.')
        git(self.remote, 'commit', '-m', 'legacy')
        with self.assertRaisesRegex(ValueError, 'compatible'): self.updater.prepare('legacy')

    def test_manifest_cannot_add_credentials_or_private_data(self):
        for name in ('manager/.env', '.git/config', 'data/private.py', '../outside.py', '/outside.py', 'dashboard/link', 'config/services.json'):
            self.assertFalse(safe_path(name), name)
        for name in ('.env.example', 'manager/.env.example', 'dashboard/main.py', 'dashboard-update-manifest.json'):
            self.assertTrue(safe_path(name), name)
        (self.remote / 'manager/.env').write_text('secret=value\n')
        git(self.remote, 'add', '-f', 'manager/.env')
        git(self.remote, 'commit', '-m', 'unsafe file')
        with self.assertRaisesRegex(ValueError, 'non autorisé'): self.updater.prepare('test/dashboard')
