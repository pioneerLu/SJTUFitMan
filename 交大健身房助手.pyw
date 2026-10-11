from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
from runtime import ensure_venv
try:
    ensure_venv()
except RuntimeError:
    import tkinter as tk
    from tkinter import messagebox
    window=tk.Tk();window.withdraw()
    messagebox.showerror('首次安装','请先按照README.md安装依赖：运行scripts/setup_env.ps1。',parent=window)
    window.destroy();raise SystemExit(2)
from desktop_entry import main

raise SystemExit(main())
