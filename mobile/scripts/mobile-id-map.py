#!/usr/bin/env python3
"""
mobile/coreboard_id_map.json: такая же карта, как coreboard_id_map.json у основной (ПК) доски, но для мобильной.
  {"<ID игрока на CoreBoard>": {"nick": "ник на CoreBoard", "gdr_id": "ID аккаунта на WML"}}
compare.py читает её сам (mobile-update.py передаёт --id-map): по ней игрок CoreBoard сопоставляется с игроком WML по ID,
а не по нику, и новым игрокам ID выдаются после занятых.

Как ведётся (карта только дополняется, выданные ID никогда не меняются и не переиспользуются):
  1. игроки берутся из cbd.txt в порядке рейтинга CoreBoard, их WML-ID из ids_mobile.json (ник -> WML-ID);
  2. игрок уже есть в карте (тот же WML-ID): его ID остаётся, ник обновляется, если сменился;
  3. нового игрока ID берётся так: числовой ID с сайта из cbd_ids.json (если ссылки на игроков на CoreBoard числовые);
     иначе ключ, который мы сами выдали ему в cbd/missing_players.json (чтобы после добавления на CoreBoard ID не поменялся);
     иначе следующий свободный номер после самого большого в карте (для пустой карты: 1, 2, 3 ... по порядку cbd);
  4. игроков, которые выпали из cbd, из карты не убираем;
  5. игрок из cbd без WML-ID в ids_mobile.json в карту не попадает, он пишется в отчёт (допиши "ник": WML-ID в ids_mobile.json).

Запуск:
  python mobile/scripts/mobile-id-map.py --cbd mobile/scripts/cbd.txt
  python mobile/scripts/mobile-id-map.py --cbd cbd.txt --dry-run        ничего не записывать
  python mobile/scripts/mobile-id-map.py --from-ids-only                 пустая карта из ids_mobile.json: ID по порядку строк в файле
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
    ap.add_argument("--cbd-ids", default=str(HERE / "cbd_ids.json"), help="ник -> ID с сайта (если ссылки на игроков числовые)")
    ap.add_argument("--ids", default=str(DATA / "ids_mobile.json"))
    ap.add_argument("--map", default=str(DATA / "coreboard_id_map.json"))
    ap.add_argument("--players", default=str(DATA / "cbd" / "missing_players.json"), help="выданные нами ID новых игроков")
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
    cmap = {str(k): dict(v) for k, v in cur.items() if isinstance(v, dict) and v.get("gdr_id")}
    by_wml = {str(v["gdr_id"]): k for k, v in cmap.items()}

    site_ids = {}
    for nick, cid in (read_json(args.cbd_ids, {}) or {}).items():
        if str(cid).strip().isdigit():
            site_ids[norm(nick)] = str(cid).strip()
    issued = {}
    for key, info in ((read_json(args.players, {}) or {}).get("users", {}) or {}).items():
        if str(key).isdigit() and isinstance(info, dict) and info.get("gdr_id"):
            issued[str(info["gdr_id"])] = str(key)

    def next_free():
        return str(max([int(k) for k in cmap if k.isdigit()] or [0]) + 1)

    added, renamed, no_wml = [], [], []
    for nick, wid in players:
        if not wid:
            no_wml.append(nick)
            continue
        if wid in by_wml:
            k = by_wml[wid]
            if cmap[k].get("nick") != nick:
                renamed.append(f"{k}: {cmap[k].get('nick')} -> {nick}")
                cmap[k]["nick"] = nick
            continue
        k = next((c for c in (site_ids.get(norm(nick)), issued.get(wid)) if c and c not in cmap), None) or next_free()
        cmap[k] = {"nick": nick, "gdr_id": wid}
        by_wml[wid] = k
        added.append(f"{k}: {nick} (WML {wid})")

    print(f"Игроков в cbd: {len(players)}, в карте было {len(cur)}, стало {len(cmap)}: добавлено {len(added)}, ник сменился у {len(renamed)}")
    for line in added[:20]:
        print("  + " + line)
    for line in renamed[:20]:
        print("  ~ " + line)
    if no_wml:
        print(f"  В cbd есть, а WML-ID в ids_mobile.json нет, в карту не попали: {len(no_wml)}: " + ", ".join(no_wml[:10])
              + (" ..." if len(no_wml) > 10 else ""))
    if args.dry_run:
        print("--dry-run: файл не записан.")
        return
    if not added and not renamed and Path(args.map).exists():
        return
    out = {k: cmap[k] for k in sorted(cmap, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x))}
    tmp = Path(args.map + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", "utf-8")
    os.replace(tmp, args.map)
    print(f"{Path(args.map).name} записан.")


main()
