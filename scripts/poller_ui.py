"""Local configuration and daily-worker controls; credentials stay on this computer."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import tomllib
from datetime import timedelta

from booking_flow import BookingError
from login_flow import atomic_json
from poll_runner import STATUS, STOP
from poll_settings import SETTINGS_PATH, PollSettings, WaitlistSettings, beijing_now, load_settings, load_mail_settings, load_waitlist_settings
from booking_options import GYM_TIME_SLOTS, date_label, date_value, local_dates, period_choices
from selection_widgets import TimeSlotPicker
from runtime import worker_command
from session import ROOT
from configuration import read_config, write_config, ConfigError


class PollerApp:
    def __init__(self, window, autostart=False, initial_tab=None,parent=None,activity_guard=None):
        self.window=window
        self.embedded=parent is not None
        self.closed=False
        self.activity_guard=activity_guard or (lambda:True)
        self.process=None
        self.busy=False
        self.closing=False
        self.events=queue.Queue()
        self.variables={}
        self.inputs=[]
        self.buttons=[]
        self.extra_buttons=[]
        self.waitlist_vars={}
        self.readonly_inputs=[]
        self.running_command=None
        if not self.embedded:
            window.title('交大健身房 · 预约轮询')
            window.geometry('870x790')
            window.minsize(810,740)
        outer=self.outer=ttk.Frame(parent if parent is not None else window,padding=18)
        outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='每日开抢，或等待退选名额',font=('Microsoft YaHei UI',18,'bold')).pack(anchor='w')
        ttk.Label(outer,text='每日预约与退选候补分开设置；成功后停止查询并提醒付款，不自动付款。').pack(anchor='w',pady=(6,12))
        tabs=self.tabs=ttk.Notebook(outer)
        tabs.pack(fill='x')
        booking=ttk.Frame(tabs,padding=12)
        frequency=ttk.Frame(tabs,padding=12)
        mail=ttk.Frame(tabs,padding=12)
        waitlist=ttk.Frame(tabs,padding=12)
        tabs.add(booking,text='预约计划')
        tabs.add(waitlist,text='退选候补')
        tabs.add(frequency,text='轮询频次')
        tabs.add(mail,text='邮件提醒')
        self.waitlist_tab=waitlist
        self.booking_tab=booking
        self.frequency_tab=frequency
        self.mail_tab=mail
        self.build_waitlist(waitlist)
        if initial_tab=='waitlist': tabs.select(waitlist)
        self.enabled=tk.BooleanVar(value=False)
        enabled=ttk.Checkbutton(booking,text='启用每日预约（保存后还需点击启动）',variable=self.enabled)
        enabled.grid(row=0,column=0,columnspan=2,sticky='w',pady=5)
        self.inputs.append(enabled)
        ttk.Label(booking,text='预约日期').grid(row=1,column=0,sticky='nw',padx=(0,18),pady=5)
        self.date_preview=tk.StringVar()
        ttk.Label(booking,textvariable=self.date_preview,wraplength=540).grid(row=1,column=1,sticky='w',pady=5)
        ttk.Label(booking,text='候选时段（按优先顺序）').grid(row=2,column=0,sticky='nw',padx=(0,18),pady=5)
        self.time_picker=TimeSlotPicker(booking)
        self.time_picker.grid(row=2,column=1,sticky='ew',pady=5)
        self.entry(booking,3,'field','指定场地，可留空','')
        for row,(key,label,default) in enumerate([
            ('interval_seconds','两次余量检查间隔（秒，20～600）','60'),
            ('window_minutes','12:00后检查多少分钟（1～60）','15'),
            ('max_checks','每天最多检查次数（1～60）','15'),
            ('manual_timeout_seconds','手动验证码等待时间（秒，0～300）','300'),
        ]): self.entry(frequency,row,key,label,default)
        ttk.Label(frequency,text='达到检查次数或时间上限后，结束当天预约。',foreground='#666666').grid(row=4,column=0,columnspan=2,sticky='w',pady=8)
        for row,(key,label,default) in enumerate([
            ('smtp_host','SMTP服务器',''),('port','端口','465'),
            ('security','连接加密：ssl 或 starttls','ssl'),
            ('username','SMTP用户名',''),('password','SMTP授权码（本地保存）',''),
            ('sender','发件邮箱',''),('recipient','收件邮箱',''),
        ]): self.entry(mail,row,key,label,default,secret=key=='password')
        ttk.Label(mail,text='账号、邮件与预约设置统一保存到本机config/config.toml。',foreground='#666666').grid(row=7,column=0,columnspan=2,sticky='w',pady=8)
        self.status=tk.StringVar(value='尚未启动。选择每日预约或退选候补，保存配置后点击对应的启动按钮。')
        controls=ttk.Frame(outer)
        controls.pack(fill='x',pady=12)
        actions=[('保存配置',self.save_form),('发送测试邮件',lambda:self.command('test-mail')),
            ('启动每日预约',self.start),('停止轮询',self.stop),
            ('重试未发出的邮件',lambda:self.command('retry-mail')),('打开本地配置',lambda:os.startfile(SETTINGS_PATH))]
        for i,(label,callback) in enumerate(actions):
            button=ttk.Button(controls,text=label,command=callback)
            button.grid(row=i//3,column=i%3,sticky='ew',padx=3,pady=4)
            controls.columnconfigure(i%3,weight=1)
            self.buttons.append(button)
        ttk.Label(outer,textvariable=self.status,wraplength=820,font=('Microsoft YaHei UI',10)).pack(anchor='w',pady=(0,8))
        self.text=tk.Text(outer,wrap='word',font=('Microsoft YaHei UI',10),height=8)
        self.text.pack(fill='both',expand=True)
        self.show('程序需保持运行，电脑需开机联网。休眠或关机期间无法预约。\n\n邮件授权码不会显示在运行记录中。验证码失败时会打开Chrome供你手动处理；登录失败、被限频或下单结果不明时停止任务。\n\n成功后请尽快在体育平台个人订单付款，期限以平台显示为准。')
        self.read_form()
        self.update_date_preview()
        if not self.embedded: window.protocol('WM_DELETE_WINDOW',self.close)
        self.timer=window.after(300,self.poll)
        if autostart: window.after(500,self.start_saved)

    def build_waitlist(self, frame):
        current=beijing_now()
        def choice(row,key,label,values,default,parent=None,column=1,width=26):
            parent=parent or frame
            variable=tk.StringVar(value=default);self.waitlist_vars[key]=variable
            if parent is frame:
                ttk.Label(frame,text=label).grid(row=row,column=0,sticky='w',padx=(0,18),pady=5)
            widget=ttk.Combobox(parent,textvariable=variable,values=values,state='readonly',width=width)
            widget.grid(row=row,column=column,sticky='ew',pady=5,padx=(0,5))
            self.inputs.append(widget);self.readonly_inputs.append(widget)
            return widget
        dates=local_dates(current.date())
        self.waitlist_date_choice=choice(0,'date','预约日期',dates,dates[0])
        period_frame=ttk.Frame(frame);period_frame.grid(row=1,column=1,sticky='ew')
        ttk.Label(frame,text='预约时长和时段').grid(row=1,column=0,sticky='w',pady=5)
        self.waitlist_duration_choice=choice(0,'duration','',('1小时','连续2小时'),'1小时',period_frame,0,10)
        self.waitlist_time_choice=choice(0,'time','',GYM_TIME_SLOTS,'18:00-19:00',period_frame,1,18)
        self.waitlist_hours=GYM_TIME_SLOTS
        self.waitlist_duration_choice.bind('<<ComboboxSelected>>',self.waitlist_period_choices)
        self.waitlist_mode_choice=choice(2,'start_mode','开始方式',('立即开始','定时开始'),'立即开始')
        self.waitlist_mode_choice.bind('<<ComboboxSelected>>',lambda event:self.update_waitlist_controls())
        schedule=ttk.Frame(frame);schedule.grid(row=3,column=1,sticky='ew')
        ttk.Label(frame,text='开始时间（北京时间）').grid(row=3,column=0,sticky='w',pady=5)
        self.waitlist_schedule_date=choice(0,'start_date','',dates,dates[0],schedule,0,21)
        self.waitlist_schedule_hour=choice(0,'start_hour','',tuple(f'{h:02d}' for h in range(24)),f'{current.hour:02d}',schedule,1,3)
        ttk.Label(schedule,text=':').grid(row=0,column=2)
        self.waitlist_schedule_minute=choice(0,'start_minute','',tuple(f'{m:02d}' for m in range(60)),f'{current.minute:02d}',schedule,3,3)
        for row,key,label,default in [(4,'interval_seconds','余量检查间隔（秒，20～600）','60'),(5,'field','指定场地，可留空','')]:
            variable=tk.StringVar(value=default);self.waitlist_vars[key]=variable
            ttk.Label(frame,text=label).grid(row=row,column=0,sticky='w',padx=(0,18),pady=5)
            widget=ttk.Entry(frame,textvariable=variable);widget.grid(row=row,column=1,sticky='ew',pady=5)
            self.inputs.append(widget)
        self.waitlist_hint=tk.StringVar(value='本地日期和时段选项；可同步网站当前开放选项。')
        ttk.Label(frame,textvariable=self.waitlist_hint,foreground='#666666',wraplength=720).grid(row=6,column=0,columnspan=2,sticky='w',pady=5)
        controls=ttk.Frame(frame);controls.grid(row=7,column=0,columnspan=2,sticky='w',pady=5)
        for label,callback in [('同步网站选项',lambda:self.launch('options')),('启动退选候补',self.start_waitlist)]:
            button=ttk.Button(controls,text=label,command=callback);button.pack(side='left',padx=(0,10));self.extra_buttons.append(button)
        ttk.Label(frame,text='持续查询至成功或目标时段开始；两小时必须同一场地同时有余量。',foreground='#666666').grid(row=8,column=0,columnspan=2,sticky='w')
        frame.columnconfigure(1,weight=1)

    def waitlist_period_choices(self, event=None):
        choices=period_choices(self.waitlist_hours,2 if self.waitlist_vars['duration'].get()=='连续2小时' else 1)
        previous=self.waitlist_vars['time'].get()
        self.waitlist_time_choice.configure(values=choices)
        if previous not in choices:
            self.waitlist_vars['time'].set(next((period for period in choices if period[:5]==previous[:5]),choices[0] if choices else ''))

    def update_waitlist_controls(self):
        scheduled=self.waitlist_vars['start_mode'].get()=='定时开始'
        for widget in (self.waitlist_schedule_date,self.waitlist_schedule_hour,self.waitlist_schedule_minute):
            widget.configure(state='readonly' if scheduled and not self.busy else 'disabled')

    def waitlist_values(self):
        values={key:variable.get().strip() for key,variable in self.waitlist_vars.items()}
        return {'date':date_value(values['date']),'time':values['time'],'field':values['field'],
            'start_mode':'scheduled' if values['start_mode']=='定时开始' else 'now',
            'start_at':date_value(values['start_date'])+'T'+values['start_hour']+':'+values['start_minute']+':00+08:00',
            'interval_seconds':int(values['interval_seconds']),
            'manual_timeout_seconds':int(self.variables['manual_timeout_seconds'].get())}

    def start_waitlist(self):
        try:
            WaitlistSettings.read({'waitlist':self.waitlist_values()}).validate_start(beijing_now())
        except (BookingError,ValueError,TypeError) as error:
            self.status.set(str(error) if isinstance(error,BookingError) else '请检查候补日期、时段和查询间隔。');return
        if not self.save_form(): return
        try: load_waitlist_settings(SETTINGS_PATH)
        except BookingError as error:
            self.status.set(str(error));return
        self.launch('waitlist')

    def entry(self,frame,row,key,label,default,secret=False):
        variable=tk.StringVar(value=default)
        self.variables[key]=variable
        ttk.Label(frame,text=label).grid(row=row,column=0,sticky='w',padx=(0,18),pady=5)
        entry=ttk.Entry(frame,textvariable=variable,show='●' if secret else '')
        entry.grid(row=row,column=1,sticky='ew',pady=5)
        frame.columnconfigure(1,weight=1)
        self.inputs.append(entry)

    def read_form(self):
        try:
            data=read_config(SETTINGS_PATH)
            self.enabled.set(data.get('polling',{}).get('enabled',False))
            self.time_picker.set(data.get('booking',{}).get('time_slots',[]))
            for section in ('polling','booking','mail'):
                for key,value in data.get(section,{}).items():
                    if key in self.variables:
                        self.variables[key].set(str(value))
            candidate=data.get('waitlist',{})
            for key in ('time','field','interval_seconds'):
                if key in candidate and (key!='time' or candidate[key]): self.waitlist_vars[key].set(str(candidate[key]))
            if candidate.get('date'):
                self.waitlist_vars['date'].set(date_label(candidate['date']))
            from booking_flow import hourly_periods
            try: hours=len(hourly_periods(candidate.get('time') or '18:00-19:00'))
            except BookingError: hours=1
            self.waitlist_vars['duration'].set('连续2小时' if hours==2 else '1小时')
            self.waitlist_vars['start_mode'].set('定时开始' if candidate.get('start_mode')=='scheduled' else '立即开始')
            if candidate.get('start_at'):
                from datetime import datetime
                from poll_settings import BEIJING
                start=datetime.fromisoformat(candidate['start_at'])
                if start.tzinfo: start=start.astimezone(BEIJING)
                self.waitlist_vars['start_date'].set(date_label(start.date().isoformat()))
                self.waitlist_vars['start_hour'].set(start.strftime('%H'))
                self.waitlist_vars['start_minute'].set(start.strftime('%M'))
        except (OSError,ValueError): self.status.set('本地配置无法读取，请检查config/config.toml。')
        self.waitlist_period_choices()
        self.update_waitlist_controls()

    def update_date_preview(self):
        try:
            minutes=int(self.variables['window_minutes'].get())
            if not 1<=minutes<=60: minutes=15
        except ValueError: minutes=15
        current=beijing_now()
        schedule=PollSettings(False,(),'',window_minutes=minutes)
        release=schedule.next_release(current)
        self.date_preview.set('自动预约开抢日的7天后\n下一次：'+date_label(schedule.target_date(release.date()))+'；'+release.strftime('%m月%d日 12:00')+'开始')

    def save_form(self):
        if self.busy or self.closed or not self.activity_guard(): return False
        try:
            data=read_config(SETTINGS_PATH)
            values={key:variable.get().strip() for key,variable in self.variables.items()}
            for section,keys in [('polling',('interval_seconds','window_minutes','max_checks','manual_timeout_seconds')),
                    ('booking',('field',)),('mail',('smtp_host','port','security','username','sender','recipient'))]:
                for key in keys:
                    data.setdefault(section,{})[key]=int(values[key]) if key in ('interval_seconds','window_minutes','max_checks','manual_timeout_seconds','port') else values[key]
            # Preserve the exact authorization code; never pass it to a shell or log.
            data.setdefault('mail',{})['password']=self.variables['password'].get()
            data.setdefault('polling',{})['enabled']=self.enabled.get()
            data.setdefault('booking',{})['time_slots']=self.time_picker.get()
            data['booking'].pop('max_price',None)
            data['waitlist']=self.waitlist_values()
            write_config(data,SETTINGS_PATH)
            self.status.set('配置已保存到本机。')
            return True
        except Exception:
            self.status.set('配置未保存。请检查数字和本地配置文件格式。')
            return False

    def set_busy(self,value):
        self.busy=value
        for widget in self.inputs: widget.configure(state='disabled' if value else 'normal')
        self.time_picker.set_busy(value)
        for widget in self.readonly_inputs: widget.configure(state='disabled' if value else 'readonly')
        self.update_waitlist_controls()
        for button in self.extra_buttons: button.configure(state='disabled' if value else 'normal')
        for index,button in enumerate(self.buttons): button.configure(state='normal' if index in (3,5) or not value else 'disabled')

    def launch(self,command):
        if self.busy or self.closed or not self.activity_guard(): return
        self.set_busy(True)
        settings={'cwd':ROOT,'stdin':subprocess.DEVNULL,'stdout':subprocess.PIPE,'stderr':subprocess.PIPE,'text':True,'encoding':'utf-8','errors':'replace'}
        if os.name=='nt': settings['creationflags']=subprocess.CREATE_NO_WINDOW
        try:
            arguments=worker_command('booking' if command=='options' else 'poller',command)
            if command=='options': arguments+=['--activity','健身房','--date',date_value(self.waitlist_vars['date'].get())]
            self.process=subprocess.Popen(arguments,**settings)
        except Exception:
            self.set_busy(False);self.status.set('程序未能启动，请检查程序文件或项目独立环境。');return
        process=self.process
        self.running_command=command
        def collect():
            output,error=process.communicate()
            self.events.put((command,process.returncode,output,error))
        threading.Thread(target=collect,daemon=True).start()
        self.status.set({'run':'每日任务已启动，等待12:00。','waitlist':'候补任务已启动，正在读取计划。',
            'options':'正在同步网站日期和时段，不创建订单。'}.get(command,'正在处理邮件……'))

    def start_saved(self):
        try: load_settings(SETTINGS_PATH,require_enabled=True)
        except BookingError as error:
            self.status.set(str(error));return
        self.launch('run')

    def start(self):
        if self.save_form(): self.start_saved()

    def command(self,command):
        if not self.save_form(): return
        try: load_mail_settings(SETTINGS_PATH)
        except BookingError as error:
            self.status.set(str(error));return
        self.launch(command)

    def stop(self):
        atomic_json(STOP,{'requested_at':beijing_now().isoformat()})
        self.status.set('已请求停止。正在核实的订单会先完成核实，邮件结果不明时不会自动重发。')

    def show(self,text):
        self.text.configure(state='normal');self.text.delete('1.0','end');self.text.insert('1.0',text);self.text.configure(state='disabled')

    def apply_waitlist_options(self, data):
        dates=tuple(date_label(day) for day in data['dates'] if beijing_now().date().isoformat()<=day<=
            (beijing_now().date()+timedelta(days=7)).isoformat())
        if not dates: raise ValueError('没有开放日期')
        self.waitlist_date_choice.configure(values=dates)
        if self.waitlist_vars['date'].get() not in dates: self.waitlist_vars['date'].set(dates[0])
        self.waitlist_hours=tuple(data['time_slots'])
        self.waitlist_period_choices()
        self.waitlist_hint.set('已同步网站开放选项；余量仍会在候补时持续检查。')

    def poll(self):
        self.update_date_preview()
        if self.busy and self.process and self.running_command in ('run','waitlist'):
            try:
                state=json.loads(STATUS.read_text(encoding='utf-8'))
                self.status.set(state['message'])
                order=state.get('order') or state.get('last_order')
                if order:
                    slot=order['slot']
                    mail_phase=state.get('mail_phase') or state.get('last_mail_phase','sending')
                    label={'queued':'待发送','connecting':'连接中','sending':'发送中','retryable':'等待有限重试',
                        'sent':'邮件服务器已接受','failed':'发送失败','unknown':'结果不明，不自动重发','not_needed':'订单无需付款提醒'}.get(mail_phase,mail_phase)
                    self.show(f"最近一次确认的预约\n{slot['venue_name']} · {slot['activity']}\n{slot['date']} {slot['time']}\n{slot['field']} · {order['amount_due']}元\n订单号：{order['order_id']}\n邮件状态：{label}\n\n请在体育平台个人订单核对并付款。")
                elif state.get('settings'):
                    setting=state['settings']
                    details=(f"持续候补至：{setting['expires_at']}；成功后停止。" if setting.get('mode')=='waitlist' else
                        f"最多{setting['max_checks']}次；窗口{setting['window_minutes']}分钟。")
                    self.show(f"开始时间：{setting['release_at']}\n预约日期：{setting['target_date']}\n目标时段：{', '.join(setting['time_slots'])}\n间隔：{setting['interval_seconds']}秒\n{details}")
            except (OSError,ValueError,KeyError): pass
        try: command,code,output,error=self.events.get_nowait()
        except queue.Empty: pass
        else:
            self.process=None;self.set_busy(False)
            self.running_command=None
            if command=='options' and code==0:
                try:
                    self.apply_waitlist_options(json.loads(output));self.status.set('候补日期和时段已同步。')
                except (ValueError,KeyError,TypeError): self.status.set('网站选项未能读取，请重新登录或稍后同步。')
            elif command not in ('run','waitlist') or code:
                self.status.set(output.strip() or error.strip() or '任务已停止，请检查配置和本地记录。')
            if self.closing:
                self.closed=True
                (self.outer if self.embedded else self.window).destroy();return
        self.timer=self.window.after(300,self.poll)

    def close(self):
        if self.closed: return True
        if self.busy:
            self.closing=True;self.stop()
            self.status.set('正在停止并核实当前操作，完成后窗口自动关闭。')
            return False
        self.closed=True
        self.window.after_cancel(self.timer)
        (self.outer if self.embedded else self.window).destroy()
        return True


def main(autostart=False, initial_tab=None):
    window=tk.Tk()
    PollerApp(window,autostart,initial_tab)
    window.mainloop()
