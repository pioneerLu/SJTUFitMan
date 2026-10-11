"""Local manual/OCR login helper. All recognition stays on this computer."""
import argparse
from datetime import datetime, timezone
import io
from pathlib import Path
import queue
import sys
import threading
import time

from runtime import ROOT, ensure_venv
if __name__=='__main__':
    ensure_venv()

from captcha import CaptchaError, normalize_code, recognize_ocr
from login_flow import FlowError, LoginBrowser, atomic_json, load_config
from configuration import CONFIG_PATH, ConfigError, update_config


def browser_worker(config, commands, events):
    def emit(kind,value):
        events.put((kind,value))
        if kind in ('status','error'):
            atomic_json(ROOT/'.local/login-status.json',{
                'updated_at':datetime.now(timezone.utc).isoformat(),'kind':kind,'message':value})
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            flow=None
            while True:
                try: action,payload=commands.get(timeout=.25)
                except queue.Empty: action,payload=None,None
                try:
                    if action=='quit':
                        if flow: flow.close()
                        return
                    if action in ('open','restore'):
                        if flow: flow.close()
                        flow=LoginBrowser(config,emit)
                        flow.start(pw,restore=action=='restore')
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
                except (FlowError,CaptchaError) as error: emit('error',str(error))
                except Exception: emit('error','操作已停止，请在当前浏览器手动处理。')
                finally:
                    payload=None
                    if action: events.put(('done',None))
                if flow and flow.browser and not flow.browser.is_connected():
                    flow=None
                    emit('status','浏览器已关闭，保存的会话仍在本地。')
                if flow:
                    try: flow.tick()
                    except FlowError as error: emit('error',str(error))
                    except Exception: emit('error','会话尚未确认，请查看浏览器状态。')
    except Exception:
        emit('error','助手启动失败，请检查独立环境和Chrome。')
        events.put(('done',None))


