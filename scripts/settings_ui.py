"""Account/login and mail settings share one desktop settings page."""
import tkinter as tk
from tkinter import ttk

from configuration import read_config, write_config, CONFIG_PATH, ConfigError
from desktop_worker import WorkerPage, ScrollableFrame, json_result
from login import LoginApp
from poll_settings import MailSettings, SettingsError


class MailSettingsPage(WorkerPage):
    def __init__(self, window, parent, **kwargs):
        super().__init__(window, parent, **kwargs)
        self.variables = {}
        self.inputs = []
        self.more = tk.BooleanVar(value=False)
        outer = ttk.Frame(parent, padding=16)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='邮箱通知', font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='抢到后自动发送成功通知，断网时保留并重试。').pack(anchor='w', pady=(6, 14))
        basic = ttk.Frame(outer)
        basic.pack(fill='x'); basic.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate([('sender', '发件邮箱'), ('password', '邮箱授权码'), ('recipient', '收件邮箱')]):
            self.entry(basic, row, key, label, '', secret=key == 'password')
        ttk.Label(outer, text='授权码从邮箱的SMTP服务设置中获取。QQ邮箱通常只需填写上面三项。',
            wraplength=800, foreground='#666666').pack(anchor='w', pady=8)
        toggle = ttk.Checkbutton(outer, text='高级邮件设置', variable=self.more, command=self.toggle_advanced)
        toggle.pack(anchor='w', pady=4); self.inputs.append(toggle)
        self.advanced = ttk.LabelFrame(outer, text='服务器和重试', padding=10)
        self.advanced.columnconfigure(1, weight=1)
        for row, (key, label, default) in enumerate([
            ('smtp_host', 'SMTP服务器', 'smtp.qq.com'), ('port', '端口', '465'), ('security', '加密方式：ssl / starttls', 'ssl'),
            ('username', 'SMTP用户名', ''), ('timeout_seconds', '连接等待秒数', '15'),
            ('retry_interval_seconds', '邮件重试间隔（秒）', '60'), ('max_attempts', '自动尝试次数', '3')]):
            self.entry(self.advanced, row, key, label, default)
        controls = self.controls = ttk.Frame(outer)
        controls.pack(fill='x', pady=14)
        self.buttons = []
        for label, action in [('保存邮箱设置', self.save), ('发送测试邮件', lambda: self.command('test-mail')),
                              ('重试未发出的邮件', lambda: self.command('retry-mail'))]:
            button = ttk.Button(controls, text=label, command=action)
            button.pack(side='left', padx=(0, 10)); self.buttons.append(button)
        ttk.Label(outer, textvariable=self.status, wraplength=800, foreground='#215c4b').pack(anchor='w', pady=8)
        self.load_form()
        self.last_sender = self.variables['sender'].get()
        self.variables['sender'].trace_add('write', self.sender_changed)

    def entry(self, frame, row, key, label, default, secret=False):
        self.variables[key] = tk.StringVar(value=default)
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 20), pady=5)
        entry = ttk.Entry(frame, textvariable=self.variables[key], show='●' if secret else '')
        entry.grid(row=row, column=1, sticky='ew', pady=5)
        self.inputs.append(entry)

    def load_form(self):
        try:
            values = read_config(self.config_path).get('mail', {})
            for key, value in values.items():
                if key in self.variables: self.variables[key].set(str(value))
            if not self.variables['username'].get(): self.variables['username'].set(self.variables['sender'].get())
            self.status.set('填写后保存，可发送测试邮件确认收件。')
        except ConfigError: self.status.set('邮箱配置无法读取，请检查本地配置格式。')

    def sender_changed(self, *args):
        if self.variables['username'].get() in ('', self.last_sender):
            self.variables['username'].set(self.variables['sender'].get())
        self.last_sender = self.variables['sender'].get()

    def toggle_advanced(self):
        if self.more.get(): self.advanced.pack(fill='x', before=self.controls, pady=5)
        else: self.advanced.pack_forget()

    def save(self):
        if self.busy or self.closed or not self.activity_guard(): return False
        try:
            data = read_config(self.config_path)
            values = {key: variable.get().strip() for key, variable in self.variables.items() if key != 'password'}
            values['password'] = self.variables['password'].get()
            if not values['sender']: raise SettingsError('请填写发件邮箱。')
            if not values['recipient']: raise SettingsError('请填写收件邮箱。')
            for key in ('port', 'timeout_seconds', 'retry_interval_seconds', 'max_attempts'): values[key] = int(values[key])
            data['mail'] = {**data.get('mail', {}), **values}
            MailSettings.read(data['mail'])
            write_config(data, self.config_path)
            self.status.set('邮箱设置已保存；已核实的成功通知由后台自动发送。')
            return True
        except SettingsError as error: self.status.set(str(error))
        except (ConfigError, ValueError, TypeError): self.status.set('邮箱设置未保存，请检查填写内容。')
        return False

    def command(self, command):
        if self.save() and self.launch('poller', command): self.status.set('正在发送测试邮件……' if command == 'test-mail' else '正在重试成功通知……')

    def set_busy(self, busy):
        super().set_busy(busy)
        for widget in [*self.inputs, *self.buttons]: widget.configure(state='disabled' if busy else 'normal')

    def on_result(self, command, code, output, error):
        if code:
            self.status.set(error.strip() or '邮件操作未完成，请检查邮箱设置。')
        elif command == 'test-mail': self.status.set('测试邮件已被服务器接受，请检查收件箱和垃圾邮件。')
        else:
            try:
                counts = json_result(output)
                self.status.set(f"已被服务器接受{counts.get('sent', 0)}封；待发送或重试{counts.get('queued', 0) + counts.get('retryable', 0)}封；失败{counts.get('failed', 0)}封。详细状态见“记录”。")
            except (ValueError, TypeError, AttributeError): self.status.set('邮件处理已结束，请在“记录”查看状态。')


class SettingsPage:
    def __init__(self, window, parent, config_loader, config_path=CONFIG_PATH, start_workers=True, activity_guard=None):
        self.window = window
        self.closed = False
        outer = ttk.Frame(parent, padding=20); outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='设置', font=('Microsoft YaHei UI', 19, 'bold')).pack(anchor='w', pady=(0, 12))
        self.tabs = ttk.Notebook(outer); self.tabs.pack(fill='both', expand=True)
        account = ScrollableFrame(self.tabs); mail = ScrollableFrame(self.tabs)
        self.tabs.add(account, text='账号与登录'); self.tabs.add(mail, text='邮箱通知')
        self.login = LoginApp(config_loader(), auto_open=False, start_worker=start_workers, window=window,
            parent=account.content, activity_guard=activity_guard, config_path=config_path, compact=True,
            reveal_captcha=account.reveal)
        self.mail = MailSettingsPage(window, mail.content, config_path=config_path, activity_guard=activity_guard)

    @property
    def pending(self): return self.login.pending

    @property
    def busy(self): return self.mail.busy

    @property
    def status(self):
        return self.login.status if self.login.pending or self.tabs.index(self.tabs.select()) == 0 and not self.mail.busy else self.mail.status

    def close(self):
        if self.pending or self.busy: return False
        if not self.closed:
            login_closed = self.login.close()
            mail_closed = self.mail.close()
            if not login_closed or not mail_closed: return False
            self.closed = True
        return True
