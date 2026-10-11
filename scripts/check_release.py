"""Offline checks for files about to be shared. Never print credential values."""
import json
from pathlib import Path
import subprocess
import sys
import tomllib

from runtime import ROOT

EXAMPLE = 'config/config.example.toml'
PRIVATE_PROBES = (
    'config.toml', 'reservation.toml', 'reservation.tmp', 'config/config.toml',
    'config/private/example.toml', 'config/.config-example.tmp',
    '.local/auth/storage-state.json', '.local/mail/outbox.json', '.local/order.json',
    '.venv/pyvenv.cfg', '.inspection/example.js', '.env', '.env.local',
)
PUBLIC_ROOT_FILES = (
    '.gitignore', '.gitattributes', 'README.md', '交大健身房助手.pyw',
    'requirements.txt', 'requirements-login.txt', 'requirements-booking.txt',
)


def private_path(name):
    """Reject private files even if someone force-added them to the index."""
    parts = Path(name).as_posix().lower().split('/')
    first = parts[0]
    return (
        first in {'.local', '.venv', '.inspection', '.git', '.pytest_cache', '.ruff_cache',
                  '.idea', '.vscode', 'dist', 'build'}
        or (first == 'config' and name.replace('\\', '/') != EXAMPLE)
        or first in {'config.toml', 'reservation.toml', 'reservation.tmp', '.env'}
        or first.startswith('.env.')
        or '__pycache__' in parts
        or Path(name).suffix.lower() in {'.pyc', '.pyo', '.log'}
    )


def git(root, *arguments, input_bytes=None):
    try:
        return subprocess.run(
            ['git', '-C', str(root), *arguments], capture_output=True, check=False, input=input_bytes,
        )
    except OSError:
        return None


def inventory(root):
    repository = git(root, 'rev-parse', '--show-toplevel')
    if repository and repository.returncode == 0:
        found = Path(repository.stdout.decode('utf-8').strip()).resolve()
        if found != root.resolve():
            raise ValueError('项目位于其他 Git 仓库内，请在项目目录建立独立仓库。')
        listed = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
        if listed is None or listed.returncode:
            raise ValueError('无法读取 Git 文件清单。')
        names = sorted(set(listed.stdout.decode('utf-8').rstrip('\0').split('\0')) - {''})
        return names, True
    names = [name for name in (*PUBLIC_ROOT_FILES, EXAMPLE) if (root/name).is_file()]
    for directory in ('scripts', 'tests', 'docs', '.github'):
        for path in (root/directory).rglob('*'):
            name = path.relative_to(root).as_posix()
            if path.is_file() and not private_path(name):
                names.append(name)
    return sorted(set(names)), False


def indexed_content(root):
    """Check staged blobs too; sanitizing a worktree file does not unstage its old secret."""
    listed = git(root, 'ls-files', '--stage', '-z')
    if listed is None or listed.returncode:
        raise ValueError('无法读取 Git 暂存区。')
    entries = []
    try:
        for item in listed.stdout.split(b'\0'):
            if not item:
                continue
            header, name = item.split(b'\t', 1)
            mode, oid, stage = header.split()
            if stage != b'0':
                raise ValueError('Git 暂存区存在未解决的冲突。')
            name = name.decode('utf-8')
            if not private_path(name):
                entries.append((name, oid))
        if not entries:
            return {}
        contents = git(root, 'cat-file', '--batch',
                       input_bytes=b''.join(oid+b'\n' for _, oid in entries))
        if contents is None or contents.returncode:
            raise ValueError('无法检查 Git 暂存文件内容。')
        cursor = 0
        result = {}
        for name, oid in entries:
            end = contents.stdout.index(b'\n', cursor)
            found_oid, kind, size = contents.stdout[cursor:end].split()
            if found_oid != oid or kind != b'blob':
                raise ValueError('Git 暂存文件内容检查未完成。')
            start = end+1
            cursor = start+int(size)
            result[name] = contents.stdout[start:cursor]
            if contents.stdout[cursor:cursor+1] != b'\n':
                raise ValueError('Git 暂存文件内容检查未完成。')
            cursor += 1
        return result
    except (UnicodeError, ValueError):
        raise ValueError('无法检查 Git 暂存区，请先修正索引或合并冲突。') from None


