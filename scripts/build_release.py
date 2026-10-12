"""Build and check a clean Windows portable release without copying private data."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import zipfile

from check_release import check_release, secret_variants
from configuration import read_config
from runtime import ROOT
from version import VERSION
from browser_runtime import detect_browsers


def distribution_issues(directory, secrets=()):
    issues=[]
    directory=Path(directory)
    for path in directory.rglob('*'):
        relative=path.relative_to(directory)
        if '.local' in relative.parts or path.name=='config.toml':
            issues.append(relative.as_posix())
        elif path.is_symlink():
            issues.append(relative.as_posix())
        elif path.is_file() and secrets:
            content=path.read_bytes()
            if any(secret in content for secret in secrets): issues.append(relative.as_posix())
    return sorted(set(issues))


def command(arguments,directory,timeout=300):
    options={'cwd':directory,'stdin':subprocess.DEVNULL,'capture_output':True,'text':True,
             'encoding':'utf-8','errors':'replace','timeout':timeout}
    if os.name=='nt': options['creationflags']=subprocess.CREATE_NO_WINDOW
    return subprocess.run([str(arg) for arg in arguments],**options)


def has_shutdown_error(result):
    return any(marker in result.stderr for marker in (
        'Task was destroyed', 'Future exception was never retrieved', 'TargetClosedError'))


def dependency_notices(destination):
    names=[]
    for line in (ROOT/'requirements.txt').read_text(encoding='utf-8').splitlines():
        if '==' in line: names.append(line.split('==',1)[0])
    names.append('pyinstaller')
    lines=['本发布包包含下列第三方组件。版权与许可全文见licenses目录。',
           'PyInstaller引导程序适用其许可证及引导程序分发例外：',
           'https://pyinstaller.org/en/stable/license.html','']
    for name in names:
        package=importlib.metadata.distribution(name)
        lines.append(f'{package.metadata["Name"]} {package.version}')
        for record in package.files or ():
            if any(part.upper().startswith(('LICENSE','COPYING','NOTICE')) for part in record.parts):
                source=package.locate_file(record)
                if source.is_file():
                    target=destination/'licenses'/name/Path(str(record)).name
                    if target.exists(): target=target.with_name(target.stem+'-'+uuid.uuid4().hex[:8]+target.suffix)
                    target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(source,target)
    (destination/'THIRD_PARTY_NOTICES.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def build(skip_tests=False):
    if os.name!='nt' or sys.version_info[:2]!=(3,13):
        raise RuntimeError('请在Windows的Python 3.13项目环境中打包。')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.]+)?',VERSION):
        raise RuntimeError('版本号格式有误。')
    if os.environ.get('GITHUB_REF_TYPE')=='tag' and os.environ.get('GITHUB_REF_NAME')!=f'v{VERSION}':
        raise RuntimeError('发布标签须与scripts/version.py的版本一致。')
    importlib.metadata.version('PyInstaller')
    issues,_,_=check_release()
    if issues: raise RuntimeError('源代码发布检查未通过；请先运行scripts/check_release.py。')
    name=f'SJTUFitMan-v{VERSION}-windows-x64'
    target=ROOT/'dist'/name
    archive=ROOT/'dist'/f'{name}.zip'
    if target.exists() or archive.exists():
        raise RuntimeError('该版本的发布文件已存在，请先更新版本号；程序不会覆盖现有应用或用户设置。')
    work=ROOT/'build'/uuid.uuid4().hex;work.mkdir(parents=True)
    if not skip_tests:
        tested=command([sys.executable,'-m','unittest','discover','-s','tests','-p','test_*.py'],ROOT)
        (work/'tests.log').write_text(tested.stdout+tested.stderr,encoding='utf-8')
        if tested.returncode: raise RuntimeError(f'本地测试失败，日志位于{work.relative_to(ROOT)}/tests.log。')
    print('正在生成独立Windows程序，过程可能需要几分钟。',flush=True)
    with (work/'pyinstaller.log').open('w',encoding='utf-8') as log:
        result=subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean',
            '--distpath',str(work/'dist'),'--workpath',str(work/'work'),
            str(ROOT/'packaging/SJTUFitMan.spec')],cwd=ROOT,stdout=log,stderr=log)
    if result.returncode: raise RuntimeError(f'打包失败，日志位于{work.relative_to(ROOT)}/pyinstaller.log。')
    program=work/'dist/SJTUFitMan'
    print('正在验证发布版的OCR、浏览器驱动、界面和后台任务。',flush=True)
    checked=command([program/'SJTUFitMan-worker.exe','--worker','self-check'],program)
    (work/'smoke-worker.log').write_text(checked.stdout+checked.stderr,encoding='utf-8')
    if checked.returncode: raise RuntimeError(f'发布版自检失败，日志位于{work.relative_to(ROOT)}/smoke-worker.log。')
    if has_shutdown_error(checked): raise RuntimeError('发布版浏览器驱动未完整退出，请查看打包日志。')
    report=json.loads(checked.stdout.strip().splitlines()[-1])
    if not report.get('frozen'): raise RuntimeError('自检未使用内置运行环境。')
    report['browser_launch_checks']={}
    for kind in detect_browsers():
        launched=command([program/'SJTUFitMan-worker.exe','--worker','self-check','--browser',kind],program)
        (work/('smoke-browser-'+kind+'.log')).write_text(launched.stdout+launched.stderr,encoding='utf-8')
        if launched.returncode: raise RuntimeError('发布版浏览器启动检查未通过，请查看打包日志。')
        if has_shutdown_error(launched): raise RuntimeError('发布版浏览器未完整退出，请查看打包日志。')
        launch_report=json.loads(launched.stdout.strip().splitlines()[-1])
        if not launch_report.get('frozen') or launch_report.get('browser_check',{}).get('kind')!=kind:
            raise RuntimeError('发布版未使用指定浏览器，已停止打包。')
        report['browser_launch_checks'][kind]=launch_report['browser_check']
    for module in ('booking','poller'):
        help_result=command([program/'SJTUFitMan-worker.exe','--worker',module,'--help'],program)
        if help_result.returncode or not help_result.stdout: raise RuntimeError('发布版后台模块未能启动。')
    gui=command([program/'交大健身房助手.exe','--smoke-ui'],program)
    if gui.returncode or not (program/'smoke-ui.json').is_file():
        raise RuntimeError(f'发布版界面未通过验证，临时文件位于{work.relative_to(ROOT)}。')
    gui_report=json.loads((program/'smoke-ui.json').read_text(encoding='utf-8'))
    if not gui_report.get('all_pages') or gui_report.get('workers_started'):
        raise RuntimeError('发布版界面检查未通过。')
    (program/'smoke-ui.json').unlink()
    # Only the newly created blank smoke-test config is removed; private user configs are never copied.
    generated=program/'config/config.toml'
    if generated.exists(): generated.unlink()
    for local in program.rglob('.local'):
        if local.is_dir(): raise RuntimeError('发布包包含意外的运行记录，已停止生成。')
    shutil.copy2(ROOT/'config/config.example.toml',program/'config/config.example.toml')
    shutil.copy2(ROOT/'docs/RELEASE_START.txt',program/'使用说明.txt')
    shutil.copy2(ROOT/'README.md',program/'README.md')
    shutil.copytree(ROOT/'docs',program/'docs')
    dependency_notices(program)
    secrets=set()
    if (ROOT/'config/config.toml').exists(): secrets=secret_variants(read_config())
    leaked=distribution_issues(program,secrets)
    if leaked: raise RuntimeError('发布包隐私检查未通过，仅涉及文件名：'+', '.join(leaked))
    manifest={**report,'gui':gui_report,'source_commit':source_commit(),'source_dirty':source_dirty(),
              'distribution':'portable','private_config_included':False}
    (program/'release-info.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(program,target)
    temporary=archive.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as output:
            for path in sorted(target.rglob('*')):
                if path.is_file(): output.write(path,arcname=Path(name)/path.relative_to(target))
        os.replace(temporary,archive)
    finally:
        if temporary.exists(): temporary.unlink()
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum=archive.with_suffix('.zip.sha256')
    checksum.write_text(f'{digest}  {archive.name}\n',encoding='utf-8')
    print(f'已生成：{archive.relative_to(ROOT)}',flush=True)
    print(f'压缩包大小：{archive.stat().st_size/1024/1024:.1f} MB；SHA256校验文件已生成。')
    return 0


def source_commit():
    try:
        result=subprocess.run(['git','-C',str(ROOT),'rev-parse','--short','HEAD'],capture_output=True,text=True)
        return result.stdout.strip() if result.returncode==0 else None
    except OSError: return None


def source_dirty():
    try:
        result=subprocess.run(['git','-C',str(ROOT),'status','--porcelain'],capture_output=True)
        return bool(result.stdout) if result.returncode==0 else None
    except OSError: return None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-tests',action='store_true',help='For build repair after tests have already passed.')
    options=parser.parse_args()
    try: return build(options.skip_tests)
    except Exception as error:
        if isinstance(error,RuntimeError): print(str(error),file=sys.stderr)
        else: print('打包未完成，请检查打包环境和本地日志。',file=sys.stderr)
        return 1


if __name__=='__main__':
    if sys.stdout: sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr: sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