class LoginApp:
    def __init__(self,config,auto_open=True,start_worker=True,window=None,parent=None,activity_guard=None,config_path=CONFIG_PATH):
        import tkinter as tk
        from tkinter import ttk
        self.tk=tk
        self.root=window if window is not None else tk.Tk()
        self.embedded=parent is not None
        self.closed=False
        self.activity_guard=activity_guard or (lambda:True)
        if not self.embedded:
            self.root.title('交大健身房 · 本地登录助手')
            self.root.geometry('800x740')
            self.root.minsize(740,700)
        self.root.option_add('*Font',('Microsoft YaHei UI',10))
        self.config=config
        self.config_path=Path(config_path)
        self.commands,self.events=queue.Queue(),queue.Queue()
        self.buttons=[]
        self.pending=False
        self.image=None
        self.username=tk.StringVar(value=config.get('auth',{}).get('username',''))
        self.password=tk.StringVar(value=config.get('auth',{}).get('password',''))
        self.code=tk.StringVar()
        self.status=tk.StringVar(value='识别完全在本机进行。失败时保留浏览器供手动登录。')
        outer=self.outer=ttk.Frame(parent if parent is not None else self.root,padding=20)
        outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='登录并保存体育平台会话',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w')
        row=ttk.Frame(outer)
        row.pack(fill='x',pady=14)
        self.button(row,'打开登录页 / 重新登录',lambda:self.enqueue('open')).pack(side='left')
        self.button(row,'检查已保存会话',lambda:self.enqueue('restore')).pack(side='left',padx=10)
        credentials=ttk.LabelFrame(outer,text='本地账号密码',padding=12)
        credentials.pack(fill='x',pady=8)
        for index,(label,variable) in enumerate((('jAccount账号',self.username),('密码',self.password))):
            ttk.Label(credentials,text=label).grid(row=index,column=0,sticky='w',pady=4)
            ttk.Entry(credentials,textvariable=variable,width=45,show='●' if index else '').grid(row=index,column=1,padx=12,pady=4)
        self.button(credentials,'填入浏览器',lambda:self.enqueue('credentials',self.credentials())).grid(row=2,column=0,columnspan=2,sticky='w',pady=6)
        self.button(credentials,'保存账号到本地配置',self.save_credentials).grid(row=2,column=1,sticky='e',pady=6)
        ttk.Label(credentials,text='输入只在内存中使用；点击保存才写入个人config/config.toml。').grid(row=3,column=0,columnspan=2,sticky='w')
        area=ttk.LabelFrame(outer,text='本地ddddocr与手动处理',padding=12)
        area.pack(fill='x',pady=8)
        row=ttk.Frame(area)
        row.pack(fill='x')
        self.button(row,'OCR自动登录',lambda:self.enqueue('auto',self.credentials())).pack(side='left')
        self.button(row,'只识别当前图片',lambda:self.enqueue('ocr')).pack(side='left',padx=8)
        self.button(row,'刷新图片',lambda:self.enqueue('refresh')).pack(side='left')
        self.image_label=ttk.Label(area,text='验证码图片会显示在这里',anchor='center')
        self.image_label.pack(fill='x',pady=10)
        row=ttk.Frame(area)
        row.pack(fill='x')
        ttk.Label(row,text='手动验证码').pack(side='left')
        ttk.Entry(row,textvariable=self.code,width=15).pack(side='left',padx=10)
        self.button(row,'使用此验证码登录',self.submit).pack(side='left')
        ttk.Label(area,text='自动最多尝试2次；仅验证码错误时重试，间隔至少20秒。\n密码错误、网络异常、额外认证或识别失败时转手动。',wraplength=720).pack(anchor='w',pady=(10,0))
        ttk.Label(outer,textvariable=self.status,wraplength=740,foreground='#215c4b').pack(anchor='w',pady=12)
        self.summary=ttk.Label(outer,text='成功后：服务器确认 → 保存会话 → 新会话恢复验证 → 检查健身房预约页',wraplength=740)
        self.summary.pack(anchor='w')
        if not self.embedded: self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.worker=None
        if start_worker:
            self.worker=threading.Thread(target=browser_worker,args=(config,self.commands,self.events),daemon=True)
            self.worker.start()
        self.poll_id=self.root.after(150,self.poll_events)
        if auto_open: self.root.after(300,lambda:self.enqueue('open'))

    def credentials(self): return {'username':self.username.get(),'password':self.password.get()}

    def save_credentials(self):
        if self.pending or not self.activity_guard(): return
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
        if self.pending or self.closed or not self.activity_guard(): return
        self.pending=True
        for button in self.buttons: button.configure(state='disabled')
        self.status.set('正在处理，请稍候……')
        self.commands.put((action,payload))

    def submit(self):
        from tkinter import messagebox
        try: self.enqueue('submit',normalize_code(self.code.get()))
        except CaptchaError as error: messagebox.showerror('验证码',str(error),parent=self.root)

    def poll_events(self):
        from PIL import Image,ImageTk
        while True:
            try: kind,value=self.events.get_nowait()
            except queue.Empty: break
            if kind in ('status','error'): self.status.set(value)
            elif kind=='image':
                image=Image.open(io.BytesIO(value))
                image.thumbnail((360,100))
                self.image=ImageTk.PhotoImage(image)
                self.image_label.configure(image=self.image,text='')
            elif kind=='code': self.code.set(value)
            elif kind=='done':
                self.pending=False
                for button in self.buttons: button.configure(state='normal')
            elif kind=='inspection': self.summary.configure(text='已恢复会话并检查真实健身房预约页。')
        self.poll_id=self.root.after(150,self.poll_events)

    def close(self):
        from tkinter import messagebox
        if self.closed: return True
        if self.pending:
            if not self.embedded: messagebox.showinfo('正在处理','请等待本次操作完成后关闭。',parent=self.root)
            return False
        self.closed=True
        self.commands.put(('quit',None))
        self.root.after_cancel(self.poll_id)
        (self.outer if self.embedded else self.root).destroy()
        if self.worker: self.worker.join(timeout=10)
        return True

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
        from playwright.sync_api import sync_playwright
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
    except (FlowError,CaptchaError) as error:
        if sys.stdout: print(str(error))
        sys.exit(2)
    except Exception:
        if sys.stdout: print('助手已停止，请检查本地环境和浏览器。')
        sys.exit(3)
