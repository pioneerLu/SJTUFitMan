"""Validated local polling and mail settings. Secrets never appear in repr or output."""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path
import re
import tomllib

from booking_flow import BookingError, hourly_periods
from session import ROOT
from configuration import CONFIG_PATH, read_config, ConfigError

BEIJING = timezone(timedelta(hours=8), 'Asia/Shanghai')
MAX_DAILY_WINDOW_MINUTES = 24 * 60
MAX_WAITLIST_DURATION_MINUTES = 8 * 24 * 60
SETTINGS_PATH = CONFIG_PATH


class SettingsError(BookingError):
    pass


def beijing_now():
    return datetime.now(timezone.utc).astimezone(BEIJING)


def integer(values, key, default, minimum, maximum):
    result = values.get(key, default)
    if type(result) is not int or not minimum <= result <= maximum:
        raise SettingsError(f'{key}须为{minimum}～{maximum}之间的整数。')
    return result


def parse_stop_at(value):
    if not isinstance(value, str) or not value.strip():
        raise SettingsError('请选择截止日期和时间。')
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None: moment = moment.replace(tzinfo=BEIJING)
        return moment.astimezone(BEIJING)
    except (ValueError, OverflowError):
        raise SettingsError('截止日期和时间格式错误。') from None


def address(value):
    if not isinstance(value, str) or len(value) > 254 or not re.fullmatch(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value):
        raise SettingsError('请填写完整的发件人与收件人邮箱，不填写显示名称。')
    return value


def activity_name(values):
    activity = values.get('activity', '健身房')
    if not isinstance(activity, str) or not activity.strip() or len(activity) > 80 or any(ord(c) < 32 for c in activity):
        raise SettingsError('请从项目选项中选择预约项目。')
    return activity.strip()


