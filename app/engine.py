"""Offline CoinPoker NLH conversion, exact cents, conservative exclusions."""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import itertools
import json
import os
import re
import sqlite3
import tempfile
import uuid
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Callable

from . import __version__
from . import legacy_core as core
from .splash_renderer import render_ps_splash_candidate

MAX_FILE = 256 * 1024 * 1024
MAX_BATCH = 512 * 1024 * 1024
MAX_HANDS = 250_000
MAX_HAND_BYTES = 256 * 1024
DEFAULT_RULES = [
    {"bb_cents": bb, "shared_percent": shared, "since": "1900-01-01"}
    for bb, shared in [(2, 0), (5, 0), (10, 60), (25, 60), (50, 60)]
]

REASONS = {
    "format": "Неподдерживаемый или повреждённый формат раздачи",
    "hero": "Не удалось однозначно определить Hero или ник совпал с соперником",
    "duplicate_conflict": "Один ID содержит разные исходные раздачи; все варианты исключены",
    "actions": "Некорректная последовательность действий или стеков в источнике",
    "cards": "Некорректные карты, борды или номера мест",
    "pot": "Банк не равен взносам игроков и разыгрываемому Splash",
    "payout": "Выплаты, рейк и Splash не согласуются с банком",
    "hero_cashout": "EV Cashout самого Hero пока не поддержан",
    "cashout_conflict": "Противоречивые записи EV Cashout одного игрока",
    "cashout_evidence": "Для EV Cashout соперника не подтверждён итог Hero по следующей раздаче",
    "three_boards": "Три борда: нет проверенного представления в H2N3",
    "splash_allocation": "Нельзя однозначно распределить Splash по победителям и бордам",
    "splash_rule": "Нет действующего правила Splash для этого лимита и даты",
    "zero_contested": "При 100% общей доле нельзя восстановить сундук из SPLASH dropped",
    "changed_export": "ID уже выгружался с другим содержимым; требуется полный экспорт в отдельную базу",
    "render": "Проверка совместимости перед записью не пройдена",
}


class Cancelled(Exception):
    pass


