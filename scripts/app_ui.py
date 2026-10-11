"""One desktop window hosting local login, booking and polling modules."""
import argparse
import tkinter as tk
from tkinter import ttk

from booking_ui import BookingApp
from login import LoginApp
from login_flow import load_config
from poller_ui import PollerApp
from session import ROOT
from configuration import CONFIG_PATH, ConfigError, ensure_config


class FitnessApp:
    PAGES={'home':'首页','login':'登录与会话','booking':'单次预约',
        'daily':'每日开抢','waitlist':'退选候补','mail':'邮件设置'}

    def __init__(self, window, initial_page='home', autostart=False, start_workers=True, config_loader=None):
        self.window=window
        self.components={}
        self.frames={}
        self.start_workers=start_workers
        self.config_loader=config_loader or (lambda:load_config(CONFIG_PATH))
        self.closing=False
        self.closed=False
        self.close_timer=None
        self.current_page='home'
        window.title('交大健身房助手')
        window.geometry('1000x940')
        window.minsize(940,900)
        window.option_add('*Font',('Microsoft YaHei UI',10))
        header=ttk.Frame(window,padding=(20,12))
        header.pack(fill='x')
        ttk.Label(header,text='交大健身房助手',font=('Microsoft YaHei UI',20,'bold')).pack(anchor='w')
        ttk.Label(header,text='登录、预约和付款提醒，集中在一个窗口中操作。',foreground='#666666').pack(anchor='w',pady=(4,0))
        nav=ttk.Frame(window,padding=(15,0,15,8));nav.pack(fill='x')
        self.nav_buttons={}
        for column,(page,label) in enumerate(self.PAGES.items()):
            button=ttk.Button(nav,text=label,command=lambda selected=page:self.show_page(selected))
            button.grid(row=0,column=column,sticky='ew',padx=3)
            nav.columnconfigure(column,weight=1);self.nav_buttons[page]=button
        self.content=ttk.Frame(window)
        self.content.pack(fill='both',expand=True)
        self.content.rowconfigure(0,weight=1);self.content.columnconfigure(0,weight=1)
        footer=ttk.Frame(window,padding=(20,8));footer.pack(fill='x')
        self.task_status=tk.StringVar(value='就绪。打开功能页面不会自动预约。')
        ttk.Label(footer,textvariable=self.task_status,wraplength=760).pack(side='left',fill='x',expand=True)
        self.stop_button=ttk.Button(footer,text='停止当前轮询',command=self.stop_polling,state='disabled')
        self.stop_button.pack(side='right',padx=(12,0))
        self.build_home()
        self.show_page('daily' if autostart else initial_page)
        window.protocol('WM_DELETE_WINDOW',self.close)
        self.timer=window.after(250,self.refresh_status)
        if autostart: window.after(500,self.components['polling'].start_saved)

    def frame(self, name):
        frame=ttk.Frame(self.content)
        frame.grid(row=0,column=0,sticky='nsew')
        self.frames[name]=frame
        return frame

    def build_home(self):
        home=self.frame('home')
        outer=ttk.Frame(home,padding=24);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='选择你要做的事情',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w')
        ttk.Label(outer,text='首次使用先登录；已有会话可以直接选择预约方式。',foreground='#666666').pack(anchor='w',pady=(8,22))
        cards=ttk.Frame(outer);cards.pack(fill='x')
        entries=[('login','登录与会话','本地图片识别或手动登录，保存会话供预约复用。'),
            ('booking','单次预约','选择日期和时段，检查余量、查看须知并确认一次预约。'),
            ('daily','每日开抢','每天北京时间12:00预约七天后的时段，成功后邮件提醒付款。'),
            ('waitlist','退选候补','选定七天内的日期和时段，立即或定时等待退选名额。')]
        self.home_buttons=[]
        for index,(page,title,description) in enumerate(entries):
            card=ttk.LabelFrame(cards,text=title,padding=18)
            card.grid(row=index//2,column=index%2,sticky='nsew',padx=6,pady=8)
            ttk.Label(card,text=description,wraplength=370).pack(anchor='w',pady=(0,18))
            button=ttk.Button(card,text='进入'+title,command=lambda selected=page:self.show_page(selected))
            button.pack(anchor='w');self.home_buttons.append(button)
        cards.columnconfigure(0,weight=1);cards.columnconfigure(1,weight=1)
        ttk.Label(outer,text='邮件设置共用已有配置；支持一小时或连续两小时预约。\n切换页面后，已启动的轮询继续运行。电脑需保持开机联网且不睡眠。\n预约成功后请到体育平台个人订单付款，助手不会自动付款。',
            wraplength=850,foreground='#666666').pack(anchor='w',pady=(24,0))

    def show_page(self, page):
        if self.closing or page not in self.PAGES: return
        name='polling' if page in ('daily','waitlist','mail') else page
        if name not in self.frames:
            parent=self.frame(name)
            guard=lambda:self.allow_action(name)
            try:
                if name=='login':
                    component=LoginApp(self.config_loader(),auto_open=False,start_worker=self.start_workers,
                        window=self.window,parent=parent,activity_guard=guard)
                elif name=='booking':
                    component=BookingApp(self.window,parent=parent,activity_guard=guard)
                else:
                    component=PollerApp(self.window,parent=parent,activity_guard=guard)
                self.components[name]=component
            except Exception:
                # Keep config/browser exceptions out of the interface and logs.
                parent.destroy();self.frames.pop(name,None)
                self.task_status.set('功能页面未能打开，请检查本地配置和虚拟环境。')
                return
        self.frames[name].tkraise()
        self.current_page=page
        if name=='polling':
            component=self.components[name]
            component.tabs.select({'daily':component.booking_tab,'waitlist':component.waitlist_tab,
                'mail':component.mail_tab}[page])
        for key,button in self.nav_buttons.items():
            button.state(['pressed'] if key==page else ['!pressed'])

    def active(self):
        for name,component in self.components.items():
            if getattr(component,'pending',False) or getattr(component,'busy',False): return name,component
            review=getattr(component,'review',None)
            if review is not None and review.winfo_exists(): return name,component
        return None,None

    def allow_action(self, owner):
        active,component=self.active()
        if not self.closing and (active is None or active==owner): return True
        message='正在关闭，请等待当前操作完成。' if self.closing else '当前'+{
            'login':'登录','booking':'单次预约','polling':'预约轮询或邮件'}[active]+'尚未结束，请先完成或停止当前操作。'
        self.task_status.set(message)
        if owner in self.components: self.components[owner].status.set(message)
        return False

    def stop_polling(self):
        component=self.components.get('polling')
        if component and component.busy and component.running_command in ('run','waitlist'):
            component.stop()

    def refresh_status(self):
        if self.closed: return
        name,component=self.active()
        if self.closing:
            self.task_status.set('正在停止并等待当前操作核实，完成后自动关闭。')
        elif component:
            title={'login':'登录','booking':'单次预约','polling':'预约轮询 / 邮件'}[name]
            self.task_status.set(title+'：'+component.status.get())
        else:
            self.task_status.set('就绪。请选择功能；预约成功后需自行付款。')
        polling=self.components.get('polling')
        self.stop_button.configure(state='normal' if polling and polling.busy and
            polling.running_command in ('run','waitlist') else 'disabled')
        self.timer=self.window.after(250,self.refresh_status)

    def close(self):
        if self.closed: return
        if not self.closing:
            self.closing=True
            for button in [*self.nav_buttons.values(),*self.home_buttons]: button.configure(state='disabled')
            booking=self.components.get('booking')
            if booking and booking.review is not None and booking.review.winfo_exists():
                if booking.review.grab_current() is booking.review: booking.review.grab_release()
                booking.review.destroy();booking.review=None
            self.stop_polling()
        if any(getattr(component,'pending',False) or getattr(component,'busy',False)
                for component in self.components.values()):
            self.task_status.set('正在停止并等待当前操作核实，完成后自动关闭。')
            if self.close_timer is None: self.close_timer=self.window.after(200,self.finish_close)
            return
        self.closed=True
        if self.close_timer is not None: self.window.after_cancel(self.close_timer)
        self.window.after_cancel(self.timer)
        for component in self.components.values(): component.close()
        self.window.destroy()

    def finish_close(self):
        self.close_timer=None
        self.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--page',choices=tuple(FitnessApp.PAGES),default='home')
    parser.add_argument('--autostart',action='store_true',help='Start an already-enabled daily plan.')
    options=parser.parse_args(argv)
    try: ensure_config()
    except ConfigError as error:
        from tkinter import messagebox
        error_window=tk.Tk();error_window.withdraw()
        messagebox.showerror('本地配置',str(error),parent=error_window)
        error_window.destroy();return
    window=tk.Tk()
    FitnessApp(window,options.page,options.autostart)
    window.mainloop()


if __name__=='__main__':
    from runtime import ensure_venv
    ensure_venv()
    main()
