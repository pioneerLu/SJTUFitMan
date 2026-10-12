"""Offline release smoke checks; never log in, reserve, or send mail."""
import io
import argparse
import base64
import json
from pathlib import Path
import sys
import tomllib

from runtime import ROOT, FROZEN, asset_path


def delayed_login_fixture(browser, template):
    """Only locally fulfilled pages: delayed redirect and a >5s form render."""
    from login_flow import LoginBrowser, LOGIN_HOST, HOST
    from urllib.parse import urlsplit
    target = 'https://' + LOGIN_HOST + '/__sjtufitman_fixture__'
    landing = '<script>setTimeout(() => location.replace(' + json.dumps(target) + '), 200)</script>'
    form = ('<input id="input-login-user" disabled hidden>'
        '<input id="input-login-pass" type="password" disabled hidden>'
        '<script>setTimeout(() => {for (const id of ["input-login-user","input-login-pass"]) {'
        'const field=document.getElementById(id);field.hidden=false;field.disabled=false;}}, 6000)</script>')
    context = browser.new_context(offline=True)
    def fulfill(route):
        parts = urlsplit(route.request.url)
        if parts.scheme == 'https' and parts.path == '/__sjtufitman_fixture__' and parts.hostname in (HOST, LOGIN_HOST):
            route.fulfill(status=200, content_type='text/html', body=landing if parts.hostname == HOST else form)
        else: route.abort()
    context.route('**/*', fulfill)
    try:
        flow = LoginBrowser(template, lambda *args: None)
        flow.page = context.new_page()
        flow.page.goto('https://' + HOST + '/__sjtufitman_fixture__', wait_until='domcontentloaded')
        flow.fill_credentials('LOCAL-FIXTURE', 'LOCAL-FIXTURE-PASSWORD')
        assert flow.page.locator('#input-login-user').input_value() == 'LOCAL-FIXTURE'
        assert flow.page.locator('#input-login-pass').input_value() == 'LOCAL-FIXTURE-PASSWORD'
        return True
    finally: context.close()


def components(browser_preference=None):
    from PIL import Image
    import cv2
    import numpy as np
    from playwright_runtime import sync_playwright
    from captcha import recognize_ocr, CaptchaError
    import booking_flow
    from poll_settings import PollSettings
    from version import VERSION
    from browser_runtime import preferred_browser, launch_browser
    template=tomllib.loads(asset_path('config/config.example.toml').read_text(encoding='utf-8'))
    assert not template['auth']['username'] and not template['auth']['password']
    assert not template['mail']['password'] and not template['mail']['recipient']
    assert not template['polling']['enabled']
    assert preferred_browser(template)=='auto'
    browser_report=None
    for _ in range(3):
        # Exercise the formerly failing start-and-immediately-stop path.
        with sync_playwright(): pass
    # Creating the Playwright driver verifies bundled Node/JS without starting a browser.
    with sync_playwright() as pw:
        assert pw.chromium is not None
        if browser_preference is not None:
            from captcha_image import NATIVE_IMAGE_JS, decode_native_image
            browser,choice=launch_browser(pw,{'browser':{'preferred':browser_preference}},headless=True)
            try:
                context=browser.new_context(locale='zh-CN',timezone_id='Asia/Shanghai')
                page=context.new_page()
                sample=io.BytesIO();Image.new('RGB',(110,40),'white').save(sample,format='PNG')
                image_url='data:image/png;base64,'+base64.b64encode(sample.getvalue()).decode()
                page.set_content('<title>SJTUFitMan browser check</title><img id="captcha-img" width="110" height="36" src="'+image_url+'">')
                page.wait_for_function("document.querySelector('#captcha-img').complete && document.querySelector('#captcha-img').naturalWidth > 0")
                _,native=decode_native_image(page.locator('#captcha-img').evaluate(NATIVE_IMAGE_JS))
                with Image.open(io.BytesIO(native)) as image: assert image.size==(110,40)
                if choice.kind=='edge': assert page.evaluate("navigator.userAgent.includes('Edg/')")
                browser_report={'kind':choice.kind,'version':browser.version,'native_captcha':True,'network_requests':False}
                browser_report['delayed_login_fill'] = delayed_login_fixture(browser, template)
            finally: browser.close()
    from PIL import ImageDraw,ImageFont
    image=Image.new('RGB',(160,60),'white')
    try: font=ImageFont.truetype('arial.ttf',36)
    except OSError: font=ImageFont.load_default(size=36)
    ImageDraw.Draw(image).text((5,4),'aB39',fill='black',font=font)
    stream=io.BytesIO();image.save(stream,format='PNG')
    try: recognize_ocr(stream.getvalue())
    except CaptchaError:
        # A synthetic image may be unrecognized. Model inference still must run.
        import captcha
        assert captcha._reader is not None
        result=captcha._reader.classification(stream.getvalue())
        assert isinstance(result,str)
    frame=np.zeros((20,20),dtype=np.uint8)
    assert cv2.Canny(frame,20,40).shape==(20,20)
    assert booking_flow.SNAPSHOT and booking_flow.CHECKOUT
    PollSettings.read({**template,'booking':{'time_slots':['18:00-20:00']}})
    return {'version':VERSION,'frozen':FROZEN,'offline_check':True,'browser_check':browser_report,
                     'playwright_driver':True,'driver_shutdown':True,'ocr_inference':True,'opencv':True,
                     'assets':True,'real_order_created':False,'email_sent':False}


