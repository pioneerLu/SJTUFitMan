"""Three desktop pages: automatic reservations, records and settings."""
import argparse
import tkinter as tk
from tkinter import ttk

from automatic_ui import AutoReservationPage
from records_ui import RecordsPage
from settings_ui import SettingsPage
from configuration import CONFIG_PATH, ConfigError, ensure_config, read_config
from notification_service import NoticeService


class FitnessApp:
    PAGES = {'auto': '自动预约', 'records': '记录', 'settings': '设置'}

    def __init__(self, window, initial_page='auto', autostart=False, start_workers=True,
                 config_loader=None, config_path=CONFIG_PATH, records_root=None):
        self.window = window
        self.components = {}
        self.frames = {}
        self.start_workers = start_workers
        self.config_path = config_path
        self.records_root = records_root
        self.config_loader = config_loader or (lambda: read_config(self.config_path))
        self.mail_service = NoticeService(config_path=config_path, enabled=start_workers)
        self.closing = False
        self.closed = False
        self.close_timer = None
        self.current_page = 'auto'
        window.title('交大健身房助手')
        height = min(820, max(660, window.winfo_screenheight() - 110))
        window.geometry(f'980x{height}'); window.minsize(860, 660)
        window.option_add('*Font', ('Microsoft YaHei UI', 10))
        header = ttk.Frame(window, padding=(20, 12)); header.pack(fill='x')
        ttk.Label(header, text='交大健身房助手', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
        ttk.Label(header, text='设置目标，持续轮询；抢到后邮件通知。', foreground='#666666').pack(anchor='w', pady=(4, 0))
        nav = ttk.Frame(window, padding=(15, 0, 15, 8)); nav.pack(fill='x')
        self.nav_buttons = {}
        for column, (page, label) in enumerate(self.PAGES.items()):
            button = ttk.Button(nav, text=label, command=lambda selected=page: self.show_page(selected))
            button.grid(row=0, column=column, sticky='ew', padx=3)
            nav.columnconfigure(column, weight=1); self.nav_buttons[page] = button
        footer = ttk.Frame(window, padding=(20, 8)); footer.pack(side='bottom', fill='x')
        self.task_status = tk.StringVar(value='就绪。保存并启动后才开始自动预约。')
        self.notice_status = tk.StringVar(value=self.mail_service.status_text())
        ttk.Label(footer, textvariable=self.task_status, wraplength=710).pack(side='left', fill='x', expand=True)
        self.stop_button = ttk.Button(footer, text='停止当前任务', command=self.stop_polling, state='disabled')
        self.stop_button.pack(side='right', padx=(12, 0))
        ttk.Label(window, textvariable=self.notice_status, wraplength=820, foreground='#666666',
                  padding=(20, 4)).pack(side='bottom', fill='x')
        self.content = ttk.Frame(window); self.content.pack(fill='both', expand=True)
        self.content.rowconfigure(0, weight=1); self.content.columnconfigure(0, weight=1)
        self.show_page('auto' if autostart else initial_page)
        window.protocol('WM_DELETE_WINDOW', self.close)
        self.timer = window.after(250, self.refresh_status)
        self.mail_service.start()
        if autostart and start_workers and 'auto' in self.components: window.after(500, self.components['auto'].start_saved)

    def show_page(self, page):
        if self.closing or page not in self.PAGES: return
        if page not in self.frames:
            parent = ttk.Frame(self.content); parent.grid(row=0, column=0, sticky='nsew')
            self.frames[page] = parent
            guard = lambda: self.allow_action(page)
            try:
                if page == 'auto':
                    component = AutoReservationPage(self.window, parent, config_path=self.config_path, activity_guard=guard)
                elif page == 'records':
                    extra = {'root': self.records_root} if self.records_root is not None else {}
                    component = RecordsPage(self.window, parent, config_path=self.config_path, activity_guard=guard, **extra)
                else:
                    component = SettingsPage(self.window, parent, self.config_loader, config_path=self.config_path,
                                             start_workers=self.start_workers, activity_guard=guard)
                self.components[page] = component
            except Exception:
                parent.destroy(); self.frames.pop(page, None)
                self.task_status.set('页面未能打开，请检查本地配置和软件文件。')
                return
        self.frames[page].tkraise(); self.current_page = page
        for name, button in self.nav_buttons.items(): button.state(['pressed'] if name == page else ['!pressed'])

    def active(self):
        for name, component in self.components.items():
            if getattr(component, 'pending', False) or getattr(component, 'busy', False): return name, component
        return None, None

    def allow_action(self, owner):
        active, component = self.active()
        if not self.closing and (active is None or active == owner): return True
        message = '正在关闭，请等待当前操作完成。' if self.closing else '当前“' + self.PAGES[active] + '”操作尚未结束，请先完成或停止当前操作。'
        self.task_status.set(message)
        if owner in self.components: self.components[owner].status.set(message)
        return False

    def stop_polling(self):
        component = self.components.get('auto')
        if component and component.busy and component.running_command in ('run', 'waitlist'): component.stop()

    def refresh_status(self):
        if self.closed: return
        self.notice_status.set(self.mail_service.status_text())
        name, component = self.active()
        if self.closing: self.task_status.set('正在停止并等待当前操作完成，随后自动关闭。')
        elif component: self.task_status.set(self.PAGES[name] + '：' + component.status.get())
        else:
            selected = self.components.get(self.current_page)
            self.task_status.set(self.PAGES[self.current_page] + '：' + selected.status.get() if selected else '就绪。')
        automatic = self.components.get('auto')
        self.stop_button.configure(state='normal' if automatic and automatic.busy and automatic.running_command in ('run', 'waitlist') else 'disabled')
        self.timer = self.window.after(250, self.refresh_status)

    def close(self):
        if self.closed: return
        if not self.closing:
            self.closing = True
            for button in self.nav_buttons.values(): button.configure(state='disabled')
            self.stop_polling()
        mail_closed = self.mail_service.close()
        components_closed = False
        if mail_closed and self.active()[0] is None:
            components_closed = all([component.close() for component in self.components.values()])
        if not components_closed:
            self.task_status.set('正在停止并等待当前操作核实，完成后自动关闭。')
            if self.close_timer is None: self.close_timer = self.window.after(200, self.finish_close)
            return
        self.closed = True
        if self.close_timer is not None: self.window.after_cancel(self.close_timer)
        self.window.after_cancel(self.timer)
        self.window.destroy()

    def finish_close(self):
        self.close_timer = None; self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--page', choices=tuple(FitnessApp.PAGES), default='auto')
    parser.add_argument('--autostart', action='store_true', help='Start an already-enabled daily plan.')
    options = parser.parse_args(argv)
    try: ensure_config()
    except ConfigError as error:
        from tkinter import messagebox
        window = tk.Tk(); window.withdraw()
        messagebox.showerror('本地配置', str(error), parent=window); window.destroy(); return
    window = tk.Tk(); FitnessApp(window, options.page, options.autostart); window.mainloop()


if __name__ == '__main__':
    from runtime import ensure_venv
    ensure_venv(); main()
