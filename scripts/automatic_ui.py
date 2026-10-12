"""One reservation form, with independent fixed-date and daily plan drafts."""
from datetime import datetime, timedelta
import json
import tkinter as tk
from tkinter import ttk

from booking_flow import BookingError
from booking_options import DEFAULT_ACTIVITIES, GYM_TIME_SLOTS, date_label, date_value, local_dates
from configuration import read_config, write_config, ConfigError
from desktop_worker import WorkerPage, ScrollableFrame, json_result
from login_flow import atomic_json
from poll_runner import STATUS, STOP
from poll_settings import BEIJING, PollSettings, WaitlistSettings, MailSettings, beijing_now, MAX_DAILY_WINDOW_MINUTES
from selection_widgets import TimeSlotPicker


class AutoReservationPage(WorkerPage):
    MODES = {'waitlist': '指定日期候补', 'daily': '每日12点开抢'}
    STOP_MODES = {'slot': '直到目标时段开始', 'duration': '持续指定时长', 'deadline': '指定截止时间'}

    def __init__(self, window, parent, **kwargs):
        super().__init__(window, parent, **kwargs)
        self.mode_key = 'waitlist'
        self.inputs = []
        self.readonly_inputs = []
        self.variables = {}
        self.drafts = {}
        self.options_by_mode = {}
        self.preview = tk.StringVar()
        self.hint = tk.StringVar(value='可刷新网站日期和时段；保存并启动后持续查询，有名额时自动预约。')
        self.more = tk.BooleanVar(value=False)
        outer = ttk.Frame(parent, padding=20)
        outer.pack(fill='both', expand=True)
        self.scroller = ScrollableFrame(outer)
        self.scroller.pack(fill='both', expand=True)
        body = self.scroller.content
        form = self.form = ttk.Frame(body)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        self.mode = tk.StringVar(value=self.MODES['waitlist'])
        ttk.Label(form, text='预约计划').grid(row=0, column=0, sticky='w', padx=(0, 20), pady=5)
        self.mode_choice = ttk.Combobox(form, textvariable=self.mode, values=tuple(self.MODES.values()), state='readonly')
        self.mode_choice.grid(row=0, column=1, sticky='ew', pady=5)
        self.mode_choice.bind('<<ComboboxSelected>>', self.change_mode)
        self.inputs.append(self.mode_choice); self.readonly_inputs.append(self.mode_choice)
        self.activity_choice = self.choice(form, 1, 'activity', '预约项目', DEFAULT_ACTIVITIES, '健身房')
        self.activity_choice.bind('<<ComboboxSelected>>', self.change_activity)
        self.date_label = ttk.Label(form, text='预约日期')
        self.date_label.grid(row=2, column=0, sticky='w', pady=5)
        self.date_area = ttk.Frame(form)
        self.date_area.grid(row=2, column=1, sticky='ew', pady=5)
        self.date_area.columnconfigure(0, weight=1)
        days = local_dates(beijing_now().date())
        self.variables['date'] = tk.StringVar(value=days[0])
        self.date_choice = ttk.Combobox(self.date_area, textvariable=self.variables['date'], values=days, state='readonly')
        self.date_choice.grid(row=0, column=0, sticky='ew')
        self.inputs.append(self.date_choice); self.readonly_inputs.append(self.date_choice)
        self.daily_date = tk.StringVar()
        self.daily_date_label = ttk.Label(self.date_area, textvariable=self.daily_date, wraplength=640)
        self.daily_date_label.grid(row=0, column=0, sticky='w')
        self.period_label = ttk.Label(form, text='预约时段')
        self.period_label.grid(row=3, column=0, sticky='nw', pady=5)
        self.time_picker = TimeSlotPicker(form, multiple=False)
        self.time_picker.listbox.configure(height=3)
        self.time_picker.grid(row=3, column=1, sticky='ew', pady=5)
        self.start_area = ttk.Frame(form)
        self.start_area.grid(row=4, column=0, columnspan=2, sticky='ew', pady=5)
        self.start_area.columnconfigure(1, weight=1)
        self.start_choice = self.choice(self.start_area, 0, 'start_mode', '开始方式', ('立即开始', '定时开始'), '立即开始')
        self.start_choice.bind('<<ComboboxSelected>>', lambda event: self.update_controls())
        self.schedule = ttk.Frame(self.start_area)
        self.schedule.grid(row=1, column=1, sticky='w', pady=5)
        self.schedule_date = self.choice(self.schedule, 0, 'start_date', '', days, days[0], column=0, width=23)
        self.schedule_hour = self.choice(self.schedule, 0, 'start_hour', '', tuple(f'{x:02d}' for x in range(24)), '12', column=1, width=5)
        self.schedule_minute = self.choice(self.schedule, 0, 'start_minute', '', tuple(f'{x:02d}' for x in range(60)), '00', column=2, width=5)
        self.interval_entry = self.entry(form, 5, 'interval_seconds', '查询间隔（秒）', '60')
        self.limit_area = ttk.Frame(form)
        self.limit_area.grid(row=6, column=0, columnspan=2, sticky='ew', pady=5)
        self.limit_area.columnconfigure(1, weight=1)
        self.stop_choice = self.choice(self.limit_area, 0, 'stop_mode', '停止条件', tuple(self.STOP_MODES.values()), self.STOP_MODES['slot'])
        self.stop_choice.bind('<<ComboboxSelected>>', self.change_stop_mode)
        self.duration_area = ttk.Frame(self.limit_area)
        self.duration_area.grid(row=1, column=1, sticky='w', pady=5)
        self.duration_hour = self.choice(self.duration_area, 0, 'duration_hours', '', tuple(f'{x:02d}' for x in range(193)), '01', column=0, width=7)
        ttk.Label(self.duration_area, text='小时').grid(row=0, column=1, padx=(0, 10))
        self.duration_minute = self.choice(self.duration_area, 0, 'duration_minutes', '', tuple(f'{x:02d}' for x in range(60)), '00', column=2, width=5)
        ttk.Label(self.duration_area, text='分钟').grid(row=0, column=3)
        self.deadline_area = ttk.Frame(self.limit_area)
        self.deadline_area.grid(row=2, column=1, sticky='w', pady=5)
        self.end_date = self.choice(self.deadline_area, 0, 'end_date', '', days, days[0], column=0, width=23)
        self.end_hour = self.choice(self.deadline_area, 0, 'end_hour', '', tuple(f'{x:02d}' for x in range(24)), '22', column=1, width=5)
        self.end_minute = self.choice(self.deadline_area, 0, 'end_minute', '', tuple(f'{x:02d}' for x in range(60)), '00', column=2, width=5)
        for widget in (self.duration_hour, self.duration_minute, self.end_date, self.end_hour, self.end_minute):
            widget.bind('<<ComboboxSelected>>', lambda event: self.update_preview())
        self.duration_hour.bind('<<ComboboxSelected>>', lambda event: self.update_controls())
        ttk.Label(form, textvariable=self.hint, wraplength=720, foreground='#666666').grid(row=7, column=0, columnspan=2, sticky='w', pady=8)
        toggle = ttk.Checkbutton(body, text='更多设置', variable=self.more, command=self.toggle_advanced)
        toggle.pack(anchor='w', pady=4); self.inputs.append(toggle)
        self.advanced = ttk.LabelFrame(body, text='预约限制与场地', padding=10)
        self.advanced.columnconfigure(1, weight=1)
        self.entry(self.advanced, 0, 'field', '指定场地（留空选可用场地）', '')
        self.entry(self.advanced, 1, 'manual_timeout_seconds', '手动验证码等待秒数', '300')
        controls = ttk.Frame(outer)
        controls.pack(fill='x', pady=(12, 8))
        self.refresh_button = ttk.Button(controls, text='刷新日期和时段', command=self.refresh_options)
        self.start_button = ttk.Button(controls, text='保存并启动', command=self.start)
        self.stop_button = ttk.Button(controls, text='停止', command=self.stop, state='disabled')
        self.buttons = [self.refresh_button, self.start_button, self.stop_button]
        for button in self.buttons: button.pack(side='left', padx=(0, 10))
        self.preview_label = ttk.Label(outer, textvariable=self.preview, wraplength=760)
        self.preview_label.pack(anchor='w', pady=6)
        self.authorization_label = ttk.Label(outer, text='点击“保存并启动”即授权创建目标订单。验证码失败转手动；结果不明时停止并提示核查。',
            wraplength=760, foreground='#666666')
        self.authorization_label.pack(anchor='w', pady=6)
        self.load_form()

    def choice(self, frame, row, key, label, choices, default, column=1, width=None):
        variable = self.variables[key] = tk.StringVar(value=default)
        if label:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 20), pady=5)
        widget = ttk.Combobox(frame, textvariable=variable, values=choices, state='readonly', **({'width': width} if width else {}))
        widget.grid(row=row, column=column, sticky='ew', padx=(0, 6), pady=5)
        self.inputs.append(widget); self.readonly_inputs.append(widget)
        return widget

    def entry(self, frame, row, key, label, default):
        variable = self.variables[key] = tk.StringVar(value=default)
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 20), pady=5)
        widget = ttk.Entry(frame, textvariable=variable)
        widget.grid(row=row, column=1, sticky='ew', pady=5)
        self.inputs.append(widget)
        return widget

    def load_form(self):
        try:
            data = read_config(self.config_path)
            current = beijing_now()
            days = local_dates(current.date())
            polling, booking, fixed = data.get('polling', {}), data.get('booking', {}), data.get('waitlist', {})
            later = current + timedelta(hours=2)
            common = {'start_mode': '立即开始', 'start_date': days[0], 'start_hour': '12', 'start_minute': '00',
                      'date': days[0], 'end_date': date_label(later.date().isoformat()),
                      'end_hour': later.strftime('%H'), 'end_minute': later.strftime('%M')}
            daily_minutes = polling.get('window_minutes', 15)
            self.drafts['daily'] = {**common, 'activity': booking.get('activity', '健身房'), 'field': booking.get('field', ''),
                'interval_seconds': str(polling.get('interval_seconds', 60)), 'manual_timeout_seconds': str(polling.get('manual_timeout_seconds', 300)),
                'periods': list(booking.get('time_slots', [])),
                'stop_mode': self.STOP_MODES.get(polling.get('stop_mode', 'duration'), self.STOP_MODES['duration']),
                'duration_hours': f'{daily_minutes // 60:02d}', 'duration_minutes': f'{daily_minutes % 60:02d}'}
            fixed_minutes = fixed.get('duration_minutes', 60)
            self.drafts['waitlist'] = {**common, 'activity': fixed.get('activity', '健身房'), 'field': fixed.get('field', ''),
                'date': date_label(fixed['date']) if fixed.get('date') else days[0],
                'interval_seconds': str(fixed.get('interval_seconds', 60)), 'manual_timeout_seconds': str(fixed.get('manual_timeout_seconds', 300)),
                'periods': [fixed.get('time') or '18:00-19:00'],
                'start_mode': '定时开始' if fixed.get('start_mode') == 'scheduled' else '立即开始',
                'stop_mode': self.STOP_MODES.get(fixed.get('stop_mode', 'slot'), self.STOP_MODES['slot']),
                'duration_hours': f'{fixed_minutes // 60:02d}', 'duration_minutes': f'{fixed_minutes % 60:02d}'}
            for mode, saved in (('daily', polling), ('waitlist', fixed)):
                if saved.get('stop_at'):
                    moment = datetime.fromisoformat(saved['stop_at'])
                    if moment.tzinfo: moment = moment.astimezone(BEIJING)
                    self.drafts[mode].update(end_date=date_label(moment.date().isoformat()),
                        end_hour=moment.strftime('%H'), end_minute=moment.strftime('%M'))
            if fixed.get('start_at'):
                moment = datetime.fromisoformat(fixed['start_at'])
                if moment.tzinfo: moment = moment.astimezone(BEIJING)
                self.drafts['waitlist'].update(start_date=date_label(moment.date().isoformat()), start_hour=moment.strftime('%H'), start_minute=moment.strftime('%M'))
            self.mode_key = data.get('ui', {}).get('reservation_mode', 'daily' if polling.get('enabled') else 'waitlist')
            if self.mode_key not in self.MODES: self.mode_key = 'waitlist'
            self.restore_draft()
            self.status.set('尚未启动。选择目标后点击“保存并启动”。')
        except (ConfigError, ValueError, TypeError, KeyError):
            self.status.set('本地计划无法读取，请检查配置格式。')

    def remember_draft(self):
        self.drafts[self.mode_key] = {**{key: value.get() for key, value in self.variables.items()}, 'periods': self.time_picker.get()}
        self.options_by_mode[self.mode_key] = tuple(self.time_picker.hour_choices)

    def restore_draft(self):
        draft = self.drafts[self.mode_key]
        self.mode.set(self.MODES[self.mode_key])
        for key, variable in self.variables.items():
            variable.set(draft.get(key, variable.get()))
        activity = self.variables['activity'].get()
        self.activity_choice.configure(values=tuple(dict.fromkeys([*DEFAULT_ACTIVITIES, activity])))
        self.time_picker.set_multiple(self.mode_key == 'daily')
        self.time_picker.hour_choices = self.options_by_mode.get(self.mode_key, GYM_TIME_SLOTS if activity == '健身房' else ())
        self.time_picker.set(draft.get('periods', []))
        self.update_controls()
        self.scroller.canvas.yview_moveto(0)

    def change_mode(self, event=None):
        if self.busy: return
        selected = next(key for key, label in self.MODES.items() if label == self.mode.get())
        self.remember_draft()
        self.mode_key = selected
        self.restore_draft()

    def change_activity(self, event=None):
        self.time_picker.set([])
        self.time_picker.set_hours(())
        self.variables['field'].set('')
        self.hint.set('项目已改变，请刷新网站时段，再选择目标。')

    def toggle_advanced(self):
        if self.more.get(): self.advanced.pack(fill='x', pady=5)
        else: self.advanced.pack_forget()

    def update_controls(self):
        daily = self.mode_key == 'daily'
        self.date_choice.grid_remove() if daily else self.date_choice.grid()
        self.daily_date_label.grid() if daily else self.daily_date_label.grid_remove()
        self.start_area.grid_remove() if daily else self.start_area.grid()
        self.period_label.configure(text='候选时段（按顺序）' if daily else '预约时段')
        scheduled = not daily and self.variables['start_mode'].get() == '定时开始'
        self.schedule.grid() if scheduled else self.schedule.grid_remove()
        for widget in (self.schedule_date, self.schedule_hour, self.schedule_minute):
            widget.configure(state='readonly' if scheduled and not self.busy else 'disabled')
        modes = ('duration', 'deadline') if daily else ('slot', 'duration', 'deadline')
        self.stop_choice.configure(values=tuple(self.STOP_MODES[key] for key in modes))
        if daily and self.variables['stop_mode'].get() == self.STOP_MODES['slot']:
            self.variables['stop_mode'].set(self.STOP_MODES['duration'])
        stop_mode = self.selected_stop_mode()
        self.duration_area.grid() if stop_mode == 'duration' else self.duration_area.grid_remove()
        self.deadline_area.grid() if stop_mode == 'deadline' else self.deadline_area.grid_remove()
        self.duration_hour.configure(values=tuple(f'{x:02d}' for x in range(25 if daily else 193)))
        maximum = '24' if daily else '192'
        self.duration_minute.configure(values=('00',) if self.variables['duration_hours'].get()==maximum else tuple(f'{x:02d}' for x in range(60)))
        if self.variables['duration_hours'].get()==maximum: self.variables['duration_minutes'].set('00')
        for widget in (self.duration_hour, self.duration_minute):
            widget.configure(state='readonly' if stop_mode == 'duration' and not self.busy else 'disabled')
        for widget in (self.end_date, self.end_hour, self.end_minute):
            widget.configure(state='readonly' if stop_mode == 'deadline' and not self.busy else 'disabled')
        self.update_preview()

    def selected_stop_mode(self):
        return next((key for key, label in self.STOP_MODES.items() if label == self.variables['stop_mode'].get()), 'slot')

    def change_stop_mode(self, event=None):
        self.update_controls()
        self.window.after_idle(lambda:self.scroller.reveal(self.limit_area))

    def limit_values(self):
        mode = self.selected_stop_mode()
        duration = int(self.variables['duration_hours'].get()) * 60 + int(self.variables['duration_minutes'].get())
        stop_at = (date_value(self.variables['end_date'].get()) + 'T' + self.variables['end_hour'].get()
            + ':' + self.variables['end_minute'].get() + ':00+08:00') if mode == 'deadline' else ''
        return mode, duration, stop_at

    def update_preview(self):
        try: stop_mode, duration, stop_at = self.limit_values()
        except (ValueError, TypeError):
            self.preview.set('请选择有效的持续时长或截止日期、时间。'); return
        length = f'{duration // 60}小时{duration % 60}分钟'
        if stop_mode=='deadline':
            try: datetime.fromisoformat(stop_at)
            except ValueError:
                self.preview.set('请选择有效的截止日期和时间。'); return
        if self.mode_key == 'daily':
            schedule = PollSettings(False, (), '', window_minutes=duration if 1 <= duration <= MAX_DAILY_WINDOW_MINUTES else 15,
                stop_mode=stop_mode, stop_at=stop_at)
            try: release = schedule.next_release(beijing_now())
            except BookingError:
                self.preview.set('请选择有效的截止日期和时间。'); return
            self.daily_date.set(date_label(schedule.target_date(release.date())) + '（随开抢日期自动更新）')
            if stop_mode == 'deadline':
                end = datetime.fromisoformat(stop_at)
                text = '每天12:00开抢，整体截止：' + end.strftime('%Y-%m-%d %H:%M') + '；到期后不再启动下一天任务。'
            else:
                text = '每天12:00起最多持续' + length + '；当天成功后停止，次日继续。'
        else:
            if stop_mode == 'duration': text = '从计划开始时间起最多持续' + length + '，登录耗时计入。'
            elif stop_mode == 'deadline': text = '候补截止：' + datetime.fromisoformat(stop_at).strftime('%Y-%m-%d %H:%M') + '。'
            else: text = '持续候补到目标时段开始。'
            text += '抢到即停；目标时段更早开始时提前结束。连续两小时一起预约。'
        self.preview.set(text + '\n场地：' + (self.variables['field'].get() or '任一符合条件的场地'))

    def plan_config(self):
        data = read_config(self.config_path)
        values = {key: variable.get().strip() for key, variable in self.variables.items()}
        if not self.time_picker.get():
            raise BookingError('请选择预约时段；每日计划需至少添加一个候选。')
        stop_mode, duration, stop_at = self.limit_values()
        if stop_mode=='duration' and duration<1: raise BookingError('持续时长至少选择1分钟。')
        if self.mode_key == 'daily':
            data['booking'] = {**data.get('booking', {}), 'activity': values['activity'], 'field': values['field'], 'time_slots': self.time_picker.get()}
            data['booking'].pop('max_price', None)
            data['polling'] = {**data.get('polling', {}), 'enabled': True,
                **{key: int(values[key]) for key in ('interval_seconds', 'manual_timeout_seconds')},
                'window_minutes': duration if stop_mode=='duration' else 15,
                'max_checks': 0, 'stop_mode': stop_mode, 'stop_at': stop_at}
            PollSettings.read(data).validate_start(beijing_now())
        else:
            periods = self.time_picker.get()
            data['waitlist'] = {**data.get('waitlist', {}), 'activity': values['activity'], 'date': date_value(values['date']),
                'time': periods[0] if periods else '', 'field': values['field'],
                'start_mode': 'scheduled' if values['start_mode'] == '定时开始' else 'now',
                'start_at': date_value(values['start_date']) + 'T' + values['start_hour'] + ':' + values['start_minute'] + ':00+08:00',
                'interval_seconds': int(values['interval_seconds']), 'manual_timeout_seconds': int(values['manual_timeout_seconds']),
                'stop_mode': stop_mode, 'duration_minutes': duration if stop_mode=='duration' else 60, 'stop_at': stop_at}
            WaitlistSettings.read(data).validate_start(beijing_now())
        MailSettings.read(data.get('mail', {}))
        data.setdefault('ui', {})['reservation_mode'] = self.mode_key
        return data

    def start(self):
        if self.busy or self.closed or not self.activity_guard(): return
        try:
            data = self.plan_config()
            write_config(data, self.config_path)
            self.remember_draft()
        except BookingError as error:
            message = str(error)
            for key, label in [('interval_seconds', '查询间隔'), ('window_minutes', '每日持续分钟数'),
                ('duration_minutes', '持续分钟数'), ('manual_timeout_seconds', '手动验证码等待时间')]:
                message = message.replace(key, label)
            self.status.set(message); return
        except (ValueError, TypeError, ConfigError):
            self.status.set('计划未保存。请检查日期、时段及查询间隔。'); return
        command = 'run' if self.mode_key == 'daily' else 'waitlist'
        if self.launch('poller', command): self.status.set('计划已保存，自动预约正在启动。')

    def start_saved(self):
        from poll_settings import load_settings
        try: load_settings(self.config_path, require_enabled=True)
        except BookingError as error:
            self.status.set(str(error)); return
        self.remember_draft(); self.mode_key = 'daily'; self.restore_draft()
        if self.launch('poller', 'run'): self.status.set('已启动保存的每日计划，等待开抢。')

    def refresh_options(self):
        extra = ['--activity', self.variables['activity'].get()]
        if self.mode_key == 'waitlist':
            try: day = date_value(self.variables['date'].get())
            except ValueError:
                self.status.set('请从选项中选择有效日期。'); return
            if day >= beijing_now().date().isoformat(): extra += ['--date', day]
        if self.launch('booking', 'options', *extra): self.status.set('正在刷新网站日期和时段，不创建订单。')

    def apply_options(self, data):
        if data.get('activity') != self.variables['activity'].get(): raise ValueError('项目不一致')
        self.activity_choice.configure(values=tuple(dict.fromkeys([*(data.get('activities') or DEFAULT_ACTIVITIES), data['activity']])))
        removed = self.time_picker.set_hours(data['time_slots'])
        self.options_by_mode[self.mode_key] = tuple(self.time_picker.hour_choices)
        self.date_choice.configure(values=local_dates(beijing_now().date()))
        message = '网站时段已更新，具体余量会在运行中查询。'
        if self.mode_key == 'waitlist' and date_value(self.variables['date'].get()) not in data['dates']:
            message += '所选日期当前尚未开放，目标日期已保留。'
        if removed: message += '旧候选不在当前时段表内，请重新选择。'
        self.hint.set(message)

    def set_busy(self, busy):
        super().set_busy(busy)
        for widget in self.inputs: widget.configure(state='disabled' if busy else 'normal')
        for widget in self.readonly_inputs: widget.configure(state='disabled' if busy else 'readonly')
        self.time_picker.set_busy(busy)
        for button in (self.refresh_button, self.start_button): button.configure(state='disabled' if busy else 'normal')
        self.stop_button.configure(state='normal' if busy and self.running_command in ('run', 'waitlist') else 'disabled')
        self.update_controls()

    def stop(self):
        if self.running_command not in ('run', 'waitlist'): return
        atomic_json(STOP, {'requested_at': beijing_now().isoformat()})
        self.status.set('已请求停止，正在核实的订单会先完成核实；成功通知仍由后台处理。')

    def update_running(self):
        self.update_preview()
        if self.busy and self.running_command in ('run', 'waitlist'):
            self.stop_button.configure(state='normal')
            try:
                state = json.loads(STATUS.read_text(encoding='utf-8'))
                self.status.set(state['message'])
            except (OSError, ValueError, KeyError): pass

    def on_result(self, command, code, output, error):
        if command == 'options' and code == 0:
            try:
                self.apply_options(json_result(output)); self.status.set('网站选项已刷新。')
            except (ValueError, TypeError, KeyError): self.status.set('网站选项无法读取，请重新登录或稍后再试。')
        elif code:
            self.status.set(error.strip() or '任务已停止，请在“记录”中核查订单，在“设置”中检查登录和邮箱。')
        else: self.status.set('本次自动预约已结束，结果与邮件状态见“记录”。')
