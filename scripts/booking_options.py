"""Selection values for the local UI; website options contain no account data."""
from datetime import date, timedelta
import re

from booking_flow import BookingError

WEEKDAYS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')
# Observed on the school's gym page. A fresh website query replaces these values.
GYM_TIME_SLOTS = tuple(f'{hour:02d}:00-{hour+1:02d}:00' for hour in range(7, 22))


def date_label(value):
    day = date.fromisoformat(value)
    return f'{day.isoformat()}（{WEEKDAYS[day.weekday()]}）'


def date_value(label):
    return date.fromisoformat(label[:10]).isoformat()


def local_dates(today):
    return tuple(date_label((today + timedelta(days=offset)).isoformat()) for offset in range(8))


def valid_period(value):
    return isinstance(value, str) and bool(re.fullmatch(r'(?:[01]\d|2[0-3]):00-(?:[01]\d|2[0-3]):00', value)) and int(value[6:8]) == int(value[:2]) + 1


def period_choices(periods, hours=1):
    if hours not in (1, 2):
        raise BookingError('预约时长须为1小时或连续2小时。')
    periods = sorted({period for period in periods if valid_period(period)})
    if hours == 1:
        return tuple(periods)
    choices = []
    for period in periods:
        start = int(period[:2])
        second = f'{start+1:02d}:00-{start+2:02d}:00'
        if second in periods:
            choices.append(f'{start:02d}:00-{start+2:02d}:00')
    return tuple(choices)


def website_options(snapshot, today):
    """Include full periods too: future polling needs all scheduled periods."""
    if not snapshot or snapshot.get('loading'):
        raise BookingError('网站预约选项尚未加载完成，请稍后同步。')
    dates = []
    for item in snapshot.get('dates', []):
        value = item.get('isdate')
        try:
            day = date.fromisoformat(value)
        except (ValueError, TypeError):
            continue
        if day >= today and value == day.isoformat():
            dates.append(value)
    dates = sorted(set(dates))
    periods = sorted({cell.get('time_range') for column in snapshot.get('slots', [])
        for cell in column.get('entries', []) if valid_period(cell.get('time_range'))})
    if not dates or not periods:
        raise BookingError('网站没有有效的日期或时段选项，请重新同步。')
    selected = snapshot.get('selected_date', {}).get('isdate')
    return {'activity': snapshot.get('active_type', {}).get('isactiveName'),
        'dates': dates, 'date': selected if selected in dates else dates[-1],
        'time_slots': periods, 'read_only': True, 'real_order_created': False}
