"""Local buttons around the deterministic booking executable."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from runtime import ROOT, worker_command
from booking_options import GYM_TIME_SLOTS, date_label, date_value, local_dates, period_choices
from poll_settings import beijing_now
from configuration import CONFIG_PATH



class BookingApp:
    def __init__(self, window, parent=None, activity_guard=None):
        self.window = window
        self.embedded=parent is not None
        self.closed=False
        self.activity_guard=activity_guard or (lambda:True)
        if not self.embedded:
            window.title('交大健身房 · 预约助手')
            window.geometry('820x700')
            window.minsize(740, 620)
        self.events = queue.Queue()
        self.busy = False
        self.prepared = None
        self.review = None
        self.buttons = []
        self.inputs = []
        self.activity = tk.StringVar(value='健身房')
        dates = local_dates(beijing_now().date())
        self.day = tk.StringVar(value=dates[-1])
        self.duration = tk.StringVar(value='1小时')
        self.hour_choices = GYM_TIME_SLOTS
        self.period = tk.StringVar(value='18:00-19:00')
        self.field = tk.StringVar()
        self.status = tk.StringVar(value='复用本机登录会话，滑块自动识别失败后可手动处理。')

        outer = self.outer = ttk.Frame(parent if parent is not None else window, padding=20)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='交大体育预约', font=('Microsoft YaHei UI', 19, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='选好日期和时段后，点击“确认真实预约”，阅读须知并勾选后提交。').pack(anchor='w', pady=(8, 18))
        form = ttk.Frame(outer)
        form.pack(fill='x')
        entries = [('项目', self.activity), ('日期', self.day), ('预约时长', self.duration), ('时段', self.period),
                   ('场地（可留空）', self.field)]
        for index, (label, variable) in enumerate(entries):
            ttk.Label(form, text=label).grid(row=index, column=0, sticky='w', padx=(0, 20), pady=5)
            if label == '项目':
                widget = ttk.Combobox(form, textvariable=variable, values=('健身房','台球','舞蹈','排练厅','钢琴','交谊厅'), state='readonly')
                widget.bind('<<ComboboxSelected>>', self.change_activity)
            elif label == '日期':
                widget = self.date_choice = ttk.Combobox(form, textvariable=variable, values=dates, state='readonly')
                widget.bind('<<ComboboxSelected>>', self.change_selection)
            elif label == '时段':
                widget = self.time_choice = ttk.Combobox(form, textvariable=variable, values=GYM_TIME_SLOTS, state='readonly')
                widget.bind('<<ComboboxSelected>>', self.change_selection)
            elif label == '预约时长':
                widget = ttk.Combobox(form, textvariable=variable, values=('1小时', '连续2小时'), state='readonly')
                widget.bind('<<ComboboxSelected>>', self.change_duration)
            else:
                widget = ttk.Entry(form, textvariable=variable)
                widget.bind('<KeyRelease>', self.change_selection)
            widget.grid(row=index, column=1, sticky='ew', pady=5)
            self.inputs.append(widget)
        form.columnconfigure(1, weight=1)
        self.options_status = tk.StringVar(value='本地日期选项；点击“同步网站选项”获取当前开放日期。')
        ttk.Label(form, textvariable=self.options_status, foreground='#666666', wraplength=560).grid(row=5, column=1, sticky='w', pady=(3, 12))

        controls = ttk.Frame(outer)
        controls.pack(fill='x', pady=(4, 12))
        for index, (text, callback) in enumerate([
            ('同步网站选项', self.sync_options),
            ('检查预约余量', lambda: self.run('inspect', self.selection())),
            ('查看预订须知', lambda: self.run('prepare', self.selection())),
            ('确认真实预约', self.confirm),
            ('运行台球模拟', lambda: self.run('simulate', [])),
            ('核查上次订单', lambda: self.run('reconcile', [])),
        ]):
            button = ttk.Button(controls, text=text, command=callback)
            button.grid(row=index // 3, column=index % 3, sticky='ew', padx=3, pady=4)
            controls.columnconfigure(index % 3, weight=1)
            self.buttons.append(button)
        ttk.Label(outer, textvariable=self.status, wraplength=760).pack(anchor='w', pady=(0, 12))
        area = ttk.Frame(outer)
        area.pack(fill='both', expand=True)
        self.text = tk.Text(area, wrap='word', font=('Microsoft YaHei UI', 10), relief='solid', borderwidth=1)
        scroll = ttk.Scrollbar(area, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.text.pack(side='left', fill='both', expand=True)
        self.show('台球模拟会自动选取明天 12:00–13:00的可选场地。最终下单请求被拦截，不占用真实场地。\n\n真实预约先查看须知并确认。滑块会在本机自动处理，失败时保留Chrome窗口，最多等待5分钟供你手动完成。程序只创建订单，不操作付款。\n\n在统一助手中切换“登录与会话”可重新登录；“每日开抢”和“退选候补”可启动轮询，两种任务共用邮件配置。')
        if not self.embedded: window.protocol('WM_DELETE_WINDOW', self.close)
        self.timer = window.after(200, self.poll)

    def show(self, text):
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.insert('1.0', text)
        self.text.configure(state='disabled')

    def selection(self):
        values = ['--activity', self.activity.get().strip(), '--date', date_value(self.day.get()),
                  '--time', self.period.get().strip()]
        if self.field.get().strip():
            values += ['--field', self.field.get().strip()]
        return values

    def change_selection(self, event=None):
        self.prepared = None

    def change_duration(self, event=None):
        self.change_selection()
        choices = period_choices(self.hour_choices, 2 if self.duration.get() == '连续2小时' else 1)
        previous = self.period.get()
        self.time_choice.configure(values=choices)
        if previous not in choices:
            matching = next((period for period in choices if period[:5] == previous[:5]), None)
            self.period.set(matching or (choices[0] if choices else ''))
        if self.duration.get() == '连续2小时':
            self.options_status.set('连续2小时需同一场地的两个小时都可预约，金额会在订单中显示。')

    def change_activity(self, event=None):
        self.change_selection()
        self.date_choice.configure(values=local_dates(beijing_now().date()))
        self.day.set(self.date_choice.cget('values')[-1])
        # A different activity's actual periods will be obtained from the website.
        self.time_choice.configure(values=())
        self.hour_choices = ()
        self.period.set('')
        self.options_status.set('项目已改变，请同步网站选项。')

    def sync_options(self):
        extra = ['--activity', self.activity.get()]
        if self.day.get() and date_value(self.day.get()) >= beijing_now().date().isoformat():
            extra += ['--date', date_value(self.day.get())]
        self.run('options', extra)

    def apply_options(self, data):
        if data.get('activity') != self.activity.get():
            return
        self.change_selection()
        dates = tuple(date_label(value) for value in data['dates'])
        self.date_choice.configure(values=dates)
        self.day.set(date_label(data['date']))
        self.hour_choices = tuple(data['time_slots'])
        self.change_duration()
        if self.duration.get() == '1小时':
            self.options_status.set('已同步网站选项；余量以下次检查为准。')

    def run(self, mode, extra):
        if self.busy or self.closed or not self.activity_guard():
            return
        if mode in ('inspect', 'prepare') and (not self.day.get() or not self.period.get()):
            self.status.set('请先同步网站选项并选择日期、时段。')
            return
        self.busy = True
        self.status.set('正在检查网站，请稍候……')
        for button in self.buttons:
            button.configure(state='disabled')
        for widget in self.inputs:
            widget.configure(state='disabled')
        if mode in ('prepare', 'options', 'inspect'):
            self.prepared = None
        def worker():
            settings = dict(cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600)
            if os.name == 'nt':
                settings['creationflags'] = subprocess.CREATE_NO_WINDOW
            try:
                command = worker_command('booking','--config',CONFIG_PATH,mode,*extra)
                result = subprocess.run(command, **settings)
                start = result.stdout.find('{')
                data = json.JSONDecoder().raw_decode(result.stdout[start:])[0] if start >= 0 else None
                self.events.put((mode, data, result.stderr.strip() if data is None else ''))
            except Exception:
                self.events.put((mode, None, '流程未完成。请先核查个人订单，再决定下一步。'))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        if self.busy:
            try:
                waiting=json.loads((ROOT/'.local/captcha/manual-status.json').read_text(encoding='utf-8'))
                if waiting.get('waiting'): self.status.set(waiting['message'])
            except (OSError,ValueError): pass
        try:
            mode, data, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            for button in self.buttons:
                button.configure(state='normal')
            for widget in self.inputs:
                widget.configure(state='readonly' if isinstance(widget, ttk.Combobox) else 'normal')
            if data is None:
                self.status.set('流程已停止。')
                self.show(error or '请检查网络与登录会话。')
            else:
                self.display_result(mode, data)
        self.timer = self.window.after(200, self.poll)

    def display_result(self, mode, data):
        if mode == 'options':
            self.apply_options(data)
            self.status.set('日期和时段已从学校预约页面同步。')
            self.show('选择预约日期和时段后，点击“检查预约余量”。\n\n网站开放日期：\n' + '\n'.join(date_label(value) for value in data['dates']) + '\n\n网站时段：\n' + '、'.join(data['time_slots']))
        elif mode == 'inspect':
            if data.get('options'):
                self.apply_options(data['options'])
            self.status.set(f"找到 {data['available']} 个符合条件的时段。")
            self.show('\n'.join(f"{x['date']} {x['time']} · {x['field']} · {x['price']}元" for x in data['slots']) or '没有符合当前条件的可选时段。')
        elif mode == 'prepare':
            self.prepared = data
            self.status.set('预约已准备，请在须知窗口勾选同意，再点击“创建此次真实订单”。')
            self.show(self.description(data['slot']) + '\n\n' + data['terms_title'] + '\n\n' + data['terms_text'])
            self.confirm()
        elif mode == 'reconcile':
            results = data['reconciliation']
            self.status.set('订单核查完成。')
            self.show('\n\n'.join(x.get('message') or (self.description(x['slot']) + '\n订单状态：' + x['status']) for x in results) or '没有待核实的本地提交记录。')
        else:
            status = {'confirmed': '已确认待付款订单', 'unknown': '结果不明，请核查个人订单',
                'verification_required': '网站要求额外验证码，请手动处理', 'rejected': '网站拒绝本次下单',
                'profile_required': '请先在体育平台更新个人信息', 'auth_required': '请重新登录'}
            label = status.get(data['phase'], '流程停止')
            if data.get('phase') == 'confirmed' and data.get('status') == '已支付':
                label = '已确认订单（已支付）'
            self.status.set(('模拟：' if data.get('simulated') else '') + label)
            text = self.description(data['slot']) + '\n\n' + label
            if data.get('order_id'):
                text += '\n订单号：' + data['order_id']
            if data.get('simulated'):
                text += '\n\n这是拦截下单请求后的模拟结果，没有真实订单。'
            elif data['phase'] == 'confirmed' and data.get('status') == '待付款':
                text += '\n\n请尽快在交大体育平台的“个人订单”付款。网站提示15分钟内付款。'
            self.show(text)

    @staticmethod
    def description(slot):
        return f"{slot['venue_name']} · {slot['activity']}\n{slot['date']} {slot['time']}\n{slot['field']} · {slot['price']}元"

    def confirm(self):
        if self.busy or self.closed or not self.activity_guard():
            return
        if self.review is not None and self.review.winfo_exists():
            self.review.deiconify()
            self.review.lift()
            self.review.focus_set()
            return
        if self.prepared is None:
            self.run('prepare', self.selection())
            return
        prepared = self.prepared
        review = self.review = tk.Toplevel(self.window)
        review.title('确认此次真实预约')
        review.geometry('680x600')
        review.minsize(560, 450)
        review.transient(self.window)
        review.grab_set()
        outer = ttk.Frame(review, padding=18)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(outer, text=self.description(prepared['slot']), font=('Microsoft YaHei UI', 12, 'bold'), wraplength=620).grid(row=0, column=0, sticky='ew', pady=(0, 12))
        body = ttk.Frame(outer)
        body.grid(row=1, column=0, sticky='nsew')
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        text = tk.Text(body, wrap='word', font=('Microsoft YaHei UI', 10), height=12)
        scroll = ttk.Scrollbar(body, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        text.insert('1.0', prepared['terms_title'] + '\n\n' + prepared['terms_text'])
        text.configure(state='disabled')
        accepted = self.review_accepted = tk.BooleanVar(value=False)
        footer = ttk.Frame(outer)
        footer.grid(row=2, column=0, sticky='ew', pady=(12, 0))
        actions = ttk.Frame(footer)
        button = self.review_submit = ttk.Button(actions, text='创建此次真实订单', state='disabled')
        self.review_checkbox = ttk.Checkbutton(footer, text='我已核对预约信息，并阅读、接受以上预订须知', variable=accepted,
            command=lambda: button.configure(state='normal' if accepted.get() else 'disabled'))
        self.review_checkbox.pack(anchor='w', pady=(0, 8))
        ttk.Label(footer, text='勾选后点击“创建此次真实订单”。创建成功后，请在平台个人订单自行付款。', wraplength=520).pack(anchor='w', pady=(0, 12))
        actions.pack(fill='x')
        def dismiss():
            if review.grab_current() is review:
                review.grab_release()
            review.destroy()
            self.review = None
        review.protocol('WM_DELETE_WINDOW', dismiss)
        ttk.Button(actions, text='暂不提交', command=dismiss).pack(side='left')
        def submit():
            if not accepted.get():
                return
            if self.prepared is not prepared:
                dismiss()
                self.status.set('预约选择已变化，请重新查看须知。')
                return
            dismiss()
            self.prepared = None
            self.run('submit', ['--confirm-reservation-sha256', prepared['confirmation_sha256']])
        button.configure(command=submit)
        button.pack(side='right')

    def close(self):
        if self.closed: return True
        if self.busy:
            if not self.embedded: messagebox.showinfo('正在处理', '请等待本次操作结束后再关闭窗口。', parent=self.window)
            return False
        self.closed=True
        self.window.after_cancel(self.timer)
        if self.review is not None and self.review.winfo_exists():
            if self.review.grab_current() is self.review: self.review.grab_release()
            self.review.destroy();self.review=None
        (self.outer if self.embedded else self.window).destroy()
        return True


def main():
    window = tk.Tk()
    BookingApp(window)
    window.mainloop()


if __name__ == '__main__':
    main()
