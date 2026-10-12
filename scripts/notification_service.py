"""Process isolated mail recovery while the desktop app remains open; no website requests."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading

from configuration import CONFIG_PATH, read_config
from runtime import ROOT, worker_command


class NoticeService:
    def __init__(self, root=ROOT, config_path=CONFIG_PATH, enabled=True):
        self.root = Path(root)
        self.config_path = Path(config_path)
        self.enabled = enabled
        self.stop = threading.Event()
        self.thread = None
        self.report = {}

    def status_text(self):
        values = self.report
        if values.get('not_configured'):
            return '成功通知已保留，请完成邮件设置，后台会继续尝试发送。'
        if values.get('error'):
            return '通知队列暂未处理，请检查邮件设置和本地邮件记录。'
        failed = values.get('failed', 0)
        unknown = values.get('unknown', 0)
        pending = values.get('queued', 0) + values.get('retryable', 0)
        if failed or unknown:
            return f'成功通知：发送失败{failed}封，结果不明{unknown}封；按次数限制重试，可能重复，请检查邮箱。'
        if pending:
            return f'成功通知：{pending}封待发送，后台按邮件配置重试。'
        if values.get('sent'):
            return f"成功通知：{values['sent']}封已被邮件服务器接受，请检查收件箱。"
        return '预约成功后自动发送通知；后台重试需保持软件运行。'

    def has_work(self, current=None):
        current = current or datetime.now(timezone.utc)
        path = self.root / '.local/mail/outbox.json'
        records = json.loads(path.read_text(encoding='utf-8')).get('records', []) if path.exists() else []
        known = {item['key'] for item in records}
        try:
            max_attempts = read_config(self.config_path).get('mail', {}).get('max_attempts', 3)
            if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
                max_attempts = 3
        except Exception:
            max_attempts = 3
        self.report = {phase: sum(item['phase'] == phase for item in records)
            for phase in ('queued', 'retryable', 'sent', 'failed', 'unknown')}
        for item in records:
            if item['phase'] in ('connecting', 'sending'):
                return True
            if item['phase'] in ('queued', 'retryable', 'unknown') and item['attempts'] < max_attempts and (
                    not item.get('next_attempt_at') or current >= datetime.fromisoformat(item['next_attempt_at'])):
                return True
        journal_path = self.root / '.local/booking/journal.json'
        if journal_path.exists():
            entries = json.loads(journal_path.read_text(encoding='utf-8')).get('entries', [])
            for entry in entries:
                order = entry.get('order', {})
                if (entry.get('phase') == 'confirmed' and order.get('phase') == 'confirmed'
                        and order.get('real_order_created') is True and order.get('simulated') is False
                        and order.get('state') in ('0', '9', '1') and order.get('order_id')):
                    key = hashlib.sha256((entry['account'] + '|' + order['order_id']).encode()).hexdigest()
                    if key not in known:
                        return True
        return False

    def tick(self):
        if self.stop.is_set() or not self.has_work():
            return
        options = dict(cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=180)
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(worker_command('poller', '--settings', self.config_path, 'service-mail'), **options)
        if result.returncode:
            self.report = {'error': True}
        else:
            data = json.loads(result.stdout.strip())
            self.report = {key: value for key, value in data.items()
                if key in ('not_configured', 'retained', 'queued', 'retryable', 'sent', 'failed', 'unknown')
                and type(value) in (bool, int)}

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                self.report = {'error': True}
            self.stop.wait(30)

    def start(self):
        if self.enabled and self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        return self.thread is None or not self.thread.is_alive()