def run(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',choices=('auto','chrome','edge'),help='Optional offline launch check using a local browser.')
    options=parser.parse_args(argv)
    print(json.dumps(components(options.browser)))
    return 0


def smoke_ui():
    from configuration import ensure_config
    from app_ui import FitnessApp
    import tkinter as tk
    ensure_config()
    window=tk.Tk();window.withdraw()
    app=FitnessApp(window,start_workers=False)
    report=components()
    for page in FitnessApp.PAGES:
        app.show_page(page);window.update_idletasks();window.update()
        assert app.current_page==page
    automatic=app.components['auto']
    automatic.mode.set(automatic.MODES['daily']);automatic.change_mode()
    window.update_idletasks();window.update()
    assert automatic.mode_key=='daily'
    automatic.variables['stop_mode'].set(automatic.STOP_MODES['duration'])
    automatic.variables['duration_hours'].set('02'); automatic.variables['duration_minutes'].set('00')
    automatic.update_controls()
    assert automatic.limit_values()==('duration',120,'')
    automatic.variables['stop_mode'].set(automatic.STOP_MODES['deadline']); automatic.update_controls()
    assert automatic.limit_values()[0]=='deadline' and automatic.limit_values()[2].endswith('+08:00')
    assert not app.components['settings'].login.worker
    assert tuple(app.components['settings'].login.browser_choice.cget('values'))==(
        '自动选择（优先Chrome）','Google Chrome','Microsoft Edge')
    from PIL import Image
    stream = io.BytesIO(); Image.new('RGB', (110, 40), 'white').save(stream, format='PNG')
    login = app.components['settings'].login
    login.events.put(('image', stream.getvalue())); login.events.put(('manual-captcha', None))
    window.after_cancel(login.poll_id); login.poll_events(); window.update_idletasks(); window.update()
    assert login.more_login.get() and (login.image.width(), login.image.height()) == (330, 120)
    assert not automatic.process and not app.components['records'].process
    assert not app.components['settings'].mail.process and not app.mail_service.thread
    app.close()
    (ROOT/'smoke-ui.json').write_text(json.dumps({**report,'all_pages':True,'captcha_preview':True,'stop_limits':True,'browser_selection':True,'workers_started':False,
                                               'real_order_created':False,'email_sent':False}),encoding='utf-8')
    return 0
