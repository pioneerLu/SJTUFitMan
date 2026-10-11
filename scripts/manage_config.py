"""Initialize/check local configuration without websites, mail or secret output."""
import argparse
from configuration import ConfigError, ensure_config, read_config


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('init','check'),default='init',nargs='?')
    options=parser.parse_args(argv)
    ensure_config()
    if options.command=='check':
        data=read_config()
        if any(not isinstance(data.get(section),dict) for section in ('site','auth','polling','booking','waitlist','mail')):
            raise ConfigError('统一配置缺少必要的配置区块。')
    print('本地统一配置已准备好：config/config.toml。未访问网站、预约或发送邮件。')
    return 0


if __name__=='__main__':
    try: raise SystemExit(main())
    except ConfigError as error:
        print(str(error));raise SystemExit(2)
