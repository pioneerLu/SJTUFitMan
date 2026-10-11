"""Frozen GUI and console worker share modules, not a user's Python installation."""
import multiprocessing
import sys


def main(argv=None):
    multiprocessing.freeze_support()
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv[:1]==['--worker']:
        for stream in (sys.stdout,sys.stderr):
            if hasattr(stream,'reconfigure'): stream.reconfigure(encoding='utf-8')
        if len(argv)<2:
            print('缺少后台模块。',file=sys.stderr);return 2
        kind=argv[1]
        try:
            if kind=='booking':
                from booking import main as run
            elif kind=='poller':
                from poller import main as run
            elif kind=='self-check':
                from frozen_check import run
                return run(argv[2:])
            else:
                print('未知的后台模块。',file=sys.stderr);return 2
            return run(argv[2:])
        except KeyboardInterrupt:
            print('操作已停止。',file=sys.stderr);return 2
        except Exception as error:
            # Browser exceptions and configuration values must not leak to a console.
            if kind=='self-check':
                import json,traceback
                from pathlib import Path
                print(json.dumps({'error_type':type(error).__name__,
                    'locations':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                                 for frame in traceback.extract_tb(error.__traceback__)[-5:]]}),file=sys.stderr)
            from login_flow import FlowError
            if isinstance(error,FlowError):
                print(str(error),file=sys.stderr);return 2
            print('操作未完成，请检查本地配置、网络和程序文件。',file=sys.stderr);return 3
    if argv[:1]==['--smoke-ui']:
        try:
            from frozen_check import smoke_ui
            return smoke_ui()
        except Exception as error:
            import json,traceback
            from pathlib import Path
            from runtime import ROOT
            (ROOT/'smoke-ui.json').write_text(json.dumps({'all_pages':False,
                'error_type':type(error).__name__,
                'locations':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                             for frame in traceback.extract_tb(error.__traceback__)[-5:]]}),encoding='utf-8')
            return 3
    from app_ui import main as run_gui
    run_gui(argv)
    return 0
