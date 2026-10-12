"""One browser preference and launcher shared by login and reservations."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys

LABELS = {'auto': '自动选择（优先Chrome）', 'chrome': 'Google Chrome', 'edge': 'Microsoft Edge'}
CHANNELS = {'chrome': 'chrome', 'edge': 'msedge'}
EXECUTABLES = {'chrome': 'chrome.exe', 'edge': 'msedge.exe'}
RELATIVE_PATHS = {'chrome': Path('Google/Chrome/Application/chrome.exe'),
                  'edge': Path('Microsoft/Edge/Application/msedge.exe')}


class BrowserError(RuntimeError):
    """Only fixed messages may leave the browser launcher."""


@dataclass(frozen=True)
class BrowserChoice:
    kind: str
    executable: Path = field(repr=False)

    @property
    def label(self): return LABELS[self.kind]

    @property
    def channel(self): return CHANNELS[self.kind]


def preferred_browser(config):
    values = config.get('browser', {})
    if not isinstance(values, dict):
        raise BrowserError('浏览器设置须选择自动、Chrome或Edge。')
    preference = values.get('preferred', 'auto')
    if not isinstance(preference, str) or preference not in LABELS:
        raise BrowserError('浏览器设置须选择自动、Chrome或Edge。')
    return preference


def registry_paths(kind):
    """Read standard Windows App Paths, including custom installation drives."""
    if sys.platform != 'win32': return []
    import winreg
    paths = []
    key = r'Software\Microsoft\Windows\CurrentVersion\App Paths' + '\\' + EXECUTABLES[kind]
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as opened:
                    value, value_type = winreg.QueryValueEx(opened, None)
                if value_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                    path = Path(os.path.expandvars(value).strip().strip('"'))
                    if path.name.casefold() == EXECUTABLES[kind]: paths.append(path)
            except OSError: pass
    return paths


def detect_browsers(environment=None):
    environment = os.environ if environment is None else environment
    found = {}
    for kind in CHANNELS:
        candidates = [Path(environment[name]) / RELATIVE_PATHS[kind]
            for name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'LOCALAPPDATA')
            if environment.get(name)]
        candidates.extend(registry_paths(kind))
        seen = set()
        for path in candidates:
            identity = os.path.normcase(str(path))
            if identity in seen: continue
            seen.add(identity)
            try:
                if path.is_file():
                    found[kind] = BrowserChoice(kind, path)
                    break
            except OSError: pass
    return found


def launch_browser(playwright, config, headless=False):
    preference = preferred_browser(config)
    detected = detect_browsers()
    kinds = ('chrome', 'edge') if preference == 'auto' else (preference,)
    available = [detected[kind] for kind in kinds if kind in detected]
    if not available:
        if preference == 'auto':
            raise BrowserError('未找到Chrome或Edge，请先安装其中一种浏览器。')
        raise BrowserError('未找到所选浏览器，请安装它或改为“自动选择”。')
    for choice in available:
        try:
            # The path is locally detected, never supplied by a site/config string.
            # Explicit paths also support per-user and custom-drive installations.
            browser = playwright.chromium.launch(channel=choice.channel,
                executable_path=str(choice.executable), headless=headless, timeout=20000)
            return browser, choice
        except Exception:
            # Auto may fall back only during local startup, before any page/login.
            continue
    raise BrowserError('所选浏览器未能启动，请关闭异常窗口后重试，或选择另一种浏览器。') from None