def read_toml(path):
    with path.open('rb') as stream:
        return tomllib.load(stream)


def secret_variants(data):
    # Exact local values only. Dummy credentials in tests are unrelated and safe to share.
    fields = {'auth': ('username', 'password', 'cookie_header'),
              'mail': ('username', 'password', 'sender', 'recipient')}
    values = set()
    for section, keys in fields.items():
        table = data.get(section, {})
        if not isinstance(table, dict):
            continue
        for key in keys:
            value = table.get(key)
            if isinstance(value, str) and value:
                values.add(value)
                values.add(json.dumps(value, ensure_ascii=False)[1:-1])
                values.add(json.dumps(value, ensure_ascii=True)[1:-1])
    return {value.encode('utf-8') for value in values if value}


def check_release(root=ROOT):
    root = Path(root)
    issues = []
    try:
        names, repository = inventory(root)
        indexed = indexed_content(root) if repository else {}
    except (ValueError, UnicodeError) as error:
        return [str(error)], 0, False
    for name in names:
        if private_path(name):
            issues.append(f'私有文件进入分享清单：{name}')
    try:
        template = read_toml(root/EXAMPLE)
        for section, keys in {'auth': ('username', 'password', 'cookie_header'),
                              'mail': ('username', 'password', 'sender', 'recipient')}.items():
            if any(template.get(section, {}).get(key, '') for key in keys):
                issues.append(f'样例中有非空个人字段：{EXAMPLE}')
        if template.get('polling', {}).get('enabled') is not False:
            issues.append(f'样例不应默认启用预约：{EXAMPLE}')
    except (OSError, ValueError, AttributeError):
        issues.append(f'配置样例缺失或格式有误：{EXAMPLE}')

    secrets = set()
    private_files = set()
    for relative in ('config/config.toml', 'config.toml', 'reservation.toml'):
        path = root/relative
        if not path.exists():
            continue
        try:
            data = read_toml(path)
            secrets.update(secret_variants(data))
            for key in ('cookies_file', 'storage_state_file', 'session_storage_file'):
                location = data.get('auth', {}).get(key)
                if isinstance(location, str) and location:
                    resolved = (root/location).resolve()
                    if resolved.is_relative_to(root.resolve()):
                        private_files.add(resolved.relative_to(root.resolve()).as_posix())
        except (OSError, ValueError, AttributeError):
            issues.append(f'无法读取本地配置，个人信息比对未完成：{relative}')

    for name in names:
        if private_path(name):
            continue
        path = root/name
        if name in private_files:
            issues.append(f'会话文件进入分享清单：{name}')
            continue
        if path.is_symlink():
            issues.append(f'分享清单包含符号链接，请核查指向：{name}')
            continue
        try:
            content = path.read_bytes()
        except OSError:
            issues.append(f'无法检查文件：{name}')
            continue
        if any(secret in content for secret in secrets):
            issues.append(f'发现与本地个人配置相同的内容：{name}')
        if any(secret in indexed.get(name, b'') for secret in secrets):
            issues.append(f'暂存区发现与本地个人配置相同的内容：{name}')

    if repository:
        for relative in (*PRIVATE_PROBES, *private_files):
            result = git(root, 'check-ignore', '--no-index', '--quiet', '--', relative)
            if result is None or result.returncode != 0:
                issues.append(f'Git 忽略规则未覆盖私有文件：{relative}')
    return sorted(set(issues)), len(names), repository


def main():
    issues, count, repository = check_release()
    if issues:
        for issue in issues:
            print(issue)
        print('发布检查未通过；请修正后再提交。')
        return 1
    print(f'发布检查通过：{count} 个分享文件，未发现个人配置内容。')
    if not repository:
        print('Git 尚未初始化；当前仅检查标准项目文件，初始化后请再次检查忽略规则。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
