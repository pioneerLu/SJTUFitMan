"""Read the already-loaded CAPTCHA pixels and prepare a separate readable preview."""
import base64
import binascii
import io

from captcha import CaptchaError, MAX_IMAGE_BYTES, validate_image


# Drawing the loaded image avoids another request to the CAPTCHA endpoint.
# CSS scaling, clipping and overlays do not affect these native pixels.
NATIVE_IMAGE_JS = """image => {
    if (!image.complete || !image.naturalWidth || !image.naturalHeight ||
        image.naturalWidth > 600 || image.naturalHeight > 300)
        throw new Error('CAPTCHA image unavailable');
    const canvas = document.createElement('canvas');
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    canvas.getContext('2d').drawImage(image, 0, 0);
    return {source: image.getAttribute('src'), png: canvas.toDataURL('image/png')};
}"""


def decode_native_image(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('source'), str):
        raise CaptchaError('验证码原图尚未加载，请刷新图片或在浏览器手动登录。')
    value = snapshot.get('png')
    prefix = 'data:image/png;base64,'
    if (not isinstance(value, str) or not value.startswith(prefix)
            or len(value) > len(prefix) + 4 * ((MAX_IMAGE_BYTES + 2) // 3)):
        raise CaptchaError('验证码原图无法读取，请刷新图片或在浏览器手动登录。')
    try:
        image = base64.b64decode(value[len(prefix):], validate=True)
    except (ValueError, binascii.Error):
        raise CaptchaError('验证码原图无法读取，请刷新图片或在浏览器手动登录。') from None
    validate_image(image)
    return snapshot['source'], image


def captcha_preview(image_bytes, bounds=(440, 140)):
    """Enlarge small images up to 3x; preserve the entire image and aspect ratio."""
    from PIL import Image
    validate_image(image_bytes)
    with Image.open(io.BytesIO(image_bytes)) as image:
        rgba = image.convert('RGBA')
    background = Image.new('RGBA', rgba.size, 'white')
    background.alpha_composite(rgba)
    image = background.convert('RGB')
    scale = min(3, bounds[0] / image.width, bounds[1] / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)
