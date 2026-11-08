"""Versioned settings and portable export history. No network I/O."""
import json
import os
import re
import tempfile
from pathlib import Path
from .engine import Options, connect_history


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def load_settings(path: Path):
    if not path.exists(): return {"schema": 1, "last_hero": "", "profiles": {}}
    if path.stat().st_size > 1_000_000: raise ValueError("Файл настроек слишком большой")
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("profiles"), dict):
        raise ValueError("Неизвестная версия файла настроек")
    if not isinstance(data.get("last_hero", ""), str): raise ValueError("Некорректный последний профиль")
    for hero, profile in data["profiles"].items():
        if not isinstance(profile, dict) or "rules" not in profile: raise ValueError("Некорректный профиль")
        Options(hero, profile.get("mode", "all"), profile["rules"]).validate()
        if not isinstance(profile.get("output_dir", ""), str): raise ValueError("Некорректная папка результата")
    return data


def export_history(db_path: Path, destination: Path):
    db = connect_history(db_path)
    try:
        rows = list(db.execute("SELECT hero,hand_id,source_sha,output_sha,archive,created FROM exports ORDER BY hero,hand_id"))
        atomic_json(destination, {"application": "CoinPokerConverter", "schema": 1, "exports": rows})
        return len(rows)
    finally: db.close()


def import_history(db_path: Path, source: Path):
    if source.stat().st_size > 128 * 1024 * 1024: raise ValueError("Файл истории слишком большой")
    data = json.loads(source.read_text("utf-8-sig"))
    if not isinstance(data, dict) or data.get("application") != "CoinPokerConverter" or data.get("schema") != 1:
        raise ValueError("Это не история выгрузок CoinPoker Converter")
    rows = data.get("exports")
    if not isinstance(rows, list): raise ValueError("Нет списка выгрузок")
    seen = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 6 or any(not isinstance(v, str) for v in row):
            raise ValueError("Повреждённая запись истории")
        hero, hid, sha, outsha, archive, created = row
        Options(hero).validate()
        if not re.fullmatch(r"\d{1,30}", hid) or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in [sha, outsha]):
            raise ValueError("Некорректный ID или хэш")
        key = (hero, hid)
        if key in seen and seen[key][2:4] != row[2:4]: raise ValueError("Конфликт внутри переносимой истории")
        seen[key] = row
    db = connect_history(db_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        added = 0
        for row in seen.values():
            previous = db.execute("SELECT source_sha,output_sha FROM exports WHERE hero=? AND hand_id=?", row[:2]).fetchone()
            if previous:
                if tuple(row[2:4]) != previous: raise ValueError(f"Конфликт истории: {row[0]}, ID {row[1]}. Локальная история сохранена.")
            else:
                db.execute("INSERT INTO exports VALUES (?,?,?,?,?,?)", row)
                added += 1
        db.commit()
        return added
    finally: db.close()
