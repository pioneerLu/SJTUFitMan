"""All application entry points use this project's isolated environment."""
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def python_path(windowless=False):
    executable=ROOT/'.venv/Scripts'/('pythonw.exe' if windowless else 'python.exe')
    if not executable.is_file():
        raise RuntimeError('请先运行 scripts/setup_env.ps1 创建本项目环境。')
    return executable


def ensure_venv():
    if Path(sys.prefix).resolve() != (ROOT/'.venv').resolve():
        python=python_path(Path(sys.executable).name.lower()=='pythonw.exe')
        settings={'cwd':ROOT}
        if os.name=='nt' and python.name=='pythonw.exe':
            settings['creationflags']=subprocess.CREATE_NO_WINDOW
        raise SystemExit(subprocess.call([str(python),*sys.argv],**settings))
