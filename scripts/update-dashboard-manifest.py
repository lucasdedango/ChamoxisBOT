"""Regenerate the reviewed code manifest, excluding every private/local file."""
import json
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from dashboard.updates import safe_path

names = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                       check=True, capture_output=True).stdout.decode('utf-8').split('\x00')
files = sorted({name for name in names if name and (root / name).is_file() and safe_path(name)} |
               {'dashboard-update-manifest.json'})
(root / 'dashboard-update-manifest.json').write_text(json.dumps({'version': 1, 'files': files}, indent=2) + '\n', encoding='utf-8')
print(f'Manifest written: {len(files)} files')