class Rejected(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(REASONS.get(code, code) + (": " + detail if detail else ""))


@dataclass
class Options:
    hero: str
    mode: str = "all"
    rules: list[dict] = field(default_factory=lambda: copy.deepcopy(DEFAULT_RULES))

    def validate(self):
        if not isinstance(self.hero, str) or not re.fullmatch(r"[^\s:\[\]()'\x00-\x1f\x7f]{1,50}", self.hero):
            raise ValueError("Ник: от 1 до 50 символов, без пробелов, скобок, двоеточия и управляющих символов.")
        if self.mode not in ("all", "new"):
            raise ValueError("Неизвестный режим конвертации")
        if not isinstance(self.rules, list) or len(self.rules) > 1000:
            raise ValueError("Правила Splash должны быть списком не более 1000 строк.")
        seen = set()
        for row in self.rules:
            if not isinstance(row, dict): raise ValueError("Правило Splash должно быть объектом с лимитом, долей и датой.")
            if type(row.get("bb_cents")) is not int or row["bb_cents"] <= 0:
                raise ValueError("Большой блайнд должен быть целым положительным числом центов.")
            if type(row.get("shared_percent")) is not int or not 0 <= row["shared_percent"] <= 100:
                raise ValueError("Общая доля должна быть целым числом от 0 до 100%.")
            date = row.get("since", "")
            if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError("Дата правила: ГГГГ-ММ-ДД")
            datetime.strptime(date, "%Y-%m-%d")
            key = (row["bb_cents"], date)
            if key in seen:
                raise ValueError("Два правила для одного лимита с одинаковой датой.")
            seen.add(key)
        return self


def digest(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def money(n: int) -> str:
    return ("-" if n < 0 else "") + f"{abs(n) // 100}.{abs(n) % 100:02d}"


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "CoinPokerConverter"


def rename_player(text: str, old: str, new: str) -> str:
    """Replace player fields only; never table names, descriptions or substrings."""
    name = re.escape(old)
    patterns = [rf"^(Seat \d+: ){name}(?= )", rf"^(Dealt to ){name}(?= |$)",
                rf"^(){name}(?=: | collected | cashed out )",
                rf"^(Uncalled bet \([^\n]+\) returned to ){name}$"]
    for pattern in patterns:
        text = re.sub(pattern, lambda m: m[1] + new, text, flags=re.M)
    return text


def normalize_hero(text: str, hero: str) -> str:
    players = re.findall(r"^Seat \d+: (\S+) \(₮", text, re.M)
    if "Hero" in players:
        if hero != "Hero" and hero in players:
            raise Rejected("hero", "Ник уже занят другим игроком")
        return text
    if hero in players:
        return rename_player(text, hero, "Hero")
    raise Rejected("hero", "В источнике нет ни Hero, ни введённого ника")


def parse(text: str, hero: str) -> dict:
    normalized = normalize_hero(text, hero)
    raw_seats = re.findall(r"^Seat (\d+): (\S+) \(₮", normalized, re.M)
    if len({seat for seat, _ in raw_seats}) != len(raw_seats) or len({name for _, name in raw_seats}) != len(raw_seats):
        raise Rejected("cards", "Повторяющиеся места или имена игроков")
    try:
        h = core.parse_hand(normalized)
    except (ValueError, AssertionError, KeyError, IndexError, TypeError) as error:
        raise Rejected("format", str(error)[:250]) from error
    h["original_text"] = text
    if h["bb"] <= 0 or h["sb"] <= 0 or h["sb"] > h["bb"]:
        raise Rejected("format", "Блайнды")
    seats = [p["seat"] for p in h["players"].values()]
    if len(set(seats)) != len(seats) or any(not 1 <= s <= h["max_seats"] for s in seats):
        raise Rejected("cards", "Номера мест")
    if not 1 <= h["button"] <= h["max_seats"]:
        raise Rejected("cards", "Баттон")
    known = list(itertools.chain.from_iterable(h["known_cards"].values()))
    if any(len(cards) != 2 for cards in h["known_cards"].values()):
        raise Rejected("cards", "Карманные карты")
    if any(not re.fullmatch(r"[2-9TJQKA][cdhs]", card) for card in known):
        raise Rejected("cards", "Неизвестные карманные карты")
    for board in h["boards"].values():
        cards = known + board
        if len(board) not in (0, 3, 4, 5) or len(cards) != len(set(cards)) or any(
            not re.fullmatch(r"[2-9TJQKA][cdhs]", card) for card in cards
        ):
            raise Rejected("cards", "Повторяющиеся или неизвестные карты")
    if len(known) != len(set(known)):
        raise Rejected("cards", "Повторяющиеся карманные карты")
    # Preserve exact duplicate cashout semantics; contradictory values are unsafe.
    seen = {}
    events = []
    folded_so_far = set()
    for e in h["events"]:
        p = e.get("player")
        if p is not None and p not in h["players"]:
            raise Rejected("format", "Действие неизвестного игрока")
        if p in folded_so_far and e["kind"] in ("fold", "check", "call", "bet", "raise", "straddle"):
            raise Rejected("actions", "Игровое действие после fold")
        if e["kind"] == "fold": folded_so_far.add(p)
        if e["kind"] == "collect" and p in h["folded"]:
            raise Rejected("actions", "Выплата игроку, который сбросил карты")
        if e["kind"] == "street" and e["stage"] in ("FLOP", "TURN", "RIVER"):
            visible = list(itertools.chain.from_iterable(e["cards"]))
            count = {"FLOP": 3, "TURN": 4, "RIVER": 5}[e["stage"]]
            board = h["boards"].get(e["run"], [])
            if len(visible) != count or visible != board[:count]:
                raise Rejected("cards", "Карты улицы не совпадают с итоговым бордом")
        if e["kind"] == "cashout":
            if p in seen:
                if e != seen[p]:
                    raise Rejected("cashout_conflict")
                continue
            seen[p] = e
        events.append(e)
    h["events"] = events
    remaining = set(h["players"]) - set(h["folded"])
    # Equal-contribution HU showdown has no side-pot ambiguity. Do not apply this
    # shortcut to unequal contributions or to hidden opponent cards.
    if len(remaining) == 2 and remaining <= h["known_cards"].keys() and len({h["contributed"].get(p, 0) for p in remaining}) == 1:
        for run, board in h["boards"].items():
            if len(board) != 5: continue
            ranks = {p: rank_seven(h["known_cards"][p] + board) for p in remaining}
            best = max(ranks.values())
            for e in events:
                if e["kind"] == "collect" and e["run"] == run and e["amount"] > 0 and ranks.get(e["player"]) != best:
                    raise Rejected("payout", "Получатель банка проиграл на известном HU-борде")
    return h


def rank_five(cards):
    values = sorted(("23456789TJQKA".index(c[0]) + 2 for c in cards), reverse=True)
    groups = sorted(((count, value) for value, count in Counter(values).items()), reverse=True)
    unique = sorted(set(values), reverse=True)
    straight = (5 if unique == [14, 5, 4, 3, 2] else unique[0]) if len(unique) == 5 and (unique[0] - unique[-1] == 4 or unique == [14, 5, 4, 3, 2]) else 0
    flush = len({c[1] for c in cards}) == 1
    if straight and flush: return (8, straight)
    if groups[0][0] == 4: return (7, groups[0][1], groups[1][1])
    if [g[0] for g in groups] == [3, 2]: return (6, groups[0][1], groups[1][1])
    if flush: return (5, *values)
    if straight: return (4, straight)
    if groups[0][0] == 3: return (3, groups[0][1], *sorted((v for n, v in groups[1:]), reverse=True))
    if [g[0] for g in groups[:2]] == [2, 2]: return (2, max(groups[0][1], groups[1][1]), min(groups[0][1], groups[1][1]), groups[2][1])
    if groups[0][0] == 2: return (1, groups[0][1], *sorted((v for n, v in groups[1:]), reverse=True))
    return (0, *values)


def rank_seven(cards):
    return max(rank_five(c) for c in itertools.combinations(cards, 5))


def reconstruct_splash(h: dict) -> tuple[dict, list]:
    q = copy.deepcopy(h)
    groups = defaultdict(list)
    for i, e in enumerate(q["events"]):
        if e["kind"] == "collect":
            groups[e["run"]].append(i)
    runs = sorted(q["boards"])
    if runs not in ([1], [1, 2]) or sorted(groups) != runs or q["splash"] % len(runs):
        raise Rejected("splash_allocation", "Борды или дробный цент")
    additions = []
    for run in runs:
        recipients = sorted({q["events"][i]["player"] for i in groups[run]})
        if not 1 <= len(recipients) <= 2 or (len(runs) == 2 and len(recipients) != 1):
            raise Rejected("splash_allocation", "Сложное распределение")
        if len(recipients) > 1:
            board = q["boards"][run]
            if len(board) != 5 or any(p not in q["known_cards"] for p in recipients):
                raise Rejected("splash_allocation", "Не подтверждено равенство комбинаций")
            ranks = {rank_seven(q["known_cards"][p] + board) for p in recipients}
            if len(ranks) != 1:
                raise Rejected("splash_allocation", "Разные победители основного и побочного банков")
        per_run = q["splash"] // len(runs)
        if per_run % len(recipients):
            raise Rejected("splash_allocation", "Неизвестное округление разыгрываемой доли")
        for p in recipients:
            if p in q["folded"]:
                raise Rejected("splash_allocation", "Победитель сбросил карты")
            i = next(i for i in groups[run] if q["events"][i]["player"] == p)
            amount = per_run // len(recipients)
            q["events"][i]["amount"] += amount
            q["collected"][p] += amount
            additions.append({"player": p, "run": run, "added_cents": amount})
    q["total_collected"] = sum(q["collected"].values())
    q["payout_difference"] = q["pot"] - q["total_collected"] - q["rake"] - q["fee"]
    q["hero_net"] = q["collected"].get("Hero", 0) - q["contributed"].get("Hero", 0)
    if q["payout_difference"] or sum(a["added_cents"] for a in additions) != h["splash"]:
        raise Rejected("payout")
    return q, additions


def verify_cashout(h: dict, q: dict, next_text: str | None, hero: str):
    if next_text is None or len(q["boards"]) > 1:
        raise Rejected("cashout_evidence", "Нет следующей раздачи или несколько бордов")
    try:
        n = parse(next_text, hero)
        end = re.search(r"^Game ended: (.*?) MSK$", h["text"], re.M)
        if not end:
            raise ValueError("Нет времени окончания")
        delta = (datetime.strptime(n["date"], "%Y/%m/%d %H:%M:%S") - datetime.strptime(end[1], "%Y/%m/%d %H:%M:%S")).total_seconds()
        valid = (n["id"] == str(int(h["id"]) + 1) and n["table"] == h["table"] and
                 n["sb"] == h["sb"] and n["bb"] == h["bb"] and
                 n["players"]["Hero"]["seat"] == h["players"]["Hero"]["seat"] and
                 0 <= delta <= 120 and n["players"]["Hero"]["stack"] == h["players"]["Hero"]["stack"] + q["hero_net"])
        if not valid:
            raise ValueError("ID, стол, время или стек не совпали")
    except (ValueError, KeyError, Rejected) as error:
        raise Rejected("cashout_evidence", str(error)[:200]) from error
    q["cashout_hero_result_verified"] = True


def applicable_rule(h: dict, rules: list) -> dict:
    date = h["date"][:10].replace("/", "-")
    candidates = [r for r in rules if r["bb_cents"] == h["bb"] and r["since"] <= date]
    if not candidates:
        raise Rejected("splash_rule")
    rule = max(candidates, key=lambda r: r["since"])
    if rule["shared_percent"] == 100:
        raise Rejected("zero_contested")
    return rule


def convert_one(h: dict, next_text: str | None, options: Options):
    if "Hero" in h["cashouts"]: raise Rejected("hero_cashout")
    if "run3" in h["features"] or 3 in h["boards"]: raise Rejected("three_boards")
    if "splash" in h["features"] and h["splash"] == 0:
        applicable_rule(h, options.rules)
        raise Rejected("splash_allocation", "Нулевая разыгрываемая награда; полная сумма неизвестна")
    if h["errors"]: raise Rejected("actions", repr(h["errors"])[:250])
    if h["pot_difference"]: raise Rejected("pot", money(h["pot_difference"]))
    if h["payout_difference"] != h["splash"]: raise Rejected("payout", money(h["payout_difference"] - h["splash"]))
    q, additions = (reconstruct_splash(h) if h["splash"] else (h, []))
    if h["splash"]:
        applicable_rule(h, options.rules)
    if h["cashouts"] and h["splash"]:
        verify_cashout(h, q, next_text, options.hero)
    try:
        rendered = render_ps_splash_candidate(q) if h["splash"] else core.convert_hand(q)
    except (AssertionError, ValueError, KeyError, IndexError) as error:
        raise Rejected("render", str(error)[:250]) from error
    return rename_player(rendered, "Hero", options.hero), q, additions


def shared_row(h: dict, options: Options, status: str) -> dict:
    row = {"hand_id": h["id"], "date_msk": h["date"], "bb_cents": h["bb"],
           "players": len(h["players"]), "contested_splash_cents": h["splash"],
           "status": status, "actual_wallet_cents": "", "note": "Расчётная доля; фактическое зачисление неизвестно"}
    try:
        rule = applicable_rule(h, options.rules)
        shared = Fraction(h["splash"] * rule["shared_percent"], 100 - rule["shared_percent"])
        personal = shared / len(h["players"])
        row.update(shared_percent=rule["shared_percent"], rule_since=rule["since"],
                   full_chest_cents_fraction=str(shared + h["splash"]), shared_total_cents_fraction=str(shared),
                   hero_shared_cents_fraction=str(personal), fractional_cent=personal.denominator != 1)
    except Rejected as error:
        row["note"] = str(error)
    return row


def text_sources(paths: list[Path], cancel: Callable):
    total = 0
    for path in paths:
        if cancel(): raise Cancelled()
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as z:
                members = [i for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith(".txt")]
                if not members:
                    raise ValueError(f"В архиве нет TXT: {path.name}")
                for info in members:
                    total += info.file_size
                    if info.file_size > MAX_FILE or total > MAX_BATCH:
                        raise ValueError("Слишком большой вход: максимум 256 МиБ на TXT и 512 МиБ на запуск.")
                    if info.flag_bits & 1:
                        raise ValueError("Зашифрованные ZIP не поддержаны.")
                    if cancel(): raise Cancelled()
                    with z.open(info) as source:
                        raw = source.read(MAX_FILE + 1)
                    if len(raw) > MAX_FILE: raise ValueError("TXT превышает допустимый размер")
                    yield f"{path.name}/{info.filename}", raw
        elif path.suffix.lower() == ".txt":
            size = path.stat().st_size
            total += size
            if size > MAX_FILE or total > MAX_BATCH:
                raise ValueError("Слишком большой вход: максимум 256 МиБ на TXT и 512 МиБ на запуск.")
            with path.open("rb") as source: raw = source.read(MAX_FILE + 1)
            if len(raw) > MAX_FILE: raise ValueError("TXT превышает допустимый размер")
            yield path.name, raw
        else:
            raise ValueError(f"Выберите исходный TXT или ZIP: {path.name}")


def csv_text(rows: list[dict], fields: list[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fields, extrasaction="ignore", delimiter=";")
    writer.writeheader()
    for row in rows:
        # User-controlled fields must not become formulas in spreadsheet programs.
        safe = {k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v) for k, v in row.items()}
        writer.writerow(safe)
    return "\ufeff" + output.getvalue()


def connect_history(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=2)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1): raise ValueError("История создана более новой версией приложения.")
        db.execute("CREATE TABLE IF NOT EXISTS exports (hero TEXT, hand_id TEXT, source_sha TEXT, output_sha TEXT, archive TEXT, created TEXT, PRIMARY KEY(hero, hand_id))")
        db.execute("PRAGMA user_version=1")
        db.commit()
        return db
    except Exception:
        db.close()
        raise


def convert(paths: list[Path], output_dir: Path, options: Options, history_path: Path,
            progress: Callable = lambda text, current, total: None, cancel: Callable = lambda: False) -> dict:
    options.validate()
    if not __debug__: raise RuntimeError("Оптимизированный запуск Python (-O) не поддерживается.")
    if not paths: raise ValueError("Добавьте исходные файлы CoinPoker.")
    progress("Чтение исходных файлов…", 0, 0)
    records = {}
    variants = defaultdict(set)
    conflicts = set()
    excluded, reasons, inputs = [], [], []
    duplicates = input_count = 0
    def exclude(hid, raw, source, code, detail=""):
        excluded.append(raw)
        reasons.append(dict(hand_id=hid, source=source, code=code, reason=REASONS.get(code, code), detail=detail))

    for source, raw in text_sources([Path(p) for p in paths], cancel):
        try: text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as error: raise ValueError(f"Файл не в UTF-8: {source}") from error
        inputs.append(dict(name=source, bytes=len(raw), sha256=digest(raw)))
        blocks = re.split(r"(?=^CoinPoker Hand #)", text, flags=re.M)
        prefix = blocks.pop(0)
        if prefix.strip(): exclude("", prefix.strip(), source, "format", "Текст перед первой раздачей")
        for block in blocks:
            if cancel(): raise Cancelled()
            block = block.strip()
            if not block: continue
            input_count += 1
            if input_count > MAX_HANDS: raise ValueError(f"Не более {MAX_HANDS:,} раздач на один запуск.")
            match = re.match(r"CoinPoker Hand #(\d{1,30}):", block)
            if not match or len(block.encode("utf-8")) > MAX_HAND_BYTES:
                exclude("", block, source, "format", "ID или размер записи"); continue
            hid, sha = match[1], digest(block)
            if sha in variants[hid]:
                duplicates += 1
                continue
            variants[hid].add(sha)
            if hid in records:
                if hid not in conflicts:
                    exclude(hid, records[hid][0], records[hid][2], "duplicate_conflict")
                conflicts.add(hid)
                exclude(hid, block, source, "duplicate_conflict")
            else:
                records[hid] = (block, sha, source)
    if not records and not excluded: raise ValueError("В файлах нет раздач CoinPoker.")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    db = connect_history(Path(history_path))
    temp_path = None
    final_path = None
    try:
        db.execute("BEGIN IMMEDIATE")
        previous = {r[0]: (r[1], r[2]) for r in db.execute("SELECT hand_id,source_sha,output_sha FROM exports WHERE hero=?", (options.hero,))}
        regular, splash, ledger, shared, commits, skipped = [], [], [], [], [], 0
        totals = defaultdict(lambda: {"hands": 0, "net_cents": 0})
        for i, (hid, (raw, sha, source)) in enumerate(records.items(), 1):
            if cancel(): raise Cancelled()
            if i % 100 == 0 or i == 1 or i == len(records): progress("Конвертация и проверка…", i, len(records))
            if hid in conflicts: continue
            h = None
            status = "excluded"
            try:
                h = parse(raw, options.hero)
                neighbor = records.get(str(int(hid) + 1))
                next_text = neighbor[0] if neighbor and str(int(hid) + 1) not in conflicts else None
                rendered, q, additions = convert_one(h, next_text, options)
                outsha = digest(rendered)
                if options.mode == "new" and hid in previous:
                    if previous[hid] != (sha, outsha): raise Rejected("changed_export")
                    skipped += 1
                    status = "already_exported"
                    continue
                kind = "splash" if h["splash"] else "regular"
                (splash if h["splash"] else regular).append(rendered)
                status = "exported"
                totals[kind]["hands"] += 1
                totals[kind]["net_cents"] += q["hero_net"]
                totals[f"NL{h['bb']}"]["hands"] += 1
                totals[f"NL{h['bb']}"]["net_cents"] += q["hero_net"]
                ledger.append(dict(hand_id=hid, type=kind, date_msk=h["date"], bb_cents=h["bb"],
                                   hero_net_cents=q["hero_net"], source_sha256=sha, output_sha256=outsha,
                                   previous_export_changed=hid in previous and previous[hid] != (sha, outsha),
                                   cashout_confirmed=bool(q.get("cashout_hero_result_verified")),
                                   splash_added_cents=sum(a["added_cents"] for a in additions)))
                commits.append((hid, sha, outsha))
            except Rejected as error:
                exclude(hid, raw, source, error.code, error.detail)
            finally:
                if h and "splash" in h["features"]: shared.append(shared_row(h, options, status))
        if cancel(): raise Cancelled()
        stamp = datetime.now(timezone.utc)
        filename = f"CoinPoker_H2N3_{stamp.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.zip"
        final_path = output_dir / filename
        result = dict(version=__version__, hero=options.hero, mode=options.mode, created_utc=stamp.isoformat(),
                      input_records=input_count, unique_ids=len(records), exact_duplicates=duplicates,
                      conflicting_ids=len(conflicts), regular_hands=len(regular), splash_hands=len(splash),
                      exported_hands=len(commits), excluded_records=len(excluded), excluded_unique_ids=len({r['hand_id'] for r in reasons if r['hand_id']}),
                      previously_exported_skipped=skipped, totals=dict(totals),
                      reasons=dict(Counter(r["code"] for r in reasons)), rules=options.rules,
                      inputs=inputs, shared_in_H2N=False, splash_EV_verified=False, archive=str(final_path))
        progress("Создание ZIP-архива…", len(records), len(records))
        fd, temp = tempfile.mkstemp(prefix=".coinpoker-", suffix=".tmp", dir=output_dir)
        os.close(fd)
        temp_path = Path(temp)
        def hh(lines): return ("\n\n\n".join(lines) + ("\n" if lines else "")).replace("\n", "\r\n").encode("utf-8")
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("Import/Regular.txt", hh(regular))
            z.writestr("Import/Splash.txt", hh(splash))
            z.writestr("Excluded/Hands_original.txt", hh(excluded))
            z.writestr("Excluded/Reasons.csv", csv_text(reasons, ["hand_id", "source", "code", "reason", "detail"]))
            z.writestr("Shared_Splash.csv", csv_text(shared, ["hand_id", "date_msk", "bb_cents", "players", "contested_splash_cents", "shared_percent", "rule_since", "full_chest_cents_fraction", "shared_total_cents_fraction", "hero_shared_cents_fraction", "fractional_cent", "actual_wallet_cents", "status", "note"]))
            z.writestr("Conversion.csv", csv_text(ledger, ["hand_id", "type", "date_msk", "bb_cents", "hero_net_cents", "splash_added_cents", "cashout_confirmed", "previous_export_changed", "source_sha256", "output_sha256"]))
            z.writestr("Settings.json", json.dumps({"hero": options.hero, "rules": options.rules}, ensure_ascii=False, indent=2))
            z.writestr("Summary.json", json.dumps(result, ensure_ascii=False, indent=2))
            z.writestr("README.txt", make_readme(result).replace("\n", "\r\n").encode("utf-8-sig"))
        with zipfile.ZipFile(temp_path) as z:
            if z.testzip() is not None: raise OSError("Проверка архива не пройдена")
        if cancel(): raise Cancelled()
        # Unique generated destination; refuse any collision instead of overwriting.
        if final_path.exists(): raise FileExistsError(final_path)
        os.rename(temp_path, final_path)
        temp_path = None
        try:
            for hid, sha, outsha in commits:
                db.execute("INSERT INTO exports VALUES (?,?,?,?,?,?) ON CONFLICT(hero,hand_id) DO UPDATE SET source_sha=excluded.source_sha,output_sha=excluded.output_sha,archive=excluded.archive,created=excluded.created", (options.hero, hid, sha, outsha, str(final_path), stamp.isoformat()))
            db.commit()
        except sqlite3.Error as error:
            db.rollback()
            result["history_warning"] = f"Архив сохранён, но история выгрузок не обновлена: {error}"
        return result
    finally:
        if temp_path: temp_path.unlink(missing_ok=True)
        db.close()


def make_readme(r: dict) -> str:
    totals = r["totals"]
    net = sum(totals.get(k, {}).get("net_cents", 0) for k in ("regular", "splash"))
    lines = [f"CoinPoker → H2N3 | {__version__}", f"Hero: {r['hero']}", f"Дата UTC: {r['created_utc']}",
             f"Режим: {'вся загруженная история' if r['mode'] == 'all' else 'только ранее не выгруженные руки'}", "",
             f"Исходных записей: {r['input_records']}; уникальных ID: {r['unique_ids']}.",
             f"Обычные: {r['regular_hands']} рук; результат Hero {money(totals.get('regular', {}).get('net_cents', 0))}.",
             f"Splash: {r['splash_hands']} рук; результат Hero {money(totals.get('splash', {}).get('net_cents', 0))}.",
             f"ИТОГО: {r['exported_hands']} рук; результат за столом {money(net)}.",
             f"Исключённых записей: {r['excluded_records']}; точных дубликатов: {r['exact_duplicates']}; ранее выгруженных пропущено: {r['previously_exported_skipped']}.", "",
             "ИМПОРТ", "1. Распакуйте ZIP. В H2N3 импортируйте только Import/Regular.txt и Import/Splash.txt.",
             "2. Пустые TXT можно пропустить. Для проверки используйте отдельную базу H2N3.",
             f"3. Выберите игрока {r['hero']} в руме PS. Числа и лимиты выше — контрольные итоги этой выгрузки.",
             "4. Повторный импорт того же ID не заменяет старую раздачу. Изменённую конвертацию проверяйте в отдельной базе.", "",
             "ЧТО НАХОДИТСЯ В АРХИВЕ", "Import/Regular.txt — обычные раздачи. Import/Splash.txt — Splash с разыгрываемой частью награды.",
             "Excluded/Hands_original.txt — исходные записи, которые не удалось уверенно конвертировать.",
             "Excluded/Reasons.csv — ID и причины исключений; при конфликте ID сохранены все различные варианты.",
             "Shared_Splash.csv — расчёт общей доли на всех, включая победителя; суммы в центах и точных дробях.",
             "Conversion.csv — проверяемый список выгруженных рук, результат Hero и хэши.",
             "Summary.json / Settings.json — итоги и применённые настройки.", "",
             "SPLASH", "SPLASH dropped в исходнике уже является разыгрываемой частью; повторно умножать её на 40% нельзя.",
             "Общая доля делится на фактическое число участников раздачи, включая победителя. При 60% и шести игроках каждый получает расчётные 10% сундука.",
             "Неразыгрываемая общая доля НЕ добавлена в H2N3. Это расчёт по выбранным правилам, а не подтверждение зачисления на кошелёк.",
             "Фактическое округление дробных центов неизвестно. Поле actual_wallet_cents оставлено пустым.",
             "Дата правила — локальная дата MSK из истории. Настройки по умолчанию применены к прошлой истории по выбранному предположению.",
             "H2N3 принимал финансовые выплаты тестовых Splash, но в реплеере не учитывал начальный внешний банк.",
             "Полная корректность EV, pot odds и процентов банка для Splash НЕ подтверждена.", "",
             "ОГРАНИЧЕНИЯ", "Поддержан проверенный текстовый формат CoinPoker NLH. Другие игры и неизвестные строки исключаются.",
             "EV Cashout Hero и три борда исключаются. В обычной руке Cashout соперника допускается при согласованных исходных выплатах. Для восстановленной Splash-выплаты дополнительно требуется следующая раздача (ID+1, стол, место, лимит, время и стек).",
             "Автоматические пополнения/выводы могут не дать подтвердить стек — такая рука остаётся в исключениях.",
             "Сложное распределение Splash по нескольким банкам, неизвестные правила и недоказанные округления исключаются.",
             "USD — техническая метка для H2N3; курс валют не пересчитывается. Rake в H2N3 включает исходный Rake + Splash Fee.",
             "Анонимные имена соперников остаются как в источнике; приложение не может восстановить настоящие ники.",
             "История приложения означает «файл выгружен», а не «H2N3 импортировал». Она хранится на этом ПК отдельно для каждого ника.",
             "Для переноса истории используйте кнопки экспорта/импорта истории в приложении. Исключённые руки не помечаются выгруженными.",
             "Приложение работает локально, без учётной записи и отправки раздач в интернет.", "", "ПРИМЕНЁННЫЕ ПРАВИЛА"]
    for row in sorted(r["rules"], key=lambda x: (x["bb_cents"], x["since"])):
        lines.append(f"BB {money(row['bb_cents'])}: всем {row['shared_percent']}%, в банк {100-row['shared_percent']}%, с {row['since']}.")
    if r["reasons"]:
        lines += ["", "ПРИЧИНЫ ИСКЛЮЧЕНИЙ"]
        lines.extend(f"{REASONS.get(code, code)}: {n}" for code, n in r["reasons"].items())
    lines += ["", "ПО ЛИМИТАМ"]
    lines.extend(f"{key}: {value['hands']} рук, {money(value['net_cents'])}." for key, value in totals.items() if key.startswith("NL"))
    return "\n".join(lines) + "\n"
