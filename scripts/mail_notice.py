"""TLS SMTP payment reminders with a durable, deduplicated outbox."""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
import json
from pathlib import Path
import smtplib
import ssl

from booking_flow import BookingError, ProcessLock, ORDER_ID, hourly_periods
from login_flow import atomic_json
from poll_settings import BEIJING
from session import ROOT

OUTBOX = ROOT / '.local/mail/outbox.json'


class MailFailure(Exception):
    def __init__(self, state):
        self.state = state
        super().__init__('邮件未完成发送，请检查本地邮件状态。')


def smtp_send(settings, message, before_data):
    client = None
    data_started = False
    try:
        context = ssl.create_default_context()
        if settings.security == 'ssl':
            client = smtplib.SMTP_SSL(settings.smtp_host, settings.port,
                timeout=settings.timeout_seconds, context=context)
        elif settings.security == 'starttls':
            client = smtplib.SMTP(settings.smtp_host, settings.port, timeout=settings.timeout_seconds)
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        else:
            raise MailFailure('failed')
        client.login(settings.username, settings.password)
        before_data()
        data_started = True
        refused = client.send_message(message, from_addr=settings.sender, to_addrs=[settings.recipient])
        if refused:
            raise MailFailure('failed')
        # DATA was accepted. A QUIT/disconnect error cannot change that outcome.
        return True
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused, smtplib.SMTPNotSupportedError, ssl.SSLError):
        raise MailFailure('failed') from None
    except smtplib.SMTPResponseException as error:
        raise MailFailure('retryable' if 400 <= error.smtp_code < 500 else 'failed') from None
    except MailFailure:
        raise
    except Exception:
        raise MailFailure('unknown' if data_started else 'retryable') from None
    finally:
        if client is not None:
            try: client.close()
            except Exception: pass


def payment_message(settings, record):
    order = record['order']
    slot = order['slot']
    message = EmailMessage()
    message['From'] = settings.sender
    message['To'] = settings.recipient
    message['Subject'] = f"健身房预约已确认，请付款 · {slot['date']} {slot['time']}"
    message['Message-ID'] = record['message_id']
    message['Date'] = format_datetime(datetime.fromisoformat(record['created_at']))
    confirmed = datetime.fromisoformat(order['confirmed_at']).astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S')
    message.set_content(f"以下预约订单在确认时为待付款状态，程序已停止此次预约的余量查询。\n\n"
        f"场馆：{slot['venue_name']}\n项目：{slot['activity']}\n日期：{slot['date']}\n"
        f"时段：{slot['time']}\n时长：{len(hourly_periods(slot['time']))}小时\n场地：{slot['field']}\n应付金额：{order['amount_due']}元\n"
        f"订单号：{order['order_id']}\n确认时间：{confirmed}（北京时间）\n\n"
        "请立即打开交大体育平台的“个人订单”核对当前状态并付款，截止时间以平台显示为准。\n"
        "https://sports.sjtu.edu.cn/pc/\n\n程序没有自动付款。\n")
    return message


class Outbox:
    def __init__(self, path=OUTBOX, clock=None, sender=smtp_send):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sender = sender
        self.records = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('schema_version') != 1 or not isinstance(data.get('records'), list):
                raise BookingError('邮件记录格式错误，已停止发送。')
            self.records = data['records']
        changed = False
        for item in self.records:
            if item['phase'] == 'sending':
                item['phase'] = 'unknown'
                changed = True
            elif item['phase'] == 'connecting':
                item['phase'] = 'retryable'
                changed = True
        if changed: self.save()

    def save(self):
        atomic_json(self.path, {'schema_version': 1, 'records': self.records})

    def enqueue(self, account, order, settings):
        if (order.get('phase') != 'confirmed' or order.get('simulated') is not False
                or order.get('real_order_created') is not True or order.get('state') not in ('0','9')
                or not ORDER_ID.fullmatch(order.get('order_id', ''))):
            raise BookingError('仅为已核实的真实待付款订单发送提醒。')
        key = hashlib.sha256((account + '|' + order['order_id']).encode()).hexdigest()
        previous = next((r for r in self.records if r['key'] == key), None)
        if previous: return previous
        item = {'key': key, 'account': account, 'phase': 'queued', 'attempts': 0, 'order': order,
            'created_at': self.clock().isoformat(), 'next_attempt_at': self.clock().isoformat(),
            'message_id': f"<{key}@{settings.sender.split('@',1)[1]}>"}
        self.records.append(item)
        self.save()
        return item

    def deliver(self, item, settings):
        if item['phase'] not in ('queued', 'retryable') or item['attempts'] >= settings.max_attempts:
            return item['phase']
        if self.clock() < datetime.fromisoformat(item['next_attempt_at']):
            return item['phase']
        message = payment_message(settings, item)
        item.update(phase='connecting', attempts=item['attempts']+1,
            next_attempt_at=(self.clock()+timedelta(seconds=settings.retry_interval_seconds)).isoformat())
        self.save()
        def durable_send():
            item['phase'] = 'sending'
            self.save()
        try:
            if self.sender(settings, message, durable_send) is not True:
                raise MailFailure('unknown' if item['phase']=='sending' else 'retryable')
            item.update(phase='sent', sent_at=self.clock().isoformat())
        except MailFailure as error:
            item['phase'] = error.state
        except Exception:
            item['phase'] = 'unknown' if item['phase']=='sending' else 'retryable'
        if item['phase']=='retryable' and item['attempts']>=settings.max_attempts:
            item.update(phase='failed',reason='attempt_limit')
        self.save()
        return item['phase']


def service_outbox(settings, account=None, clock=None):
    with ProcessLock(ROOT / '.local/mail/send.lock'):
        outbox = Outbox(clock=clock)
        for record in outbox.records:
            if account is None or record['account']==account:
                outbox.deliver(record, settings)
        return {phase: sum(x['phase']==phase for x in outbox.records)
            for phase in ('queued','retryable','sent','failed','unknown')}


def send_test_mail(settings):
    message=EmailMessage()
    message['From']=settings.sender;message['To']=settings.recipient
    message['Subject']='交大健身房 · 邮件提醒测试'
    message.set_content('这是本机预约助手发送的测试邮件。没有进行预约或付款。')
    smtp_send(settings, message, lambda:None)