@dataclass(frozen=True)
class MailSettings:
    smtp_host: str
    port: int
    security: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    sender: str = field(repr=False)
    recipient: str = field(repr=False)
    timeout_seconds: int = 15
    retry_interval_seconds: int = 60
    max_attempts: int = 3

    @classmethod
    def read(cls, values):
        host = values.get('smtp_host', '')
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', host) or host.startswith(('.', '-')):
            raise SettingsError('请填写SMTP服务器名称，不包含协议或端口。')
        security = values.get('security', 'ssl')
        if security not in ('ssl', 'starttls'):
            raise SettingsError('邮件只支持ssl或starttls加密连接。')
        username = values.get('username', '')
        if not isinstance(username, str) or not username.strip():
            raise SettingsError('请填写SMTP用户名。')
        password = values.get('password', '')
        environment = values.get('password_env', 'SJTU_SMTP_PASSWORD')
        if not isinstance(environment, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', environment):
            raise SettingsError('SMTP密码环境变量名称格式错误。')
        if not isinstance(password, str):
            raise SettingsError('SMTP授权码须填写为字符串。')
        password = password or os.environ.get(environment, '')
        if not password:
            raise SettingsError('请在本地填写SMTP授权码或设置对应环境变量。')
        return cls(host, integer(values, 'port', 465, 1, 65535), security,
            username, password, address(values.get('sender', '')), address(values.get('recipient', '')),
            integer(values, 'timeout_seconds', 15, 5, 30),
            integer(values, 'retry_interval_seconds', 60, 30, 600),
            integer(values, 'max_attempts', 3, 1, 5))


@dataclass(frozen=True)
class PollSettings:
    enabled: bool
    time_slots: tuple
    field: str
    interval_seconds: int = 60
    window_minutes: int = 15
    max_checks: int = 15
    manual_timeout_seconds: int = 300
    activity: str = '健身房'
    stop_mode: str = 'duration'
    stop_at: str = ''

    @classmethod
    def read(cls, config):
        values, booking = config.get('polling', {}), config.get('booking', {})
        enabled = values.get('enabled', False)
        if type(enabled) is not bool:
            raise SettingsError('polling.enabled须填写true或false。')
        slots = booking.get('time_slots', [])
        if not isinstance(slots, list) or not 1 <= len(slots) <= 15 or any(not isinstance(x, str) for x in slots):
            raise SettingsError('请在booking.time_slots填写至少一个目标时段。')
        for slot in slots:
            try: hourly_periods(slot)
            except BookingError:
                raise SettingsError('目标须为1小时或连续2小时，例如18:00-19:00或18:00-20:00。') from None
        if len(set(slots)) != len(slots):
            raise SettingsError('目标时段不能重复。')
        field_name = booking.get('field', '')
        if not isinstance(field_name, str) or len(field_name) > 80 or any(ord(c) < 32 for c in field_name):
            raise SettingsError('场地名称格式错误。')
        activity = activity_name(booking)
        stop_mode = values.get('stop_mode', 'duration')
        stop_at = values.get('stop_at', '')
        if stop_mode not in ('duration', 'deadline'):
            raise SettingsError('每日停止条件须为持续时长或指定截止时间。')
        if stop_mode == 'deadline': parse_stop_at(stop_at)
        elif not isinstance(stop_at, str): raise SettingsError('截止时间格式错误。')
        return cls(enabled, tuple(slots), field_name,
            integer(values, 'interval_seconds', 60, 20, 600),
            integer(values, 'window_minutes', 15, 1, MAX_DAILY_WINDOW_MINUTES),
            integer(values, 'max_checks', 15, 0, 4320),
            integer(values, 'manual_timeout_seconds', 300, 0, 300), activity=activity,
            stop_mode=stop_mode, stop_at=stop_at)

    def deadline(self):
        return parse_stop_at(self.stop_at) if self.stop_mode == 'deadline' else None

    def validate_start(self, current=None):
        current = (current or beijing_now()).astimezone(BEIJING)
        deadline = self.deadline()
        if deadline is not None and deadline <= max(current, self.next_release(current)):
            raise SettingsError('截止时间须晚于当前时间和下一次开抢时间。')

    def window(self, day):
        start = datetime.combine(day, time(12), BEIJING)
        if self.stop_mode == 'deadline':
            # Each day owns its target until the next noon; the absolute deadline
            # also prevents the scheduler from starting any later daily round.
            return start, min(self.deadline(), start + timedelta(days=1))
        return start, start + timedelta(minutes=self.window_minutes)

    def target_date(self, day):
        return (day + timedelta(days=7)).isoformat()

    def next_release(self, current):
        current = current.astimezone(BEIJING)
        start, end = self.window(current.date())
        return start if current < end else self.window(current.date()+timedelta(days=1))[0]

    def public(self, current=None):
        current = current or beijing_now()
        release = self.next_release(current)
        return {'enabled': self.enabled, 'timezone': 'Asia/Shanghai', 'release_at': release.isoformat(),
            'activity': self.activity,
            'target_date': self.target_date(release.date()), 'time_slots': list(self.time_slots),
            'field': self.field or '任一符合条件的场地',
            'interval_seconds': self.interval_seconds, 'window_minutes': self.window_minutes,
            'max_checks': self.max_checks, 'stop_mode': self.stop_mode,
            'stop_at': self.window(release.date())[1].isoformat()}


def load_settings(path=SETTINGS_PATH, require_enabled=False, require_mail=True):
    try:
        config=read_config(path)
    except ConfigError:
        raise SettingsError('本地预约配置无法读取，请检查config/config.toml格式。') from None
    settings = PollSettings.read(config)
    if require_enabled and not settings.enabled:
        raise SettingsError('每日预约尚未启用，请在本地将polling.enabled设为true。')
    return settings, MailSettings.read(config.get('mail', {})) if require_mail else None


def load_mail_settings(path=SETTINGS_PATH):
    """Allow mail setup/testing before any gym time slot has been chosen."""
    try:
        config=read_config(path)
    except ConfigError:
        raise SettingsError('本地邮件配置无法读取，请检查config/config.toml格式。') from None
    return MailSettings.read(config.get('mail',{}))


@dataclass(frozen=True)
class WaitlistSettings:
    date: str
    period: str
    field: str = ''
    start_mode: str = 'now'
    start_at: str = ''
    interval_seconds: int = 60
    manual_timeout_seconds: int = 300
    activity: str = '健身房'
    stop_mode: str = 'slot'
    duration_minutes: int = 60
    stop_at: str = ''

    @property
    def time_slots(self):
        return (self.period,)

    @classmethod
    def read(cls, config):
        values = config.get('waitlist', {})
        target, period = values.get('date', ''), values.get('time', '')
        try:
            if date.fromisoformat(target).isoformat() != target: raise ValueError()
        except (TypeError, ValueError):
            raise SettingsError('请在退选候补中选择预约日期。') from None
        try: hourly_periods(period)
        except (BookingError, TypeError):
            raise SettingsError('请在退选候补中选择1小时或连续2小时时段。') from None
        field_name = values.get('field', '')
        if not isinstance(field_name, str) or len(field_name) > 80 or any(ord(c) < 32 for c in field_name):
            raise SettingsError('场地名称格式错误。')
        mode, start_at = values.get('start_mode', 'now'), values.get('start_at', '')
        if mode not in ('now', 'scheduled') or not isinstance(start_at, str):
            raise SettingsError('候补启动方式须为立即开始或定时开始。')
        stop_mode, stop_at = values.get('stop_mode', 'slot'), values.get('stop_at', '')
        if stop_mode not in ('slot', 'duration', 'deadline'):
            raise SettingsError('候补停止条件须为目标时段开始、持续时长或指定截止时间。')
        if stop_mode == 'deadline': parse_stop_at(stop_at)
        elif not isinstance(stop_at, str): raise SettingsError('截止时间格式错误。')
        result = cls(target, period, field_name, mode, start_at,
            integer(values, 'interval_seconds', 60, 20, 600),
            integer(values, 'manual_timeout_seconds', 300, 0, 300), activity=activity_name(values),
            stop_mode=stop_mode,
            duration_minutes=integer(values, 'duration_minutes', 60, 1, MAX_WAITLIST_DURATION_MINUTES),
            stop_at=stop_at)
        if mode == 'scheduled':
            try: result.start_time()
            except ValueError:
                raise SettingsError('请从选项中选择候补开始日期和时间。') from None
            if result.start_time() >= result.end_time(result.start_time()):
                raise SettingsError('候补开始时间必须早于截止时间和目标时段开始时间。')
        return result

    def start_time(self, current=None):
        if self.start_mode == 'now': return current or beijing_now()
        moment = datetime.fromisoformat(self.start_at)
        if moment.tzinfo is None: moment = moment.replace(tzinfo=BEIJING)
        return moment.astimezone(BEIJING)

    def expires_at(self):
        # Both hours must still be bookable; stop when the first hour starts.
        return datetime.combine(date.fromisoformat(self.date), time.fromisoformat(self.period[:5]), BEIJING)

    def end_time(self, started_at):
        end = self.expires_at()
        if self.stop_mode == 'duration':
            end = min(end, started_at.astimezone(BEIJING) + timedelta(minutes=self.duration_minutes))
        elif self.stop_mode == 'deadline':
            end = min(end, parse_stop_at(self.stop_at))
        return end

    def validate_start(self, current=None):
        current = (current or beijing_now()).astimezone(BEIJING)
        target = date.fromisoformat(self.date)
        if not current.date() <= target <= current.date() + timedelta(days=7):
            raise SettingsError('候补日期须选择今天至七天后的日期。')
        if current >= self.expires_at():
            raise SettingsError('目标时段已经开始，不能继续候补。')
        if current >= self.end_time(self.start_time(current)):
            raise SettingsError('设定的截止时间已经到达，不能启动候补。')

    def public(self, current=None):
        return {'mode': 'waitlist', 'timezone': 'Asia/Shanghai',
            'activity': self.activity,
            'release_at': self.start_time(current).isoformat(), 'expires_at': self.expires_at().isoformat(),
            'target_date': self.date, 'time_slots': list(self.time_slots),
            'field': self.field or '任一符合条件的场地', 'interval_seconds': self.interval_seconds,
            'stop_mode': self.stop_mode, 'duration_minutes': self.duration_minutes,
            'stop_at': self.end_time(self.start_time(current)).isoformat()}


def load_waitlist_settings(path=SETTINGS_PATH):
    try:
        config=read_config(path)
    except ConfigError:
        raise SettingsError('本地预约配置无法读取，请检查config/config.toml格式。') from None
    return WaitlistSettings.read(config), MailSettings.read(config.get('mail', {}))
