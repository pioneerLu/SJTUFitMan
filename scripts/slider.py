"""OpenCV gap localization and a bounded drag using the site's own widget."""
import base64
from dataclasses import asdict, dataclass
import hashlib
import time

import cv2
import numpy as np

from captcha import CaptchaError

VERSION = 'opencv-masked-texture-v1'


@dataclass(frozen=True)
class Match:
    x: float
    y: int
    score: float
    margin: float
    background_width: int
    algorithm: str = VERSION


def decode(image):
    if not isinstance(image, bytes) or not 0 < len(image) <= 512 * 1024:
        raise CaptchaError('滑块图片大小异常，转为手动处理。')
    from PIL import Image
    import io
    try:
        with Image.open(io.BytesIO(image)) as opened:
            if opened.format != 'PNG' or not (20 <= opened.width <= 600 and 20 <= opened.height <= 300):
                raise ValueError()
        result = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if result is None:
            raise ValueError()
        return result
    except Exception:
        raise CaptchaError('滑块图片格式变化，转为手动处理。') from None


def locate_gap(background, piece):
    bg, part = decode(background), decode(piece)
    if bg.ndim != 3 or part.ndim != 3 or part.shape[2] != 4 or bg.shape[2] not in (3,4):
        raise CaptchaError('滑块缺少透明轮廓，转为手动处理。')
    alpha = (part[:,:,3] > 127).astype(np.uint8) * 255
    x0, y0, w, h = cv2.boundingRect(alpha)
    if not (20 <= w <= 100 and 20 <= h <= 100) or bg.shape[0] != part.shape[0]:
        raise CaptchaError('滑块尺寸变化，转为手动处理。')
    template = cv2.cvtColor(part[y0:y0+h,x0:x0+w,:3], cv2.COLOR_BGR2GRAY).astype(np.float32)
    mask = cv2.erode(alpha[y0:y0+h,x0:x0+w], np.ones((5,5),np.uint8))
    if cv2.countNonZero(mask) < 150:
        raise CaptchaError('滑块有效图案不足，转为手动处理。')
    gray = cv2.cvtColor(bg[:,:,:3], cv2.COLOR_BGR2GRAY).astype(np.float32)
    response = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED, mask=mask)
    response = np.nan_to_num(response, nan=-1., posinf=-1., neginf=-1.)
    allowed = np.full_like(response,-1.)
    low, high = max(0,y0-1), min(response.shape[0],y0+2)
    min_x = max(w+5, 40)
    allowed[low:high,min_x:] = response[low:high,min_x:]
    _, score, _, location = cv2.minMaxLoc(allowed)
    x, y = location
    alternatives = allowed.copy()
    alternatives[:,max(0,x-10):min(alternatives.shape[1],x+11)] = -1.
    margin = float(score-np.max(alternatives))
    if score < .72 or margin < .08 or not 0 < x-x0 < bg.shape[1]-part.shape[1]:
        raise CaptchaError('滑块定位不确定，转为手动处理。')
    return Match(float(x-x0),y,round(float(score),4),round(margin,4),bg.shape[1])


def read_challenge(page):
    popup = page.locator('.verifybox:visible')
    if popup.count() != 1:
        raise CaptchaError('没有唯一的滑块窗口，转为手动处理。')
    page.wait_for_function("() => [...document.querySelectorAll('.verifybox')].filter(e=>e.offsetParent).some(e=>e.querySelectorAll('img').length===2&&[...e.querySelectorAll('img')].every(i=>i.complete&&i.naturalWidth>0))", timeout=15000)
    values = popup.locator('img').evaluate_all('(imgs)=>imgs.map(i=>i.src)')
    if len(values)!=2 or any(not v.startswith('data:image/png;base64,') or len(v)>700000 for v in values):
        raise CaptchaError('滑块图片来源变化，转为手动处理。')
    try:
        bg, part = [base64.b64decode(v.split(',',1)[1], validate=True) for v in values]
    except Exception:
        raise CaptchaError('滑块图片无法读取，转为手动处理。') from None
    return popup, bg, part, hashlib.sha256(bg+part).hexdigest()


def drag_once(page, gate):
    started = time.monotonic()
    try:
        popup,bg,part,image_hash = read_challenge(page)
        match = locate_gap(bg,part)
        handle = popup.locator('.verify-move-block')
        bar = popup.locator('.verify-bar-area')
        image = popup.locator('.verify-img-panel > img')
        hb,bb,ib = handle.bounding_box(),bar.bounding_box(),image.bounding_box()
        if not hb or not bb or not ib or abs(bb['width']-ib['width'])>3:
            raise CaptchaError('滑块控件尺寸变化，转为手动处理。')
        displacement = match.x * ib['width'] / match.background_width
        if not 0<displacement<bb['width']-hb['width']-2:
            raise CaptchaError('滑块移动距离异常，转为手动处理。')
        if read_challenge(page)[3] != image_hash:
            raise CaptchaError('滑块图片已刷新，转为手动处理。')
        gate.authorize_captcha(image_hash)
        start_x,start_y = hb['x']+hb['width']/2,hb['y']+hb['height']/2
        page.mouse.move(start_x,start_y)
        page.mouse.down()
        try:
            for step in range(1,21):
                page.mouse.move(start_x+displacement*step/20,start_y)
                page.wait_for_timeout(40)
            with page.expect_response(lambda r:r.url.split('?')[0].endswith('/captcha/check'),timeout=15000) as pending:
                page.mouse.up()
            response=pending.value
            body=response.json()
        finally:
            page.mouse.up()
        ok=response.status==200 and isinstance(body,dict) and body.get('repCode')=='0000'
        return {'phase':'passed' if ok else 'manual_required','server_verified':ok,
            'reason':None if ok else 'server_rejected','rep_code':str(body.get('repCode','unknown'))[:12],
            'match':asdict(match),'duration_ms':round((time.monotonic()-started)*1000)}
    except CaptchaError as error:
        return {'phase':'manual_required','server_verified':False,'reason':str(error),
            'duration_ms':round((time.monotonic()-started)*1000)}
    except Exception:
        return {'phase':'manual_required','server_verified':None,'reason':'验证响应未确认，转为手动处理。',
            'duration_ms':round((time.monotonic()-started)*1000)}
