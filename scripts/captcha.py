"""Local, CPU-only ddddocr inference. No remote recognition providers."""
import io
import re
import threading

MAX_IMAGE_BYTES = 512 * 1024
_reader = None
_lock = threading.Lock()


class CaptchaError(Exception):
    """Only fixed, non-sensitive messages may leave this module."""


def normalize_code(value):
    if not isinstance(value, str):
        raise CaptchaError('识别结果格式错误，请手动输入验证码。')
    value = value.strip().strip('`\"\'').strip()
    if not re.fullmatch(r'[A-Za-z0-9]{4,6}', value):
        raise CaptchaError('识别结果不是4～6位字母或数字，请手动输入。')
    return value


def validate_image(image):
    from PIL import Image
    if not isinstance(image, bytes) or not 0 < len(image) <= MAX_IMAGE_BYTES:
        raise CaptchaError('验证码图片大小异常。')
    try:
        with Image.open(io.BytesIO(image)) as opened:
            if opened.format != 'PNG' or not (1 <= opened.width <= 600 and 1 <= opened.height <= 300):
                raise CaptchaError('只接受裁剪后的验证码PNG图片。')
            opened.verify()
    except CaptchaError:
        raise
    except Exception:
        raise CaptchaError('验证码图片无法读取。') from None


def recognize_ocr(image):
    global _reader
    validate_image(image)
    try:
        import ddddocr
        with _lock:
            if _reader is None:
                _reader = ddddocr.DdddOcr(show_ad=False, use_gpu=False)
            return normalize_code(_reader.classification(image))
    except CaptchaError:
        raise
    except Exception:
        raise CaptchaError('本地OCR无法运行，请手动输入验证码。') from None
