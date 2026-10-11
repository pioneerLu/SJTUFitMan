"""Offline release smoke checks; never log in, reserve, or send mail."""
import io
import json
from pathlib import Path
import sys
import tomllib

from runtime import ROOT, FROZEN, asset_path


def components():
    from PIL import Image
    import cv2
    import numpy as np
    from playwright.sync_api import sync_playwright
    from captcha import recognize_ocr, CaptchaError
    import booking_flow
    from poll_settings import PollSettings
    from version import VERSION
    template=tomllib.loads(asset_path('config/config.example.toml').read_text(encoding='utf-8'))
    assert not template['auth']['username'] and not template['auth']['password']
    assert not template['mail']['password'] and not template['mail']['recipient']
    assert not template['polling']['enabled']
    # Creating the Playwright driver verifies bundled Node/JS without starting a browser.
    with sync_playwright() as pw:
        assert pw.chromium is not None
        context=pw.request.new_context()
        context.dispose()
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
    return {'version':VERSION,'frozen':FROZEN,'offline_check':True,
                     'playwright_driver':True,'ocr_inference':True,'opencv':True,
                     'assets':True,'real_order_created':False,'email_sent':False}


def run(argv=None):
    print(json.dumps(components()))
    return 0


def smoke_ui():
    from configuration import ensure_config
    from app_ui import FitnessApp
    import tkinter as tk
    ensure_config()
    window=tk.Tk();window.withdraw()
    app=FitnessApp(window,start_workers=False)
    report=components()
    for page in ('home','login','booking','daily','waitlist','mail'):
        app.show_page(page);window.update_idletasks();window.update()
        assert app.current_page==page
    assert not app.components['login'].worker
    assert not app.components['polling'].process
    assert not app.components['booking'].busy
    app.close()
    (ROOT/'smoke-ui.json').write_text(json.dumps({**report,'all_pages':True,'workers_started':False,
                                               'real_order_created':False,'email_sent':False}),encoding='utf-8')
    return 0
