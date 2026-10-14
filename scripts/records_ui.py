"""Reservation and notification history from local journals, without querying on page open."""
import hashlib
import json
from datetime import datetime
import tkinter as tk
from tkinter import ttk
import webbrowser

from booking_feedback import failure_detail
from desktop_worker import WorkerPage, json_result
from mail_notice import payment_notice_status
from runtime import ROOT
from poll_settings import BEIJING


class RecordsPage(WorkerPage):
    def __init__(self, window, parent, root=ROOT, **kwargs):
        super().__init__(window, parent, **kwargs)
        self.root = root
        self.rows = {}
        self.signature = None
        outer = ttk.Frame(parent, padding=20); outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='预约与通知记录', font=('Microsoft YaHei UI', 19, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='显示本机保存的提交结果和成功通知；订单当前状态请到体育平台核对。', wraplength=850).pack(anchor='w', pady=(6, 14))
        columns = ('date', 'activity', 'time', 'field', 'result', 'mail')
        self.table = ttk.Treeview(outer, columns=columns, show='headings', height=9, selectmode='browse')
        for key, label, width in zip(columns, ('日期', '项目', '时段', '场地', '提交结果', '邮件状态'), (115, 85, 120, 140, 115, 150)):
            self.table.heading(key, text=label); self.table.column(key, width=width, minwidth=60)
        self.table.pack(fill='x')
        self.table.bind('<<TreeviewSelect>>', self.select_record)
        controls = ttk.Frame(outer); controls.pack(fill='x', pady=12)
        self.buttons = []
        for label, action in [('刷新记录', lambda: self.refresh(force=True)), ('核查异常订单', self.reconcile),
                              ('重试未发出的邮件', lambda: self.launch('poller', 'retry-mail')),
                              ('打开体育平台', lambda: webbrowser.open('https://sports.sjtu.edu.cn/pc/'))]:
            button = ttk.Button(controls, text=label, command=action)
            button.pack(side='left', padx=(0, 10)); self.buttons.append(button)
        ttk.Label(outer, textvariable=self.status, wraplength=850, foreground='#215c4b').pack(anchor='w', pady=6)
        self.details = tk.Text(outer, height=10, wrap='word', font=('Microsoft YaHei UI', 10), state='disabled')
        self.details.pack(fill='both', expand=True, pady=(6, 0))
        self.refresh(force=True)

    def read_json(self, path, default):
        if not path.exists(): return default
        return json.loads(path.read_text(encoding='utf-8'))

    def refresh(self, force=False):
        paths = [self.root / '.local/booking/journal.json', self.root / '.local/mail/outbox.json']
        try:
            signature = tuple(path.stat().st_mtime_ns if path.exists() else None for path in paths)
            if not force and signature == self.signature: return
            entries = self.read_json(paths[0], {}).get('entries', [])
            notices = {item['key']: item for item in self.read_json(paths[1], {}).get('records', [])}
            rows = {}
            for index, entry in enumerate(entries):
                order = entry.get('order') or {}
                slot = order.get('slot') or entry.get('slot') or {}
                identifier = order.get('order_id') or entry.get('order_id') or ''
                key = hashlib.sha256((entry.get('account', '') + '|' + identifier).encode()).hexdigest()
                notice = notices.get(key)
                phase = entry.get('phase', 'unknown')
                result_label = {'confirmed': '预约已核实成功', 'rejected': '网站拒绝', 'unknown': '需核查',
                    'submitting': '提交待核查', 'accepted': '提交待核查', 'verification_required': '等待验证码',
                    'profile_required': '需更新资料', 'auth_required': '需重新登录'}.get(phase, phase)
                current = entry.get('current_order') or {}
                if phase == 'confirmed':
                    result_label = current.get('status') or order.get('status') or (
                        '待付款' if order.get('state') in ('0', '9') else '已支付' if order.get('state') == '1' else result_label)
                mail_phase = notice['phase'] if notice else None
                mail_label = {'sent': '服务器已接受', 'queued': '待发送', 'retryable': '等待重试', 'connecting': '连接中',
                    'sending': '发送中', 'failed': '发送失败', 'unknown': '结果不明'}.get(mail_phase, '待后台补发' if phase == 'confirmed' else '—')
                row_id = str(index)
                rows[row_id] = {'entry': entry, 'order': order, 'notice': notice, 'result_label': result_label,
                    'values': tuple(slot.get(key, '') for key in ('date', 'activity', 'time', 'field')) + (result_label, mail_label)}
            selected = self.table.selection()
            for row_id in self.table.get_children():
                if row_id not in rows: self.table.delete(row_id)
            for position, (row_id, row) in enumerate(reversed(list(rows.items()))):
                if self.table.exists(row_id): self.table.item(row_id, values=row['values'])
                else: self.table.insert('', 'end', iid=row_id, values=row['values'])
                self.table.move(row_id, '', position)
            self.rows = rows; self.signature = signature
            if selected and selected[0] in rows: self.select_record()
            elif rows:
                self.table.selection_set(next(reversed(rows))); self.select_record()
            else: self.show('暂无本机预约记录。启动自动预约后，订单和通知状态会显示在这里。')
        except (OSError, ValueError, KeyError, TypeError, AttributeError): self.status.set('部分本机记录无法读取，请检查记录文件格式。')

    def show(self, text):
        self.details.configure(state='normal'); self.details.delete('1.0', 'end'); self.details.insert('1.0', text); self.details.configure(state='disabled')

    def select_record(self, event=None):
        selected = self.table.selection()
        if not selected or selected[0] not in self.rows: return
        row = self.rows[selected[0]]
        entry, order, notice = row['entry'], row['order'], row['notice']
        slot = order.get('slot') or entry.get('slot') or {}
        lines = [row['result_label'], f"{slot.get('venue_name', '')} · {slot.get('activity', '')}",
            f"{slot.get('date', '')} {slot.get('time', '')}", f"场地：{slot.get('field', '')}"]
        if order.get('order_id'): lines.append('订单号：' + order['order_id'])
        if order.get('status'): lines.append('确认时订单状态：' + order['status'])
        current = entry.get('current_order') or {}
        if current.get('status'):
            lines.append('最近核查状态：' + current['status'])
            if current.get('released'):
                lines.append('旧订单已失效，可在“自动预约”重新启动相同时段的候补。')
        if order.get('confirmed_at'):
            try: lines.append('核实时间：' + datetime.fromisoformat(order['confirmed_at']).astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S（北京时间）'))
            except (ValueError, TypeError): pass
        detail = failure_detail(entry)
        if detail: lines += ['', detail]
        if entry.get('phase') == 'confirmed':
            lines += ['', '邮件状态：' + payment_notice_status(notice.get('phase') if notice else None)]
        lines += ['', '请在体育平台个人订单核对当前状态并自行付款。有效订单不会重复创建；取消或过期后需重新启动预约。']
        self.show('\n'.join(lines))

    def reconcile(self):
        if self.launch('booking', 'reconcile'): self.status.set('正在核查已有订单，不创建新订单。')

    def set_busy(self, busy):
        super().set_busy(busy)
        for button in self.buttons[1:3]: button.configure(state='disabled' if busy else 'normal')

    def update_running(self): self.refresh()

    def on_result(self, command, code, output, error):
        if code: self.status.set(error.strip() or '操作未完成，请检查登录状态和本机记录。')
        elif command == 'reconcile':
            try:
                results = json_result(output)['reconciliation']
                messages = [result['message'] for result in results if result.get('message')]
                self.status.set('；'.join(messages) or ('订单核查完成。' if results else '没有待核查的提交记录。'))
            except (ValueError, TypeError, KeyError): self.status.set('核查已结束，请查看订单记录。')
        else: self.status.set('邮件队列已处理，请查看记录中的发送状态。')
        self.refresh(force=True)
