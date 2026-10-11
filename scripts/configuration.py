"""One private TOML configuration with safe atomic updates."""
from datetime import date, datetime, time
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

from runtime import ROOT, asset_path

CONFIG_PATH=ROOT/'config/config.toml'
EXAMPLE_PATH=asset_path('config/config.example.toml')


class ConfigError(ValueError):
    pass


def _read(path):
    try:
        with Path(path).open('rb') as stream: return tomllib.load(stream)
    except (OSError,ValueError):
        raise ConfigError('本地配置无法读取，请检查config/config.toml格式。') from None


def _key(value):
    return value if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*',value) else json.dumps(value,ensure_ascii=False)


def dumps_config(data):
    """Serialize TOML without logging values; support nested tables and literal secrets."""
    def value(item):
        if isinstance(item,(datetime,date,time)): return item.isoformat()
        if isinstance(item,list): return '['+', '.join(value(part) for part in item)+']'
        return json.dumps(item,ensure_ascii=False,allow_nan=False)
    lines=[]
    def table(items,path):
        if path: lines.append('['+'.'.join(_key(part) for part in path)+']')
        for key,item in items.items():
            if not isinstance(item,dict): lines.append(_key(key)+' = '+value(item))
        lines.append('')
        for key,item in items.items():
            if isinstance(item,dict): table(item,(*path,key))
    try:
        table(data,())
        result='\n'.join(lines)
        tomllib.loads(result)
        return result
    except (TypeError,ValueError,AttributeError):
        raise ConfigError('配置未保存，请检查字段类型。') from None


def write_config(data,path=CONFIG_PATH):
    path=Path(path)
    content=dumps_config(data)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',newline='\n',
                dir=path.parent,prefix='.config-',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name)
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    except OSError:
        raise ConfigError('配置未保存，请检查本地文件权限。') from None
    finally:
        if temporary is not None and temporary.exists(): temporary.unlink()


def ensure_config(root=ROOT):
    """Create a personal copy of the example. Never overwrite an existing config."""
    root=Path(root);target=root/'config/config.toml'
    if target.exists():
        _read(target)
        return target
    template=root/'config/config.example.toml'
    data=_read(template if template.is_file() else EXAMPLE_PATH)
    write_config(data,target)
    return target


def read_config(path=None):
    path=Path(path) if path is not None else CONFIG_PATH
    if path==CONFIG_PATH and not path.exists(): ensure_config()
    return _read(path)


def update_config(changes,path=CONFIG_PATH):
    data=read_config(path)
    for section,items in changes.items(): data.setdefault(section,{}).update(items)
    write_config(data,path)
    return data
