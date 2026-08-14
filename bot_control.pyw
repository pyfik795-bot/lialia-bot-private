# -*- coding: utf-8 -*-
"""Small no-console Crimson control panel for Lialia Bot on Windows."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

import bot_control_core as control


PROJECT_DIR = Path(__file__).resolve().parent
BG = "#08090d"
CARD = "#11141b"
CARD_HOVER = "#181c25"
CRIMSON = "#dc143c"
CRIMSON_HOVER = "#f12650"
TEXT = "#f4f5f7"
MUTED = "#8e96a3"
GREEN = "#32d583"
AMBER = "#f5b942"
BORDER = "#252a35"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def elevate_if_needed() -> bool:
    if os.name != "nt" or is_admin() or "--elevated" in sys.argv:
        return True
    parameters = subprocess.list2cmdline([str(Path(__file__).resolve()), "--elevated"])
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        parameters,
        str(PROJECT_DIR),
        1,
    )
    return result > 32


class BotControlApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Ляля Бот — управление")
        self.root.geometry("540x500")
        self.root.minsize(500, 470)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.busy = False
        self.refresh_job = None
        self._build()
        self._refresh_status()

    def _build(self):
        outer = tk.Frame(self.root, bg=BG, padx=28, pady=24)
        outer.pack(fill="both", expand=True)

        tk.Label(
            outer,
            text="ЛЯЛЯ БОТ",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 24),
        ).pack(anchor="w")
        tk.Label(
            outer,
            text="CRIMSON CONTROL CENTER",
            bg=BG,
            fg=CRIMSON,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", pady=(0, 20))

        status_card = tk.Frame(
            outer,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=18,
            pady=16,
        )
        status_card.pack(fill="x")
        status_row = tk.Frame(status_card, bg=CARD)
        status_row.pack(fill="x")
        self.dot = tk.Canvas(status_row, width=18, height=18, bg=CARD, highlightthickness=0)
        self.dot.pack(side="left")
        self.dot_id = self.dot.create_oval(4, 4, 14, 14, fill=AMBER, outline="")
        self.status_label = tk.Label(
            status_row,
            text="Проверяю состояние...",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI Semibold", 13),
        )
        self.status_label.pack(side="left", padx=(8, 0))
        self.detail_label = tk.Label(
            status_card,
            text="",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
        )
        self.detail_label.pack(fill="x", pady=(8, 0))

        actions = tk.Frame(outer, bg=BG)
        actions.pack(fill="x", pady=(20, 0))
        actions.columnconfigure((0, 1), weight=1)
        self.buttons = []
        self._button(actions, "▶  Запустить", self.start, 0, 0, primary=True)
        self._button(actions, "↻  Перезапустить", self.restart, 0, 1, primary=True)
        self._button(actions, "■  Остановить", self.stop, 1, 0)
        self._button(actions, "🌐  Открыть сайт", self.open_site, 1, 1)
        self._button(actions, "📁  Открыть логи", self.open_logs, 2, 0)
        self._button(actions, "⟳  Обновить статус", self._refresh_status, 2, 1)

        self.message_label = tk.Label(
            outer,
            text="",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            wraplength=470,
            justify="left",
        )
        self.message_label.pack(fill="x", pady=(18, 0))

        tk.Label(
            outer,
            text="Закрытие этого окна не останавливает бота",
            bg=BG,
            fg="#5f6672",
            font=("Segoe UI", 8),
        ).pack(side="bottom", pady=(18, 0))

    def _button(self, parent, text, command, row, column, *, primary=False):
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=CRIMSON if primary else CARD,
            activebackground=CRIMSON_HOVER if primary else CARD_HOVER,
            fg="white",
            activeforeground="white",
            disabledforeground="#707783",
            relief="flat",
            bd=0,
            cursor="hand2",
            font=("Segoe UI Semibold", 10),
            padx=12,
            pady=12,
        )
        button.grid(row=row, column=column, sticky="ew", padx=5, pady=5)
        self.buttons.append(button)

    def _set_busy(self, busy, message=""):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in self.buttons:
            button.configure(state=state)
        if message:
            self.message_label.configure(text=message, fg=AMBER if busy else MUTED)

    def _run_action(self, label, function):
        if self.busy:
            return
        self._set_busy(True, label)

        def worker():
            try:
                result = function()
            except Exception as error:
                self.root.after(0, self._action_failed, str(error))
            else:
                self.root.after(0, self._action_done, result)

        threading.Thread(target=worker, daemon=True).start()

    def _action_done(self, result):
        self._set_busy(False)
        self.message_label.configure(text=result, fg=GREEN)
        self._refresh_status()

    def _action_failed(self, error):
        self._set_busy(False)
        self.message_label.configure(text=error, fg=CRIMSON_HOVER)
        self._refresh_status()
        messagebox.showerror("Ляля Бот", error, parent=self.root)

    def _refresh_status(self):
        if self.busy:
            return
        if self.refresh_job is not None:
            self.root.after_cancel(self.refresh_job)
            self.refresh_job = None
        running = control.dashboard_is_ready()
        if running:
            self.dot.itemconfigure(self.dot_id, fill=GREEN)
            self.status_label.configure(text="Бот работает")
            self.detail_label.configure(text="Панель: http://localhost:8080\nАвтообновление GitHub включено")
        else:
            self.dot.itemconfigure(self.dot_id, fill=CRIMSON)
            self.status_label.configure(text="Бот остановлен")
            self.detail_label.configure(text="Порт 8080 свободен")
        self.refresh_job = self.root.after(2000, self._refresh_status)

    def start(self):
        self._run_action("Запускаю бота и проверяю GitHub...", lambda: control.start_bot(PROJECT_DIR))

    def restart(self):
        self._run_action("Перезапускаю бота...", lambda: control.restart_bot(PROJECT_DIR))

    def stop(self):
        self._run_action("Останавливаю бота...", control.stop_bot)

    @staticmethod
    def open_site():
        webbrowser.open("http://localhost:8080")

    @staticmethod
    def open_logs():
        log_dir = PROJECT_DIR / "logs"
        log_dir.mkdir(exist_ok=True)
        os.startfile(log_dir)

    def run(self):
        self.root.mainloop()


def main():
    if not elevate_if_needed():
        messagebox.showerror("Ляля Бот", "Windows не разрешила запуск от администратора")
        return
    if os.name == "nt" and not is_admin() and "--elevated" not in sys.argv:
        return
    BotControlApp().run()


if __name__ == "__main__":
    main()
