"""Local manual/OCR login helper. All recognition stays on this computer."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
import queue
import sys
import threading
import time

from runtime import ROOT, ensure_venv
if __name__=='__main__':
    ensure_venv()

from captcha import CaptchaError, normalize_code, recognize_ocr
from captcha_image import captcha_preview
from login_flow import FlowError, LoginBrowser, atomic_json, load_config
from configuration import CONFIG_PATH, ConfigError, update_config
from browser_runtime import BrowserError, LABELS, preferred_browser, detect_browsers
from playwright_runtime import sync_playwright


def browser_worker(config, commands, events):
    def emit(kind,value):
        events.put((kind,value))
        if kind in ('status','error'):
            atomic_json(ROOT/'.local/login-status.json',{
                'updated_at':datetime.now(timezone.utc).isoformat(),'kind':kind,'message':value})
    try:
        with ExitStack() as resources:
            pw=flow=None
            try:
                while True:
                    try: action,payload=commands.get(timeout=.25)
                    except queue.Empty: action,payload=None,None
                    try:
                        if action=='quit': return
                        if action in ('open','restore','auto-open'):
                            if flow:
                                flow.close()
                                flow=None
                            if pw is None: pw=resources.enter_context(sync_playwright())
                            flow=LoginBrowser(config,emit)
                            flow.start(pw,restore=action=='restore')
                            if action=='auto-open':
                                flow.fill_credentials(**payload)
                                flow.automatic_login()
                            elif action=='open':
                                events.put(('manual-captcha',None))
                                flow.capture_captcha()
                        elif action:
                            if not flow or not flow.page or flow.page.is_closed():
                                raise FlowError('请先打开登录页。')
                            if action=='credentials': flow.fill_credentials(**payload)
                            elif action=='capture': flow.capture_captcha()
                            elif action=='refresh': flow.refresh_captcha()
                            elif action=='ocr':
                                events.put(('code',recognize_ocr(flow.capture_captcha())))
                                emit('status','识别完成，可核对、修改后手动提交。')
                            elif action=='auto':
                                if payload['username'] and payload['password']:
                                    flow.fill_credentials(**payload)
                                flow.automatic_login()
                            elif action=='submit': flow.submit_captcha(payload)
                    except (FlowError,CaptchaError,BrowserError) as error: emit('error',str(error))
                    except Exception: emit('error','操作已停止，请在当前浏览器手动处理。')
                    finally:
                        payload=None
                        if action: events.put(('done',None))
                    if flow and flow.browser and not flow.browser.is_connected():
                        flow.close()
                        flow=None
                        emit('status','浏览器已关闭，保存的会话仍在本地。')
                    if flow:
                        try: flow.tick()
                        except FlowError as error: emit('error',str(error))
                        except Exception: emit('error','会话尚未确认，请查看浏览器状态。')
            finally:
                if flow: flow.close()
    except Exception:
        emit('error','助手启动失败，请检查独立环境以及Chrome或Edge。')
        events.put(('done',None))


class LoginApp:
    def __init__(self,config,auto_open=True,start_worker=True,window=None,parent=None,activity_guard=None,config_path=CONFIG_PATH,compact=False,reveal_captcha=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk=tk
        self.root=window if window is not None else tk.Tk()
        self.embedded=parent is not None
        self.closed=False
        self.closing=False
        self.close_id=None
        self.open_id=None
        self.activity_guard=activity_guard or (lambda:True)
        if not self.embedded:
            self.root.title('交大健身房 · 本地登录助手')
            self.root.geometry('800x740')
            self.root.minsize(740,700)
        self.root.option_add('*Font',('Microsoft YaHei UI',10))
        self.config=config
        self.compact=compact
        self.reveal_captcha=reveal_captcha
        self.config_path=Path(config_path)
        self.commands,self.events=queue.Queue(),queue.Queue()
        self.buttons=[]
        self.pending=False
        self.image=None
        try: self.saved_browser_preference=preferred_browser(config)
        except BrowserError: self.saved_browser_preference='auto'
        self.browser_preference=tk.StringVar(value=LABELS[self.saved_browser_preference])
        self.username=tk.StringVar(value=config.get('auth',{}).get('username',''))
        self.password=tk.StringVar(value=config.get('auth',{}).get('password',''))
        self.code=tk.StringVar()
        self.status=tk.StringVar(value='识别完全在本机进行。失败时保留浏览器供手动登录。')
        outer=self.outer=ttk.Frame(parent if parent is not None else self.root,padding=20)
        outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='登录并保存体育平台会话',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w')
        browser_row=ttk.Frame(outer)
        browser_row.pack(fill='x',pady=(12,0))
        ttk.Label(browser_row,text='使用浏览器').pack(side='left')
        self.browser_choice=ttk.Combobox(browser_row,textvariable=self.browser_preference,
            values=tuple(LABELS.values()),state='readonly',width=27)
        self.browser_choice.pack(side='left',padx=10)
        self.browser_choice.bind('<<ComboboxSelected>>',self.save_browser)
        self.browser_hint=tk.StringVar()
        detected=detect_browsers()
        self.browser_hint.set('检测到：'+ '、'.join(choice.label for choice in detected.values()) if detected else
            '未检测到Chrome或Edge，请先安装其中一种。')
        ttk.Label(outer,textvariable=self.browser_hint,wraplength=720,foreground='#666666').pack(anchor='w',pady=(5,0))
        ttk.Label(outer,text='选择后自动保存，登录与预约共用；已打开的窗口下次启动时切换。',
            wraplength=720,foreground='#666666').pack(anchor='w',pady=(3,0))
        row=ttk.Frame(outer)
        row.pack(fill='x',pady=14)
        if compact:
            self.button(row,'自动登录',lambda:self.enqueue('auto-open',self.credentials())).pack(side='left')
            self.button(row,'浏览器手动登录',lambda:self.enqueue('open')).pack(side='left',padx=10)
        else:
            self.button(row,'打开登录页 / 重新登录',lambda:self.enqueue('open')).pack(side='left')
            self.button(row,'检查已保存会话',lambda:self.enqueue('restore')).pack(side='left',padx=10)
        credentials=ttk.LabelFrame(outer,text='本地账号密码',padding=12)
        credentials.pack(fill='x',pady=8)
        for index,(label,variable) in enumerate((('jAccount账号',self.username),('密码',self.password))):
            ttk.Label(credentials,text=label).grid(row=index,column=0,sticky='w',pady=4)
            ttk.Entry(credentials,textvariable=variable,width=45,show='●' if index else '').grid(row=index,column=1,padx=12,pady=4)
        if not compact:
            self.button(credentials,'填入浏览器',lambda:self.enqueue('credentials',self.credentials())).grid(row=2,column=0,columnspan=2,sticky='w',pady=6)
        self.button(credentials,'保存账号到本地配置',self.save_credentials).grid(row=2,column=1,sticky='e',pady=6)
        ttk.Label(credentials,text='输入只在内存中使用；点击保存才写入个人config/config.toml。').grid(row=3,column=0,columnspan=2,sticky='w')
        self.more_login=tk.BooleanVar(value=False)
        if compact:
            ttk.Checkbutton(outer,text='验证码辅助操作',variable=self.more_login,command=self.toggle_advanced).pack(anchor='w',pady=4)
        area=self.advanced_login=ttk.LabelFrame(outer,text='验证码辅助' if compact else '本地ddddocr与手动处理',padding=12)
        area.pack(fill='x',pady=8)
        row=ttk.Frame(area)
        row.pack(fill='x')
        if compact:
            self.button(row,'显示当前图片',lambda:self.enqueue('capture')).pack(side='left')
        else:
            self.button(row,'OCR自动登录',lambda:self.enqueue('auto',self.credentials())).pack(side='left')
            self.button(row,'显示当前图片',lambda:self.enqueue('capture')).pack(side='left',padx=(8,0))
        self.button(row,'只识别当前图片',lambda:self.enqueue('ocr')).pack(side='left',padx=8)
        self.button(row,'刷新图片',lambda:self.enqueue('refresh')).pack(side='left')
        self.image_label=tk.Label(area,text='打开登录页后显示完整验证码',anchor='center',background='white',borderwidth=0)
        self.image_label.pack(fill='x',pady=10)
        ttk.Label(area,text='完整原图，放大显示。刷新图片后请重新输入验证码。').pack(anchor='w',pady=(0,8))
        row=ttk.Frame(area)
        row.pack(fill='x')
        ttk.Label(row,text='手动验证码').pack(side='left')
        self.code_entry=ttk.Entry(row,textvariable=self.code,width=15)
        self.code_entry.pack(side='left',padx=10)
        self.code_entry.bind('<Return>',lambda event:self.submit())
        self.button(row,'使用此验证码登录',self.submit).pack(side='left')
        if compact:
            self.button(area,'检查保存的登录状态',lambda:self.enqueue('restore')).pack(anchor='w',pady=(8,0))
        ttk.Label(area,text='自动最多尝试2次；仅验证码错误时重试，间隔至少20秒。\n密码错误、网络异常、额外认证或识别失败时转手动。',wraplength=720).pack(anchor='w',pady=(10,0))
        self.status_label=ttk.Label(outer,textvariable=self.status,wraplength=740,foreground='#215c4b')
        self.status_label.pack(anchor='w',pady=12)
        self.summary=ttk.Label(outer,text='成功后：服务器确认 → 保存会话 → 新会话恢复验证 → 检查健身房预约页',wraplength=740)
        self.summary.pack(anchor='w')
        if compact: self.advanced_login.pack_forget()
        if not self.embedded: self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.worker=None
        if start_worker:
            self.worker=threading.Thread(target=browser_worker,args=(config,self.commands,self.events),daemon=True)
            self.worker.start()
        self.poll_id=self.root.after(150,self.poll_events)
        if auto_open: self.open_id=self.root.after(300,lambda:self.enqueue('open'))

    def credentials(self): return {'username':self.username.get(),'password':self.password.get()}

    def save_browser(self,event=None):
        if self.pending or self.closed or self.closing or not self.activity_guard():
            self.browser_preference.set(LABELS[self.saved_browser_preference]); return False
        preference=next((key for key,label in LABELS.items() if label==self.browser_preference.get()),None)
        if preference is None:
            self.browser_preference.set(LABELS[self.saved_browser_preference]); return False
        try:
            saved=update_config({'browser':{'preferred':preference}},path=self.config_path)
            self.config['browser']=dict(saved['browser'])
            self.saved_browser_preference=preference
            self.status.set('浏览器选择已保存，下次打开登录窗口或启动预约时生效。')
            return True
        except (ConfigError,TypeError,AttributeError):
            self.browser_preference.set(LABELS[self.saved_browser_preference])
            self.status.set('浏览器选择未保存，请检查本地配置文件。')
            return False

    def toggle_advanced(self):
        if self.more_login.get():
            self.advanced_login.pack(fill='x',pady=8,before=self.status_label)
            if self.reveal_captcha: self.root.after_idle(lambda:self.reveal_captcha(self.advanced_login))
        else: self.advanced_login.pack_forget()

    def save_credentials(self):
        if self.pending or self.closed or self.closing or not self.activity_guard(): return
        try:
            saved=update_config({'auth':self.credentials()},path=self.config_path)
            self.config.setdefault('auth',{}).update(saved['auth'])
            self.status.set('账号已保存到本机config/config.toml，不会上传到Git。')
        except ConfigError as error: self.status.set(str(error))

    def button(self,parent,text,command):
        from tkinter import ttk
        button=ttk.Button(parent,text=text,command=command)
        self.buttons.append(button)
        return button

    def enqueue(self,action,payload=None):
        if self.pending or self.closed or self.closing or not self.activity_guard(): return
        self.pending=True
        self.browser_choice.configure(state='disabled')
        for button in self.buttons: button.configure(state='disabled')
        self.status.set('正在处理，请稍候……')
        self.commands.put((action,payload))

    def submit(self):
        from tkinter import messagebox
        try: self.enqueue('submit',normalize_code(self.code.get()))
        except CaptchaError as error: messagebox.showerror('验证码',str(error),parent=self.root)

    def poll_events(self):
        from PIL import ImageTk
        if self.closed: return
        while True:
            try: kind,value=self.events.get_nowait()
            except queue.Empty: break
            if kind in ('status','error'): self.status.set(value)
            elif kind=='image':
                if value is None:
                    self.image=None
                    self.image_label.configure(image='',text='验证码尚未加载，请显示当前图片或刷新。')
                else:
                    self.image=ImageTk.PhotoImage(captcha_preview(value))
                    self.image_label.configure(image=self.image,text='')
                    if self.more_login.get() and self.reveal_captcha:
                        self.root.after_idle(lambda:self.reveal_captcha(self.advanced_login))
            elif kind=='manual-captcha':
                if self.compact:
                    self.more_login.set(True)
                    self.toggle_advanced()
            elif kind=='code': self.code.set(value)
            elif kind=='done':
                self.pending=False
                if not self.closing:
                    self.browser_choice.configure(state='readonly')
                    for button in self.buttons: button.configure(state='normal')
            elif kind=='inspection': self.summary.configure(text='已恢复会话并检查真实健身房预约页。')
        self.poll_id=self.root.after(150,self.poll_events)

    def close(self):
        from tkinter import messagebox
        if self.closed: return True
        if self.pending:
            if not self.embedded: messagebox.showinfo('正在处理','请等待本次操作完成后关闭。',parent=self.root)
            return False
        if not self.closing:
            self.closing=True
            self.browser_choice.configure(state='disabled')
            for button in self.buttons: button.configure(state='disabled')
            self.status.set('正在关闭登录浏览器，请稍候……')
            self.commands.put(('quit',None))
        if self.worker and self.worker.is_alive():
            if not self.embedded and self.close_id is None:
                self.close_id=self.root.after(100,self.finish_close)
            return False
        if self.worker: self.worker.join(timeout=0)
        self.closed=True
        self.root.after_cancel(self.poll_id)
        if self.close_id is not None: self.root.after_cancel(self.close_id)
        if self.open_id is not None: self.root.after_cancel(self.open_id)
        (self.outer if self.embedded else self.root).destroy()
        return True

    def finish_close(self):
        self.close_id=None
        self.close()

    def run(self): self.root.mainloop()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=CONFIG_PATH)
    parser.add_argument('--inspect-only',action='store_true')
    parser.add_argument('--probe',action='store_true')
    parser.add_argument('--no-auto-open',action='store_true')
    options=parser.parse_args()
    config=load_config(options.config)
    if options.inspect_only or options.probe:
        with sync_playwright() as pw:
            flow=LoginBrowser(config,lambda kind,value:print(value) if kind in ('status','error') else None)
            try:
                flow.start(pw,restore=options.inspect_only,headless=True)
                if options.probe:
                    flow.ensure_login_form()
                    print('登录字段检查：通过')
            finally: flow.close()
    else: LoginApp(config,auto_open=not options.no_auto_open,config_path=options.config).run()
    return 0


if __name__=='__main__':
    if sys.stdout: sys.stdout.reconfigure(encoding='utf-8')
    try: sys.exit(main())
    except (FlowError,CaptchaError,BrowserError) as error:
        if sys.stdout: print(str(error))
        sys.exit(2)
    except Exception:
        if sys.stdout: print('助手已停止，请检查本地环境和浏览器。')
        sys.exit(3)
