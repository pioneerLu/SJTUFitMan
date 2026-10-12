"""Check the three-page layout with blank fixture config; no website or SMTP actions."""
import argparse
import json
from pathlib import Path
import tempfile
import sys
import tkinter as tk
from app_ui import FitnessApp
from configuration import EXAMPLE_PATH
from runtime import ROOT


def inside_window(widget, window):
    return widget.winfo_ismapped() and widget.winfo_rooty() >= window.winfo_rooty() and (
        widget.winfo_rooty() + widget.winfo_height() <= window.winfo_rooty() + window.winfo_height()) and (
        widget.winfo_rootx() + widget.winfo_width() <= window.winfo_rootx() + window.winfo_width())


def screenshot_bounds(window):
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        # DWM returns physical pixels even when Windows scales the Tk coordinates.
        handle = ctypes.windll.user32.GetAncestor(window.winfo_id(), 2)
        rectangle = wintypes.RECT()
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(handle, 9, ctypes.byref(rectangle), ctypes.sizeof(rectangle))
        if result == 0: return rectangle.left, rectangle.top, rectangle.right, rectangle.bottom
    return window.winfo_rootx(), window.winfo_rooty(), window.winfo_rootx()+window.winfo_width(), window.winfo_rooty()+window.winfo_height()


def screenshot_window(window):
    """Render this window itself; never capture an app obscuring its screen rectangle."""
    if sys.platform != 'win32': raise RuntimeError('Window-only screenshots require Windows.')
    import ctypes
    from ctypes import wintypes
    from PIL import Image
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]; user.GetAncestor.restype = wintypes.HWND
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowDC.argtypes = [wintypes.HWND]; user.GetWindowDC.restype = wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]; gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]; gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]; gdi.SelectObject.restype = wintypes.HGDIOBJ
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]; gdi.DeleteDC.argtypes = [wintypes.HDC]
    gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    class Header(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('width', wintypes.LONG), ('height', wintypes.LONG),
            ('planes', wintypes.WORD), ('bits', wintypes.WORD), ('compression', wintypes.DWORD),
            ('image_size', wintypes.DWORD), ('x', wintypes.LONG), ('y', wintypes.LONG),
            ('colors', wintypes.DWORD), ('important', wintypes.DWORD)]
    handle = user.GetAncestor(window.winfo_id(), 2); rectangle = wintypes.RECT()
    if not user.GetWindowRect(handle, ctypes.byref(rectangle)): raise RuntimeError('Window bounds unavailable.')
    width, height = rectangle.right-rectangle.left, rectangle.bottom-rectangle.top
    source = user.GetWindowDC(handle); memory = gdi.CreateCompatibleDC(source)
    bitmap = gdi.CreateCompatibleBitmap(source, width, height); previous = gdi.SelectObject(memory, bitmap)
    try:
        if not user.PrintWindow(handle, memory, 2): raise RuntimeError('Window rendering failed.')
        gdi.SelectObject(memory, previous); previous = None
        header = Header(ctypes.sizeof(Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width*height*4)
        if gdi.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0) != height:
            raise RuntimeError('Window pixels unavailable.')
        return Image.frombytes('RGB', (width, height), pixels.raw, 'raw', 'BGRX')
    finally:
        if previous is not None: gdi.SelectObject(memory, previous)
        gdi.DeleteObject(bitmap); gdi.DeleteDC(memory); user.ReleaseDC(handle, source)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', action='store_true')
    options = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / 'config.toml'; config.write_bytes(EXAMPLE_PATH.read_bytes())
        window = tk.Tk(); window.attributes('-alpha', 1.0 if options.screenshots else 0.0)
        app = FitnessApp(window, start_workers=False, config_path=config, records_root=root)
        window.geometry('860x660+40+30')
        report = {}
        def inspect(name, buttons):
            window.update_idletasks(); window.update()
            visible = [button for button in buttons if button.winfo_ismapped()]
            fits = bool(visible) and all(inside_window(button, window) for button in visible)
            if name.startswith('auto-'):
                fits = fits and all(inside_window(label, window) and label.winfo_height() >= label.winfo_reqheight()
                    for label in (app.components['auto'].preview_label, app.components['auto'].authorization_label))
            report[name] = {'controls_fit': fits, 'visible_buttons': len(visible)}
            if options.screenshots:
                destination = ROOT / '.local/ui-preview'; destination.mkdir(parents=True, exist_ok=True)
                window.update()
                screenshot_window(window).save(destination / (name+'.png'))
        automatic = app.components['auto']
        inspect('auto-fixed', [*app.nav_buttons.values(), app.stop_button, *automatic.buttons])
        automatic.variables['stop_mode'].set(automatic.STOP_MODES['duration'])
        automatic.variables['duration_hours'].set('02'); automatic.variables['duration_minutes'].set('00')
        automatic.change_stop_mode()
        inspect('auto-fixed-duration', [*app.nav_buttons.values(), app.stop_button, *automatic.buttons,
            automatic.stop_choice, automatic.duration_hour, automatic.duration_minute])
        automatic.variables['stop_mode'].set(automatic.STOP_MODES['deadline']); automatic.change_stop_mode()
        inspect('auto-fixed-deadline', [*app.nav_buttons.values(), app.stop_button, *automatic.buttons,
            automatic.stop_choice, automatic.end_date, automatic.end_hour, automatic.end_minute])
        automatic.mode.set(automatic.MODES['daily']); automatic.change_mode()
        automatic.time_picker.set(['18:00-20:00', '19:00-20:00'])
        inspect('auto-daily', [*app.nav_buttons.values(), app.stop_button, *automatic.buttons])
        automatic.variables['duration_hours'].set('02'); automatic.variables['duration_minutes'].set('00')
        automatic.change_stop_mode()
        inspect('auto-daily-duration', [*app.nav_buttons.values(), app.stop_button, *automatic.buttons,
            automatic.stop_choice, automatic.duration_hour, automatic.duration_minute])
        automatic.variables['stop_mode'].set(automatic.STOP_MODES['deadline']); automatic.change_stop_mode()
        inspect('auto-daily-deadline', [*app.nav_buttons.values(), app.stop_button, *automatic.buttons,
            automatic.stop_choice, automatic.end_date, automatic.end_hour, automatic.end_minute])
        automatic.more.set(True); automatic.toggle_advanced()
        inspect('auto-advanced', [*app.nav_buttons.values(), app.stop_button, *automatic.buttons])
        app.show_page('records')
        records = app.components['records']
        inspect('records', [*app.nav_buttons.values(), app.stop_button, *records.buttons])
        app.show_page('settings')
        settings = app.components['settings']
        inspect('settings-login', [*app.nav_buttons.values(), app.stop_button, *settings.login.buttons, settings.login.browser_choice])
        from PIL import Image, ImageDraw, ImageFont
        import io
        sample = Image.new('RGB', (110, 40), 'white')
        try: font = ImageFont.truetype('arial.ttf', 22)
        except OSError: font = ImageFont.load_default(size=22)
        ImageDraw.Draw(sample).text((5, 6), 'aB39', fill='#137a49', font=font)
        stream = io.BytesIO(); sample.save(stream, format='PNG')
        settings.login.events.put(('image', stream.getvalue()))
        settings.login.events.put(('manual-captcha', None))
        window.after_cancel(settings.login.poll_id); settings.login.poll_events()
        captcha_buttons = [button for button in settings.login.buttons if button.master == settings.login.advanced_login or button.master.master == settings.login.advanced_login]
        inspect('settings-captcha', [*app.nav_buttons.values(), app.stop_button, *captcha_buttons,
            settings.login.image_label, settings.login.code_entry])
        report['settings-captcha']['full_image_visible'] = settings.login.image_label.winfo_height() >= settings.login.image.height()
        if not report['settings-captcha']['full_image_visible']: report['settings-captcha']['controls_fit'] = False
        settings.tabs.select(1)
        inspect('settings-mail', [*app.nav_buttons.values(), app.stop_button, *settings.mail.buttons])
        report['one_window'] = not any(isinstance(child, tk.Toplevel) for child in window.winfo_children())
        report['workers_started'] = bool(automatic.process or records.process or settings.login.worker or settings.mail.process or app.mail_service.thread)
        app.close()
        print(json.dumps(report))
        return 0 if all(value['controls_fit'] for value in report.values() if isinstance(value, dict)) and report['one_window'] and not report['workers_started'] else 2


if __name__ == '__main__': raise SystemExit(main())
