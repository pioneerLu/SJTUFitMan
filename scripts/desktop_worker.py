"""UI pages share hidden workers, action guards and graceful process shutdown."""
import json
import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk

from runtime import ROOT, worker_command
from configuration import CONFIG_PATH


def json_result(output):
    start = output.find('{')
    return json.JSONDecoder().raw_decode(output[start:])[0] if start >= 0 else None


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, height=1, width=1)
        scroll = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.canvas.pack(side='left', fill='both', expand=True)
        self.content = ttk.Frame(self.canvas)
        item = self.canvas.create_window((0, 0), window=self.content, anchor='nw')
        self.content.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda event: self.canvas.itemconfigure(item, width=event.width))
        self.wheel_target = self.winfo_toplevel()
        self.wheel_binding = self.wheel_target.bind('<MouseWheel>', self.wheel, add='+')
        self.bind('<Destroy>', self.remove_wheel_binding, add='+')

    def remove_wheel_binding(self, event):
        if event.widget == self and self.wheel_binding:
            try: self.wheel_target.unbind('<MouseWheel>', self.wheel_binding)
            except tk.TclError: pass
            self.wheel_binding = None

    def reveal(self, widget):
        self.update_idletasks()
        if not widget.winfo_ismapped(): return
        height = max(1, self.content.winfo_height())
        top = widget.winfo_rooty() - self.content.winfo_rooty()
        self.canvas.yview_moveto(max(0, top - 12) / height)

    def wheel(self, event):
        try:
            if not self.canvas.winfo_exists() or not self.canvas.winfo_ismapped(): return
            # Combobox popdowns exist only in Tcl, outside Tkinter's children dict.
            widget = str(self.tk.call('winfo', 'containing', event.x_root, event.y_root))
            while widget:
                if self.tk.call('winfo', 'class', widget) in ('Listbox', 'Text', 'TTreeview', 'TCombobox'): return
                if widget == str(self.canvas):
                    self.canvas.yview_scroll(-int(event.delta / 120), 'units'); return
                widget = str(self.tk.call('winfo', 'parent', widget))
        except tk.TclError: pass


class WorkerPage:
    def __init__(self, window, parent, config_path=CONFIG_PATH, activity_guard=None):
        self.window = window
        self.outer = parent
        self.config_path = config_path
        self.activity_guard = activity_guard or (lambda: True)
        self.status = tk.StringVar(value='就绪。')
        self.busy = False
        self.closed = False
        self.process = None
        self.running_command = None
        self.events = queue.Queue()
        self.timer = window.after(200, self.poll)

    def set_busy(self, busy):
        self.busy = busy

    def launch(self, module, command, *arguments):
        if self.busy or self.closed or not self.activity_guard():
            return False
        self.set_busy(True)
        options = dict(cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        try:
            command_line = worker_command(module, '--config' if module == 'booking' else '--settings',
                self.config_path, command, *arguments)
            self.process = subprocess.Popen(command_line, **options)
        except Exception:
            self.set_busy(False)
            self.status.set('程序未能启动，请检查软件文件是否完整。')
            return False
        self.running_command = command
        process = self.process
        def collect():
            output, error = process.communicate()
            self.events.put((command, process.returncode, output, error))
        threading.Thread(target=collect, daemon=True).start()
        return True

    def update_running(self):
        pass

    def on_result(self, command, code, output, error):
        self.status.set(output.strip() or error.strip() or '操作已结束。')

    def poll(self):
        if self.closed:
            return
        self.update_running()
        try:
            command, code, output, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.process = None
            self.running_command = None
            self.set_busy(False)
            self.on_result(command, code, output, error)
        self.timer = self.window.after(200, self.poll)

    def close(self):
        if self.busy:
            return False
        if not self.closed:
            self.closed = True
            self.window.after_cancel(self.timer)
        return True
