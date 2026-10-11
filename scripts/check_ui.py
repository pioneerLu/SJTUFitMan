"""Check local window layout without opening a browser or entering credentials."""
import json
import tkinter as tk
from login import LoginApp
from booking_ui import BookingApp
from poller_ui import PollerApp
from app_ui import FitnessApp


def main():
    login=LoginApp({'auth':{}},auto_open=False,start_worker=False)
    login.root.attributes('-alpha',0.0)
    login.root.update_idletasks()
    login.root.update()
    login.root.withdraw()
    login_fits=all(button.winfo_rooty()+button.winfo_height()<=login.root.winfo_rooty()+login.root.winfo_height() for button in login.buttons)
    report={'login':{'controls_fit':login_fits,'actual_height':login.root.winfo_height()}}
    login.close()
    window=tk.Tk()
    window.attributes('-alpha',0.0)
    app=BookingApp(window)
    window.update_idletasks()
    window.update()
    booking_fits=all(button.winfo_rooty()+button.winfo_height()<=window.winfo_rooty()+window.winfo_height() for button in app.buttons)
    report['booking']={'controls_fit':booking_fits,'text_height':app.text.winfo_height(),'actual_height':window.winfo_height()}
    app.prepared={'slot':{'venue_name':'布局检查','activity':'健身房','date':'2026-10-18',
        'time':'18:00-20:00','field':'场地1','price':'4.00'},'terms_title':'预订须知',
        'terms_text':'用于本地布局检查的须知。\n'*100,'confirmation_sha256':'layout-test-only'}
    app.confirm()
    review=next(widget for widget in window.winfo_children() if isinstance(widget,tk.Toplevel))
    review.attributes('-alpha',0.0)
    review.update_idletasks();review.update()
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)
    footer=[widget for widget in descendants(review)
        if widget.winfo_class()=='TCheckbutton' or
        (widget.winfo_class()=='TButton' and '真实订单' in widget.cget('text'))]
    review_fits=len(footer)==2 and all(widget.winfo_ismapped() and
        widget.winfo_rooty()>=review.winfo_rooty() and
        widget.winfo_rooty()+widget.winfo_height()<=review.winfo_rooty()+review.winfo_height()
        for widget in footer)
    report['review']={'controls_fit':review_fits,'actual_height':review.winfo_height(),
        'footer_count':len(footer),'order_submitted':False}
    review.destroy()
    app.close()
    daily_window=tk.Tk()
    daily_window.attributes('-alpha',0.0)
    daily=PollerApp(daily_window)
    daily_window.update_idletasks()
    daily_window.update()
    daily_fits=all(button.winfo_rooty()+button.winfo_height()<=daily_window.winfo_rooty()+daily_window.winfo_height() for button in daily.buttons)
    report['daily']={'controls_fit':daily_fits,'text_height':daily.text.winfo_height(),'actual_height':daily_window.winfo_height(),'worker_started':daily.process is not None}
    daily.tabs.select(daily.waitlist_tab)
    daily_window.geometry('810x740');daily_window.update_idletasks();daily_window.update()
    waitlist_fits=all(button.winfo_ismapped() and
        button.winfo_rooty()+button.winfo_height()<=daily_window.winfo_rooty()+daily_window.winfo_height()
        for button in [*daily.buttons,*daily.extra_buttons])
    report['waitlist']={'controls_fit':waitlist_fits,'text_height':daily.text.winfo_height(),
        'actual_height':daily_window.winfo_height(),'worker_started':daily.process is not None}
    daily.close()
    unified_window=tk.Tk();unified_window.attributes('-alpha',0.0)
    unified=FitnessApp(unified_window,start_workers=False,config_loader=lambda:{'auth':{}})
    unified_window.geometry('940x900')
    unified_report={}
    for page in ('home','login','booking','daily','waitlist','mail'):
        unified.show_page(page);unified_window.update_idletasks();unified_window.update()
        component=unified.components.get('polling' if page in ('daily','waitlist','mail') else page)
        buttons=[*unified.nav_buttons.values(),unified.stop_button]
        if page=='home': buttons+=unified.home_buttons
        elif component:
            buttons+=component.buttons
            if page=='waitlist': buttons+=component.extra_buttons
        visible=[button for button in buttons if button.winfo_ismapped()]
        fits=all(button.winfo_rooty()>=unified_window.winfo_rooty() and
            button.winfo_rooty()+button.winfo_height()<=unified_window.winfo_rooty()+unified_window.winfo_height()
            and button.winfo_rootx()+button.winfo_width()<=unified_window.winfo_rootx()+unified_window.winfo_width()
            for button in visible)
        unified_report[page]={'controls_fit':fits,'visible_buttons':len(visible),
            'text_height':component.text.winfo_height() if hasattr(component,'text') else None}
    unified_report['one_window']=not any(isinstance(child,tk.Toplevel) for child in unified_window.winfo_children())
    unified_report['workers_started']=bool(unified.components['login'].worker or unified.components['polling'].process or unified.components['booking'].busy)
    report['unified']=unified_report
    unified_ok=all(unified_report[page]['controls_fit'] for page in unified.PAGES) and unified_report['one_window'] and not unified_report['workers_started']
    unified.close()
    print(json.dumps(report))
    return 0 if unified_ok and login_fits and booking_fits and review_fits and daily_fits and waitlist_fits and report['booking']['text_height']>=120 and report['daily']['text_height']>=100 and report['waitlist']['text_height']>=100 and not report['daily']['worker_started'] else 2


if __name__=='__main__':
    raise SystemExit(main())
