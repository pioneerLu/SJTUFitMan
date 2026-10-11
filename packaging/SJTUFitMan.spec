# Build two small entry executables sharing one embedded runtime and dependency folder.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

root=Path(SPECPATH).parent
datas=[(str(root/'config/config.example.toml'),'config')]
datas += [(str(path),'scripts') for path in (root/'scripts').glob('*.js')]
datas += collect_data_files('ddddocr',includes=['*.onnx'])
datas += collect_data_files('onnxruntime')
binaries=collect_dynamic_libs('onnxruntime')

a=Analysis([str(root/'交大健身房助手.pyw')],pathex=[str(root/'scripts')],
           datas=datas,binaries=binaries,hiddenimports=['ddddocr','onnxruntime','cv2'],
           runtime_hooks=[str(root/'packaging/runtime_hook.py')],
           excludes=['ddddocr.api','ddddocr.__main__','pytest','unittest'],noarchive=False)
pyz=PYZ(a.pure)
options=[('X utf8',None,'OPTION')]
gui=EXE(pyz,a.scripts,[],options,exclude_binaries=True,name='交大健身房助手',
        debug=False,strip=False,upx=False,console=False,contents_directory='_internal')
worker=EXE(pyz,a.scripts,[],options,exclude_binaries=True,name='SJTUFitMan-worker',
           debug=False,strip=False,upx=False,console=True,contents_directory='_internal')
bundle=COLLECT(gui,worker,a.binaries,a.datas,strip=False,upx=False,name='SJTUFitMan')
