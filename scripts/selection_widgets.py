"""Local selection controls shared by the booking windows."""
import tkinter as tk
from tkinter import ttk

from booking_options import GYM_TIME_SLOTS, period_choices
from booking_flow import BookingError, hourly_periods


class TimeSlotPicker(ttk.Frame):
    def __init__(self, parent, choices=GYM_TIME_SLOTS, multiple=True):
        super().__init__(parent)
        self.hour_choices = tuple(choices)
        self.duration = tk.StringVar(value='1小时')
        self.duration_choice = ttk.Combobox(self, textvariable=self.duration, values=('1小时', '连续2小时'), state='readonly', width=10)
        self.duration_choice.grid(row=0, column=0, sticky='w', padx=(0, 6))
        self.duration_choice.bind('<<ComboboxSelected>>', self.refresh_choices)
        self.selected = tk.StringVar(value='18:00-19:00')
        self.choice = ttk.Combobox(self, textvariable=self.selected, values=choices, state='readonly', width=18)
        self.choice.grid(row=0, column=1, sticky='ew', padx=(0, 6))
        self.add_button = ttk.Button(self, text='添加时段', command=self.add)
        self.add_button.grid(row=0, column=2, sticky='ew')
        self.listbox = tk.Listbox(self, height=4, exportselection=False, font=('Microsoft YaHei UI', 10))
        self.listbox.grid(row=1, column=0, columnspan=2, sticky='nsew', pady=(8, 0), padx=(0, 6))
        buttons = self.actions_frame = ttk.Frame(self)
        buttons.grid(row=1, column=2, sticky='ns', pady=(8, 0))
        self.actions = []
        for label, callback in [('上移', lambda: self.move(-1)), ('下移', lambda: self.move(1)), ('移除', self.remove)]:
            button = ttk.Button(buttons, text=label, command=callback, width=10)
            button.pack(fill='x', pady=1)
            self.actions.append(button)
        self.hint = tk.StringVar(value='从上到下尝试；连续2小时一起预约，每天最多创建一张订单。')
        self.hint_label = ttk.Label(self, textvariable=self.hint, foreground='#666666', wraplength=520)
        self.hint_label.grid(row=2, column=0, columnspan=3, sticky='w', pady=(6, 0))
        self.columnconfigure(1, weight=1)
        self.set_multiple(multiple)

    def set_multiple(self, value):
        self.multiple = bool(value)
        for widget in (self.add_button, self.listbox, self.actions_frame, self.hint_label):
            widget.grid() if self.multiple else widget.grid_remove()

    def refresh_choices(self, event=None):
        hours = 2 if self.duration.get() == '连续2小时' else 1
        choices = period_choices(self.hour_choices, hours)
        previous = self.selected.get()
        self.choice.configure(values=choices)
        if previous not in choices:
            matching = next((period for period in choices if period[:5] == previous[:5]), None)
            self.selected.set(matching or (choices[0] if choices else ''))

    def get(self):
        if not self.multiple:
            return [self.selected.get()] if self.selected.get() else []
        return list(self.listbox.get(0, 'end'))

    def set_hours(self, periods):
        """Replace a project's hours; retain only candidates from its current table."""
        previous = self.get()
        self.hour_choices = period_choices(periods)
        self.listbox.delete(0, 'end')
        for candidate in previous:
            try:
                if all(hour in self.hour_choices for hour in hourly_periods(candidate)):
                    self.listbox.insert('end', candidate)
            except BookingError:
                pass
        self.refresh_choices()
        return len(previous) - len(self.get())

    def set(self, periods):
        self.listbox.delete(0, 'end')
        # Preserve old locally configured periods instead of silently changing targets.
        choices = list(self.hour_choices)
        for period in periods:
            self.listbox.insert('end', period)
            try:
                for hour in hourly_periods(period):
                    if hour not in choices: choices.append(hour)
            except BookingError: pass
        self.hour_choices = tuple(sorted(choices))
        if periods:
            try: self.duration.set('连续2小时' if len(hourly_periods(periods[0])) == 2 else '1小时')
            except BookingError: self.duration.set('1小时')
            self.selected.set(periods[0])
        self.refresh_choices()

    def add(self):
        period = self.selected.get()
        if period not in self.choice.cget('values'):
            return
        if period in self.get():
            self.hint.set('这个时段已在列表中，可以用上移、下移调整顺序。')
            return
        if self.listbox.size() >= 15:
            self.hint.set('最多选择15个候选时段。')
            return
        self.listbox.insert('end', period)
        self.focus_row(self.listbox.size() - 1)
        self.hint.set('从上到下尝试；连续2小时一起预约，每天最多创建一张订单。')

    def focus_row(self, index):
        self.listbox.selection_clear(0, 'end')
        self.listbox.selection_set(index)
        self.listbox.activate(index)
        self.listbox.see(index)

    def move(self, offset):
        selected = self.listbox.curselection()
        if not selected:
            return
        index = selected[0]
        destination = index + offset
        if not 0 <= destination < self.listbox.size():
            return
        period = self.listbox.get(index)
        self.listbox.delete(index)
        self.listbox.insert(destination, period)
        self.focus_row(destination)

    def remove(self):
        selected = self.listbox.curselection()
        if selected:
            index = selected[0]
            self.listbox.delete(index)
            if self.listbox.size():
                self.focus_row(min(index, self.listbox.size() - 1))

    def set_busy(self, busy):
        self.duration_choice.configure(state='disabled' if busy else 'readonly')
        self.choice.configure(state='disabled' if busy else 'readonly')
        for widget in [self.add_button, self.listbox, *self.actions]:
            widget.configure(state='disabled' if busy else 'normal')
