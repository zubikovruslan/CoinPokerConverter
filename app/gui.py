"""Russian desktop UI; a worker thread never calls Tk directly."""
from __future__ import annotations
import copy
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .engine import Cancelled, DEFAULT_RULES, Options, app_data_dir, convert, money
from .storage import atomic_json, export_history, import_history, load_settings

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    Root = TkinterDnD.Tk
except ImportError:
    DND_FILES = None
    Root = tk.Tk


class Application:
    def __init__(self, root=None, data_dir=None):
        self.root = root or Root()
        self.data_dir = Path(data_dir) if data_dir else app_data_dir()
        self.settings_file = self.data_dir / "settings.json"
        self.history_file = self.data_dir / "history.sqlite"
        self.load_warning = ""
        try: self.settings = load_settings(self.settings_file)
        except (OSError, ValueError, KeyError) as error:
            self.settings = {"schema": 1, "last_hero": "", "profiles": {}}
            self.load_warning = f"Не удалось прочитать настройки: {error}. Исходный файл не изменён."
        self.paths = []
        self.rules = copy.deepcopy(DEFAULT_RULES)
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.last_archive = None
        self.result = None
        self.hero = tk.StringVar(value=self.settings.get("last_hero", ""))
        self.mode = tk.StringVar(value="all")
        self.destination = tk.StringVar(value=str(Path.home() / "Documents" / "CoinPokerConverted"))
        self.status = tk.StringVar(value="Добавьте историю CoinPoker в TXT или ZIP.")
        self.file_count = tk.StringVar(value="Файлы не выбраны")
        self.metrics = [tk.StringVar(value="—") for _ in range(3)]
        self.root.title(f"CoinPoker → H2N3  •  {__version__}")
        icon = Path(__file__).resolve().parent.parent / "app.ico"
        if sys.platform == "win32" and icon.exists(): self.root.iconbitmap(str(icon))
        self.root.geometry("1000x800")
        self.root.minsize(860, 690)
        self.root.configure(bg="#f2f5f7")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self._build()
        self.load_profile()
        self.poll_after = self.root.after(100, self.poll)
        if self.load_warning: self.root.after(250, lambda: messagebox.showwarning("Настройки", self.load_warning, parent=self.root))

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background="#f2f5f7", foreground="#20313e")
        style.configure("TFrame", background="#f2f5f7")
        style.configure("Card.TFrame", background="white")
        style.configure("Card.TLabel", background="white")
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"))
        style.configure("Muted.TLabel", foreground="#586976")
        style.configure("Metric.TLabel", font=("Segoe UI", 22, "bold"), background="white")
        style.configure("TButton", padding=(12, 8))
        style.configure("Accent.TButton", background="#147d70", foreground="white", font=("Segoe UI", 11, "bold"), padding=(18, 11))
        style.map("Accent.TButton", background=[("active", "#0d6359"), ("disabled", "#8da49f")])
        style.configure("TNotebook.Tab", padding=(18, 9))
        style.configure("Treeview", rowheight=29, background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _build(self):
        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="CoinPoker → H2N3", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Конвертация на вашем компьютере  ·  Без регистрации  ·  NLH", style="Muted.TLabel").pack(anchor="w", pady=(3, 16))
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.convert_page = ttk.Frame(self.notebook, padding=16)
        self.rules_page = ttk.Frame(self.notebook, padding=16)
        self.help_page = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.convert_page, text="Конвертация")
        self.notebook.add(self.rules_page, text="Распределение Splash")
        self.notebook.add(self.help_page, text="История и справка")
        page = self.convert_page
        page.columnconfigure(1, weight=1)
        page.rowconfigure(3, weight=1)
        ttk.Label(page, text="Ник Hero").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.hero_box = ttk.Combobox(page, textvariable=self.hero, values=list(self.settings["profiles"]))
        self.hero_box.grid(row=0, column=1, sticky="ew")
        self.hero_box.bind("<<ComboboxSelected>>", lambda event: self.load_profile())
        ttk.Button(page, text="Сохранить профиль", command=self.save_profile).grid(row=0, column=2, padx=(12, 0))
        modes = ttk.Frame(page)
        modes.grid(row=1, column=0, columnspan=3, sticky="w", pady=(13, 10))
        ttk.Radiobutton(modes, text="Вся загруженная история", value="all", variable=self.mode).pack(side="left", padx=(0, 24))
        ttk.Radiobutton(modes, text="Только новые руки", value="new", variable=self.mode).pack(side="left")
        toolbar = ttk.Frame(page)
        toolbar.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 7))
        ttk.Button(toolbar, text="+ Добавить TXT / ZIP", command=self.add_dialog).pack(side="left")
        ttk.Button(toolbar, text="Убрать выбранные", command=self.remove_selected).pack(side="left", padx=8)
        ttk.Label(toolbar, textvariable=self.file_count, style="Muted.TLabel").pack(side="right")
        files = ttk.Frame(page)
        files.grid(row=3, column=0, columnspan=3, sticky="nsew")
        self.file_list = tk.Listbox(files, height=6, selectmode="extended", font=("Segoe UI", 10), relief="flat", borderwidth=1, highlightbackground="#d9e2e7", highlightthickness=1, bg="white", selectbackground="#147d70", activestyle="none")
        self.file_list.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(files, command=self.file_list.yview)
        scroll.pack(side="right", fill="y")
        self.file_list.config(yscrollcommand=scroll.set)
        if DND_FILES and hasattr(self.file_list, "drop_target_register"):
            self.file_list.drop_target_register(DND_FILES)
            self.file_list.dnd_bind("<<Drop>>", lambda event: self.add_paths(self.root.tk.splitlist(event.data)))
        ttk.Label(page, text="Можно добавить несколько файлов или перетащить их в список.", style="Muted.TLabel").grid(row=4, column=0, columnspan=3, sticky="w", pady=(5, 12))
        ttk.Label(page, text="Папка результата").grid(row=5, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(page, textvariable=self.destination).grid(row=5, column=1, sticky="ew")
        ttk.Button(page, text="Выбрать…", command=self.choose_destination).grid(row=5, column=2, padx=(12, 0), sticky="ew")
        stats = ttk.Frame(page)
        stats.grid(row=6, column=0, columnspan=3, sticky="ew", pady=16)
        for i, title in enumerate(["Обычные руки", "Splash-руки", "Исключённые записи"]):
            stats.columnconfigure(i, weight=1)
            card = ttk.Frame(stats, style="Card.TFrame", padding=(16, 10))
            card.grid(row=0, column=i, sticky="ew", padx=(0, 8) if i < 2 else 0)
            ttk.Label(card, textvariable=self.metrics[i], style="Metric.TLabel").pack(anchor="w")
            ttk.Label(card, text=title, style="Card.TLabel").pack(anchor="w")
        ttk.Label(page, text="Общая доля Splash — в отдельном отчёте. EV и начальный банк Splash в H2N3 не подтверждены.", wraplength=840, style="Muted.TLabel").grid(row=7, column=0, columnspan=3, sticky="w")
        self.progress_bar = ttk.Progressbar(page, mode="determinate")
        self.progress_bar.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(14, 6))
        ttk.Label(page, textvariable=self.status, wraplength=830).grid(row=9, column=0, columnspan=3, sticky="w")
        actions = ttk.Frame(page)
        actions.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(15, 0))
        self.start_button = ttk.Button(actions, text="Сконвертировать в ZIP", command=self.start, style="Accent.TButton")
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="Отменить", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(actions, text="Открыть папку результата", command=self.open_output, state="disabled")
        self.open_button.pack(side="right")
        self._build_rules()
        self._build_help()

    def _build_rules(self):
        page = self.rules_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1, minsize=60)
        title = ttk.Label(page, text="Общая доля достаётся всем участникам, включая победителя.", font=("Segoe UI", 11, "bold"), wraplength=780)
        title.grid(row=0, column=0, sticky="w")
        intro = ttk.Label(page, text="Остальное разыгрывается в банке. 60% / 40% при 6 игроках означает 10% каждому и ещё 40% победителю.\nSPLASH dropped в исходнике уже содержит разыгрываемую долю.", wraplength=780, style="Muted.TLabel")
        intro.grid(row=1, column=0, sticky="ew", pady=(6, 12))
        tree_frame = ttk.Frame(page)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        self.rule_table = ttk.Treeview(tree_frame, columns=("bb", "shared", "pot", "since"), show="headings", height=5, selectmode="browse")
        for key, label in [("bb", "Большой блайнд"), ("shared", "Всем, %"), ("pot", "В банк, %"), ("since", "Действует с (MSK)")]:
            self.rule_table.heading(key, text=label)
            self.rule_table.column(key, width=150, anchor="center")
        self.rule_table.pack(side="left", fill="both", expand=True)
        tree_scroll = ttk.Scrollbar(tree_frame, command=self.rule_table.yview)
        tree_scroll.pack(side="right", fill="y")
        self.rule_table.configure(yscrollcommand=tree_scroll.set)
        self.rule_table.bind("<<TreeviewSelect>>", self.select_rule)
        edit = ttk.Frame(page)
        edit.grid(row=3, column=0, sticky="ew", pady=12)
        self.rule_bb = tk.StringVar(value="0.10")
        self.rule_shared = tk.StringVar(value="60")
        self.rule_since = tk.StringVar(value="1900-01-01")
        for i, (label, var) in enumerate([("BB, например 0.10", self.rule_bb), ("Общая доля, %", self.rule_shared), ("С даты ГГГГ-ММ-ДД", self.rule_since)]):
            cell = ttk.Frame(edit)
            cell.pack(side="left", padx=(0, 16))
            ttk.Label(cell, text=label).pack(anchor="w")
            ttk.Entry(cell, textvariable=var, width=20).pack(pady=(5, 0))
        row = ttk.Frame(page)
        row.grid(row=4, column=0, sticky="ew")
        ttk.Button(row, text="Добавить / обновить", command=self.upsert_rule).pack(side="left")
        ttk.Button(row, text="Удалить выбранное", command=self.delete_rule).pack(side="left", padx=8)
        ttk.Button(row, text="Стандартные правила", command=self.default_rules).pack(side="right")
        note = ttk.Label(page, text="Дата 1900-01-01 означает применение ко всей истории. При смене условий добавьте строку с новой датой.\nПри 100% общей доле Splash остаётся в исключениях: полный размер награды неизвестен.", wraplength=780, style="Muted.TLabel")
        note.grid(row=5, column=0, sticky="ew", pady=12)
        buttons = ttk.Frame(page)
        buttons.grid(row=6, column=0, sticky="ew")
        ttk.Button(buttons, text="Сохранить профиль", command=self.save_profile).pack(side="left")
        ttk.Button(buttons, text="Экспорт настроек…", command=self.export_rules).pack(side="left", padx=8)
        ttk.Button(buttons, text="Импорт настроек…", command=self.import_rules).pack(side="left")
        page.bind("<Configure>", lambda event: [label.configure(wraplength=max(300, event.width-32)) for label in (title, intro, note)])

    def _build_help(self):
        page = self.help_page
        text = tk.Text(page, wrap="word", font=("Segoe UI", 11), bg="white", relief="flat", padx=18, pady=16, height=16)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "КАК ИМПОРТИРОВАТЬ\n\nРаспакуйте ZIP и выберите в H2N3 файлы Import/Regular.txt и Import/Splash.txt. Пустой файл можно пропустить. Выберите свой ник в руме PS. Контрольные итоги находятся в README.txt.\n\nТОЛЬКО НОВЫЕ РУКИ\n\nПриложение помнит успешно выгруженные ID для каждого ника на этом компьютере. Это история созданных файлов, а не подтверждение импорта в H2N3. Исключённые руки можно обработать повторно. Для другого компьютера перенесите историю кнопками ниже.\n\nSPLASH И СПОРНЫЕ РУКИ\n\nОбщая доля Splash рассчитывается отдельно, в график H2N3 не попадает. Фактические округления и зачисления неизвестны. EV и начальный внешний банк Splash в H2N3 не подтверждены. Три борда, Cashout Hero и неподтверждённые выплаты идут в Excluded с причиной.\n\nБЕЗОПАСНОСТЬ ДАННЫХ\n\nИсходники не изменяются. Раздачи и ник не отправляются в интернет. В приложении нет регистрации, платёжных функций и телеметрии. USD используется как метка без пересчёта курса. Имена соперников остаются анонимными, если так записаны в источнике.\n\nПЕРЕИМПОРТ\n\nНовый файл с прежним ID обычно не заменяет старую руку в H2N3. Для изменённой конвертации используйте полную выгрузку и отдельную базу. Не удаляйте рабочую базу до проверки.\n\nПОДДЕРЖИВАЕМЫЙ ВХОД\n\nТекстовые истории CoinPoker NLH (TXT или ZIP с TXT), UTF-8, денежные суммы до двух знаков. До 256 МиБ на TXT / 512 МиБ на запуск, до 250 000 записей. Неизвестный формат не исправляется догадками.\n\nНастройки и история: " + str(self.data_dir))
        text.configure(state="disabled")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(14, 0))
        ttk.Button(row, text="Экспорт истории выгрузок…", command=self.export_history).pack(side="left")
        ttk.Button(row, text="Импорт истории выгрузок…", command=self.import_history).pack(side="left", padx=8)
        ttk.Label(page, text="Переносится история всех сохранённых ников. Исходные раздачи в файл истории не входят.", style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

    def idle_only(self):
        return not self.busy

    def add_paths(self, paths):
        if self.busy: return
        bad = []
        for path in paths:
            p = Path(path).resolve()
            if p.is_file() and p.suffix.lower() in (".txt", ".zip"):
                if p not in self.paths: self.paths.append(p)
            else: bad.append(p.name)
        self.file_list.delete(0, "end")
        for p in self.paths: self.file_list.insert("end", str(p))
        self.file_count.set(f"Файлов: {len(self.paths)}")
        if bad: messagebox.showwarning("Файлы", "Нужны TXT или ZIP с исходной историей:\n" + "\n".join(bad[:10]), parent=self.root)

    def add_dialog(self):
        if not self.busy: self.add_paths(filedialog.askopenfilenames(parent=self.root, title="История CoinPoker", filetypes=[("CoinPoker TXT / ZIP", "*.txt *.zip")]))

    def remove_selected(self):
        if self.busy: return
        for i in reversed(self.file_list.curselection()): del self.paths[i]
        self.add_paths([])

    def choose_destination(self):
        if self.busy: return
        value = filedialog.askdirectory(parent=self.root, title="Папка готовых архивов")
        if value: self.destination.set(value)

    def refresh_rules(self):
        self.rule_table.delete(*self.rule_table.get_children())
        for i, rule in enumerate(self.rules):
            self.rule_table.insert("", "end", iid=str(i), values=(money(rule["bb_cents"]), rule["shared_percent"], 100-rule["shared_percent"], rule["since"]))

    def select_rule(self, event=None):
        selected = self.rule_table.selection()
        if selected:
            rule = self.rules[int(selected[0])]
            self.rule_bb.set(money(rule["bb_cents"]))
            self.rule_shared.set(str(rule["shared_percent"]))
            self.rule_since.set(rule["since"])

    def upsert_rule(self):
        if self.busy: return
        try:
            import re
            raw = self.rule_bb.get().strip().replace(",", ".")
            if not re.fullmatch(r"\d+(?:\.\d{1,2})?", raw): raise ValueError("BB: положительная сумма с максимум двумя знаками после точки")
            whole, _, fraction = raw.partition(".")
            rule = {"bb_cents": int(whole)*100 + int((fraction+"00")[:2]), "shared_percent": int(self.rule_shared.get()), "since": self.rule_since.get().strip()}
            Options("Hero", rules=[rule]).validate()
            rules = [r for r in self.rules if (r["bb_cents"], r["since"]) != (rule["bb_cents"], rule["since"])] + [rule]
            self.rules = sorted(rules, key=lambda r: (r["bb_cents"], r["since"]))
            self.refresh_rules()
        except ValueError as error: messagebox.showerror("Правило Splash", str(error), parent=self.root)

    def delete_rule(self):
        if not self.busy and self.rule_table.selection():
            del self.rules[int(self.rule_table.selection()[0])]
            self.refresh_rules()

    def default_rules(self):
        if self.busy: return
        self.rules = copy.deepcopy(DEFAULT_RULES)
        self.refresh_rules()

    def load_profile(self):
        if self.busy: return
        profile = self.settings["profiles"].get(self.hero.get(), {})
        self.rules = copy.deepcopy(profile.get("rules", DEFAULT_RULES))
        self.mode.set(profile.get("mode", "all"))
        if profile.get("output_dir"): self.destination.set(profile["output_dir"])
        self.refresh_rules()

    def save_profile(self, show=True):
        if self.busy: return False
        try:
            Options(self.hero.get().strip(), self.mode.get(), self.rules).validate()
            if self.load_warning: raise ValueError("Сначала восстановите повреждённый settings.json; приложение не будет молча перезаписывать его.")
            hero = self.hero.get().strip()
            self.hero.set(hero)
            self.settings["profiles"][hero] = {"rules": self.rules, "mode": self.mode.get(), "output_dir": self.destination.get()}
            self.settings["last_hero"] = hero
            atomic_json(self.settings_file, self.settings)
            self.hero_box.configure(values=list(self.settings["profiles"]))
            if show: self.status.set(f"Профиль {hero} сохранён.")
            return True
        except (OSError, ValueError) as error:
            messagebox.showerror("Профиль", str(error), parent=self.root)
            return False

    def export_rules(self):
        if self.busy: return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json", initialfile="Splash_settings.json", filetypes=[("JSON", "*.json")])
        if path:
            try: atomic_json(Path(path), {"schema": 1, "rules": self.rules})
            except OSError as error: messagebox.showerror("Настройки", str(error), parent=self.root)

    def import_rules(self):
        if self.busy: return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("JSON", "*.json")])
        if path:
            try:
                if Path(path).stat().st_size > 1_000_000: raise ValueError("Файл настроек слишком большой")
                data = json.loads(Path(path).read_text("utf-8-sig"))
                if not isinstance(data, dict) or data.get("schema") != 1: raise ValueError("Неизвестная версия настроек")
                Options("Hero", rules=data["rules"]).validate()
                self.rules = data["rules"]
                self.refresh_rules()
            except (OSError, ValueError, KeyError, TypeError) as error: messagebox.showerror("Настройки", str(error), parent=self.root)

    def export_history(self):
        if self.busy: return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json", initialfile="CoinPoker_export_history.json", filetypes=[("JSON", "*.json")])
        if path:
            try:
                n = export_history(self.history_file, Path(path))
                messagebox.showinfo("История", f"Сохранено записей: {n}", parent=self.root)
            except Exception as error: messagebox.showerror("История", str(error), parent=self.root)

    def import_history(self):
        if self.busy: return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("JSON", "*.json")])
        if path:
            try:
                n = import_history(self.history_file, Path(path))
                messagebox.showinfo("История", f"Добавлено записей: {n}", parent=self.root)
            except Exception as error: messagebox.showerror("История", str(error), parent=self.root)

    def start(self):
        if self.busy: return
        if not self.paths:
            messagebox.showinfo("История раздач", "Добавьте исходный TXT или ZIP CoinPoker.", parent=self.root); return
        if not self.destination.get().strip():
            messagebox.showinfo("Папка результата", "Укажите папку результата.", parent=self.root); return
        if not self.save_profile(show=False): return
        options = Options(self.hero.get(), self.mode.get(), copy.deepcopy(self.rules))
        paths, destination = list(self.paths), Path(self.destination.get()).expanduser()
        self.busy = True
        self.cancel_event.clear()
        self.result = None
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.hero_box.configure(state="disabled")
        self.notebook.tab(1, state="disabled")
        self.notebook.tab(2, state="disabled")
        for var in self.metrics: var.set("—")
        self.progress_bar.configure(value=0)
        self.status.set("Начинаю проверку…")
        def worker():
            try:
                result = convert(paths, destination, options, self.history_file,
                                 progress=lambda text, current, total: self.events.put(("progress", (text, current, total))),
                                 cancel=self.cancel_event.is_set)
                self.events.put(("done", result))
            except Cancelled: self.events.put(("cancelled", None))
            except Exception as error: self.events.put(("error", f"{type(error).__name__}: {error}"))
        threading.Thread(target=worker, name="converter", daemon=True).start()

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.set("Отмена… Ожидаю завершения текущего шага.")

    def poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "progress":
                    text, current, total = data
                    self.status.set(text + (f" {current:,} / {total:,}" if total else ""))
                    self.progress_bar.configure(value=100*current/total if total else 0)
                else:
                    self.busy = False
                    self.start_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.hero_box.configure(state="normal")
                    self.notebook.tab(1, state="normal")
                    self.notebook.tab(2, state="normal")
                    if kind == "done":
                        self.result = data
                        self.last_archive = Path(data["archive"])
                        for var, key in zip(self.metrics, ["regular_hands", "splash_hands", "excluded_records"]): var.set(f"{data[key]:,}")
                        net = sum(data["totals"].get(k, {}).get("net_cents", 0) for k in ("regular", "splash"))
                        self.status.set(f"ZIP сохранён. Результат Hero за столом: {money(net)}. Уже выгруженных пропущено: {data['previously_exported_skipped']}.")
                        self.open_button.configure(state="normal")
                        self.progress_bar.configure(value=100)
                        if data.get("history_warning"): messagebox.showwarning("История выгрузок", data["history_warning"], parent=self.root)
                    elif kind == "cancelled":
                        self.status.set("Отменено. Итоговый архив не создан, история выгрузок не обновлена.")
                        self.progress_bar.configure(value=0)
                    else:
                        self.status.set("Конвертация не завершена. " + data)
                        messagebox.showerror("Конвертация", data, parent=self.root)
        except queue.Empty: pass
        self.poll_after = self.root.after(100, self.poll)

    def open_output(self):
        if self.last_archive:
            if sys.platform == "win32": os.startfile(str(self.last_archive.parent))
            elif sys.platform == "darwin": subprocess.Popen(["open", str(self.last_archive.parent)])
            else: subprocess.Popen(["xdg-open", str(self.last_archive.parent)])

    def close(self):
        if self.busy:
            self.cancel()
            self.status.set("Отменяю обработку. Закройте окно после завершения отмены.")
        else:
            self.root.after_cancel(self.poll_after)
            self.root.destroy()


def main():
    app = Application()
    app.root.mainloop()


if __name__ == "__main__": main()
