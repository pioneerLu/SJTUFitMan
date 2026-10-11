"""Source and frozen applications share writable paths and worker commands."""
import os
from pathlib import Path
import subprocess
import sys

FROZEN=bool(getattr(sys,'frozen',False))
ROOT=Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[1]
BUNDLE_ROOT=Path(getattr(sys,'_MEIPASS',ROOT)) if FROZEN else ROOT


def asset_path(relative):
    return BUNDLE_ROOT/relative


def python_path(windowless=False):
    if FROZEN:
        executable=ROOT/('交大健身房助手.exe' if windowless else 'SJTUFitMan-worker.exe')
        if not executable.is_file(): raise RuntimeError('程序文件不完整，请重新解压完整发布包。')
        return executable
    executable=ROOT/'.venv/Scripts'/('pythonw.exe' if windowless else 'python.exe')
    if not executable.is_file():
        raise RuntimeError('请先运行 scripts/setup_env.ps1 创建本项目环境。')
    return executable


def worker_command(module,*arguments):
    if module not in ('booking','poller'): raise ValueError('未知的后台模块。')
    executable=str(python_path())
    prefix=[executable,'--worker',module] if FROZEN else [executable,str(ROOT/'scripts'/f'{module}.py')]
    return [*prefix,*map(str,arguments)]


def ensure_venv():
    if FROZEN: return
    if Path(sys.prefix).resolve() != (ROOT/'.venv').resolve():
        python=python_path(Path(sys.executable).name.lower()=='pythonw.exe')
        settings={'cwd':ROOT}
        if os.name=='nt' and python.name=='pythonw.exe':
            settings['creationflags']=subprocess.CREATE_NO_WINDOW
        raise SystemExit(subprocess.call([str(python),*sys.argv],**settings))
