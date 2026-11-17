#!/usr/bin/env python3
"""
mobile/coreboard_id_map.json: такая же карта, как coreboard_id_map.json у основной (ПК) доски, но для мобильной.
  {"<ID игрока>": {"nick": "ник на CoreBoard", "gdr_id": "ID аккаунта на WML"}}
compare.py читает её сам (mobile-update.py передаёт --id-map): по ней игрок CoreBoard сопоставляется с игроком WML по ID,
а не по нику.

Как строится (карта пересобирается целиком при каждом запуске):
  1. игроки берутся из cbd.txt (основной топ + Топ РК) в порядке рейтинга CoreBoard, WML-ID из ids_mobile.json;
  2. ID идут по порядку 1, 2, 3 ... БЕЗ пропусков и только для тех, кто есть в cbd: выпавшие из cbd в карте не остаются
     и номера не занимают;
  3. игрок из cbd без WML-ID в ids_mobile.json в карту не попадает и номер не занимает, он пишется в отчёт
     (допиши "ник": WML-ID в ids_mobile.json).
Номера привязаны к порядку в cbd: если кто-то вышел из cbd или вошёл выше, номера ниже него сдвинутся.

Запуск:
  python mobile/scripts/mobile-id-map.py --cbd mobile/scripts/cbd.txt
  python mobile/scripts/mobile-id-map.py --cbd cbd.txt --dry-run        ничего не записывать
  python mobile/scripts/mobile-id-map.py --from-ids-only                 cbd.txt нет: порядок берётся из ids_mobile.json
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent
FLAG_RE = re.compile(r"^(?:[\U0001F1E6-\U0001F1FF]{2}|[\U0001F3F3\U0001F3F4]\uFE0F?)\s*")
CBD_ROW = re.compile(r"^\s*\d+\)\s*(?:[\U0001F1E6-\U0001F1FF]{2}\s*)?(.*?)\s\(.*\)\s-\s(\d+(?:\.\d+)?)\s*$")


def norm(s):
    return re.sub(r"\s+", " ", FLAG_RE.sub("", str(s or "")).strip()).casefold()


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser(description="Карта ID игроков CoreBoard (мобильная): ID -> ник и WML-ID")
    ap.add_argument("--cbd", default=str(HERE / "cbd.txt"), help="cbd.txt: топ мобильной доски CoreBoard")
    ap.add_argument("--ids", default=str(DATA / "ids_mobile.json"))
    ap.add_argument("--map", default=str(DATA / "coreboard_id_map.json"))
    ap.add_argument("--from-ids-only", action="store_true", help="cbd.txt нет: порядок игроков берётся из ids_mobile.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ids = read_json(args.ids, {})
    if not isinstance(ids, dict) or not ids:
        sys.exit("ids_mobile.json пустой или не словарь. Останавливаюсь.")
    wml_by_nick = {}
    for nick, wid in ids.items():
        wml_by_nick.setdefault(norm(nick), (nick, str(wid).strip()))

    if args.from_ids_only:
        players = [(nick, str(wid).strip()) for nick, wid in ids.items()]
    else:
        p = Path(args.cbd)
        if not p.exists() or p.stat().st_size == 0:
            sys.exit(f"Нет {p} (или он пустой). Карту не трогаю.")
        players, seen = [], set()
        for line in p.read_text("utf-8").splitlines():
            m = CBD_ROW.match(line)
            if not m or norm(m.group(1)) in seen:
                continue
            seen.add(norm(m.group(1)))
            nick = FLAG_RE.sub("", m.group(1)).strip()
            hit = wml_by_nick.get(norm(nick))
            players.append((nick, hit[1] if hit else None))
        if not players:
            sys.exit("В cbd.txt не распознано ни одной строки игрока. Карту не трогаю.")

    cur = read_json(args.map, {})
    if not isinstance(cur, dict):
        sys.exit(f"{args.map}: ожидался словарь. Останавливаюсь, чтобы его не затереть.")
    no_wml = [nick for nick, wid in players if not wid]
    cmap = {}
    for nick, wid in players:
        if wid:
            cmap[str(len(cmap) + 1)] = {"nick": nick, "gdr_id": wid}

    old_id_by_wml = {str(v.get("gdr_id")): k for k, v in cur.items() if isinstance(v, dict)}
    moved = [f"{v['nick']}: {old_id_by_wml[v['gdr_id']]} -> {k}" for k, v in cmap.items()
             if v["gdr_id"] in old_id_by_wml and old_id_by_wml[v["gdr_id"]] != k]
    gone = [str(v.get("nick")) for v in cur.values() if isinstance(v, dict)
            and str(v.get("gdr_id")) not in {x["gdr_id"] for x in cmap.values()}]
    print(f"Игроков в cbd: {len(players)}, в карте {len(cmap)} (было {len(cur)}), ID сменился у {len(moved)}, убрано {len(gone)}")
    for line in moved[:15]:
        print("  ~ " + line)
    if gone:
        print(f"  убраны (нет в cbd): {', '.join(gone[:10])}" + (" ..." if len(gone) > 10 else ""))
    if no_wml:
        print(f"  В cbd есть, а WML-ID в ids_mobile.json нет, в карту не попали: {len(no_wml)}: " + ", ".join(no_wml[:10])
              + (" ..." if len(no_wml) > 10 else ""))
    if args.dry_run:
        print("--dry-run: файл не записан.")
        return
    if cmap == cur:
        return
    tmp = Path(args.map + ".tmp")
    tmp.write_text(json.dumps(cmap, ensure_ascii=False, indent=1) + "\n", "utf-8")
    os.replace(tmp, args.map)
    print(f"{Path(args.map).name} записан.")


main()
