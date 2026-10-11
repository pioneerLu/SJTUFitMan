"""Read slider images from the existing page; never submit any order."""
import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright
from booking_flow import BookingBrowser
from login_flow import atomic_json, load_config
from configuration import CONFIG_PATH
from session import ROOT

SHOW = '''() => {
  const stack = [document.querySelector('#app')?.__vue__];
  while(stack.length) {
    const v = stack.pop();
    if(v?.$options?.name === 'apointmentDetails') {
      v.$refs.verify.show(); return true;
    }
    stack.push(...(v?.$children || []));
  }
  return false;
}'''

def main():
    private = ROOT / '.local/captcha/probe'
    private.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        b = BookingBrowser(load_config(CONFIG_PATH))
        try:
            b.start(pw)
            b.page.evaluate(SHOW)
            print(json.dumps(b.page.locator('.verifybox').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,cls:e.className,visible:!!e.offsetParent,imgs:e.querySelectorAll("img").length}))')))
            b.page.locator('.verifybox:visible').first.wait_for(state='visible')
            b.page.wait_for_function("() => [...document.querySelectorAll('.verifybox')].filter(e=>e.offsetParent).some(e=>e.querySelectorAll('img').length===2 && [...e.querySelectorAll('img')].every(i=>i.complete&&i.naturalWidth>0))")
            import base64
            values = b.page.locator('.verifybox:visible').first.locator('img').evaluate_all('(imgs)=>imgs.map(i=>({src:i.src,w:i.naturalWidth,h:i.naturalHeight,box:{w:i.width,h:i.height}}))')
            for name, value in zip(('background', 'piece'), values):
                (private / (name+'.png')).write_bytes(base64.b64decode(value.pop('src').split(',',1)[1]))
            b.page.locator('.verifybox:visible').first.screenshot(path=str(private / 'popup.png'))
            report={'images':values,'handle':b.page.locator('.verifybox:visible').first.locator('.verify-move-block').bounding_box(), 'network': b.gate.events, 'real_order_created':False}
            atomic_json(private / 'structure.json',report)
            print(json.dumps(report,ensure_ascii=False,indent=2))
        finally:
            b.close()

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try: main()
    except Exception as error:
        import traceback
        print(json.dumps({'error_type':type(error).__name__,'locations':[{'function':x.name,'line':x.lineno} for x in traceback.extract_tb(error.__traceback__) if Path(x.filename).parent==Path(__file__).parent]}))
        sys.exit(1)
