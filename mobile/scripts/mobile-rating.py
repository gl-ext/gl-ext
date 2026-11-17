#!/usr/bin/env python3
"""
Рейтинг МОБИЛЬНОЙ доски по записям с WML (аналог gd-rating.py, но без глобала и Pointercrate).

Формула та же, что в gd-rating.py: по каждому уровню стоимость f, из лучших N записей берётся геометрическое среднее
(меньше = лучше). 100% даёт f = place уровня; иначе f = place * a * (T/c)^(ln(b/a)/ln(T/t)), a=2, b=4.
place, T и t берутся из mobile/levels_mobile.json (его делает mobile-levels.py).

СВОЯ СИСТЕМА ID:
  игрок  - ID его аккаунта на WML (одно число на человека, ник может меняться);
  уровень - число из levels_mobile.json (1, 2, 3...).
mobile/ids_mobile.json (ник -> ID игрока) этот скрипт ведёт сам, ТОЛЬКО для тех, кто есть в cbd.txt (--check-cbd): ник как на
CoreBoard, ID = ID аккаунта на WML. Пропавших из cbd он убирает. Ники, которые на WML написаны иначе, дописываются руками:
"ник на CoreBoard": WML-ID (пока игрок в cbd, строка остаётся). Без cbd.txt файл не меняется.
Названия уровней, которые на WML отличаются от CoreBoard, задаются в mobile/level_aliases.json:
  {"название на WML": "название на CoreBoard"}.

Результат (в папку scripts/):
  gdr.txt          итоговый рейтинг (WML + my_extras.json), строки как у gd-rating.py с ID в [скобках]:
                   "5) 🇵🇱 ник (Уровень, Уровень 88%) - 14.18 [12345]"
  gdr.json         то же полностью (записи с ID уровней)
  gdr_wml.txt      тот же рейтинг только по WML, без my_extras (по нему compare.py ищет, чего у нас не хватает)
  gdr_videos.json  {ID игрока: {ID уровня: [процент, ссылка]}}: видео для команд /fulladd
  mobile_levels_not_found.txt  названия уровней WML, которым не нашлось пары в levels_mobile.json

Запуск:
  python mobile/scripts/mobile-rating.py
  python mobile/scripts/mobile-rating.py --check-cbd cbd.txt   ещё сверить очки с CoreBoard (проверка формулы)
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import levels as levelsearch  # noqa: E402
import lists_core as L  # noqa: E402
import wml as site  # noqa: E402

A, B = 2, 4
FLAG_RE = re.compile(r"^(?:[\U0001F1E6-\U0001F1FF]{2}|[\U0001F3F3\U0001F3F4]\uFE0F?)\s*")

ap = argparse.ArgumentParser(description="Рейтинг мобильной доски по WML")
ap.add_argument("--n", type=int, default=6, help="сколько лучших записей берём")
ap.add_argument("--cap", type=float, default=300, help="макс. стоимость записи; недостающие добираются этим значением")
ap.add_argument("--show", type=int, default=30, help="сколько игроков печатать")
ap.add_argument("--ids", action="store_true", help="добавлять ID игрока в конец строки (нужно для compare.py)")
ap.add_argument("--list-conc", type=int, default=3, help="сколько страниц игроков WML качать одновременно")
ap.add_argument("--list-ttl", type=int, default=7 * 86400, help="секунд, пока страница игрока считается свежей")
ap.add_argument("--extras", default=str(DATA / "my_extras.json"), help="my_extras.json: записи CoreBoard, которых нет на WML; идут в gdr_full.*")
ap.add_argument("--check-cbd", default="", help="файл cbd.txt: сверить наши очки с очками CoreBoard")
ap.add_argument("--out-dir", default=str(HERE), help="куда писать gdr.txt и остальное (по умолчанию scripts/)")
args = ap.parse_args()
N = args.n
OUT = Path(args.out_dir)
OUT.mkdir(parents=True, exist_ok=True)


# ---------- формула ----------
def score(h, c, T, t):
    if c >= 100:
        return h
    if T is None or t is None or not (t < T) or c < t or c <= 0:
        return None
    return h * A * (T / c) ** (math.log(B / A) / math.log(T / t))


def geo_mean(values):
    return math.exp(sum(math.log(x) for x in values) / len(values))


def flag(code):
    code = str(code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return default


# ---------- уровни ----------
levels_data = read_json(DATA / "levels_mobile.json", {}).get("levels")
if not levels_data:
    sys.exit("Нет mobile/levels_mobile.json (или он пустой). Сначала запусти mobile-levels.py.")

meta = {}
for k, e in levels_data.items():
    try:
        meta[str(k)] = {"name": e["name"], "place": float(e["place"]),
                        "T": float(e["T"]) if e.get("T") is not None else None,
                        "t": float(e["t"]) if e.get("t") is not None else None}
    except (KeyError, TypeError, ValueError):
        print(f"  уровень {k}: нет place/name, пропускаю")
print(f"Уровней в levels_mobile.json: {len(meta)}")

index = levelsearch.LevelIndex()
index.add_tp(levels_data)
for wml_name, cb_name in (read_json(DATA / "level_aliases.json", {}) or {}).items():
    key = index.find(cb_name)
    if key is None:
        print(f"  level_aliases.json: уровня {cb_name!r} нет в levels_mobile.json, алиас {wml_name!r} пропускаю")
    else:
        index.add_alias(wml_name, key)
index.stats.clear()

# ---------- WML ----------
src = L.get("wml")
if src is None or not L.is_enabled(src):
    sys.exit("Лист wml не подключён или выключен (GL_LISTS_OFF).")
src.ttl = args.list_ttl
store = L.JsonStore(str(HERE / "wmlcache.json"))
print("\nWML:")
data = src.fetch(store, workers=args.list_conc)
if not data:
    sys.exit("WML недоступен или скачался не весь: рейтинг не строю, чтобы не опубликовать неполные данные.")

# ---------- записи игроков ----------
best_by, videos, unresolved = {}, {}, {}
found = {}
for r in data["records"]:
    name = r["l"]
    fk = (name, r.get("pl"))
    if fk not in found:
        found[fk] = index.find(name, r.get("pl"))
    k = found[fk]
    if k is None:
        label = f"{name} (#{r['pl']})" if r.get("pl") else name
        unresolved[label] = unresolved.get(label, 0) + 1
        continue
    pct = float(r["p"])
    if pct <= 0:
        continue
    uid = str(r["a"])
    cur = best_by.setdefault(uid, {})
    if pct > cur.get(k, (-1, None))[0]:
        cur[k] = (pct, site.normalize_link(r["v"]) if r.get("v") else "")
print(f"Игроков WML: {len(data['players'])}, записей: {len(data['records'])}")
with_place = sum(1 for r in data["records"] if r.get("pl"))
print(f"  записей с местом уровня на WML: {with_place} из {len(data['records'])}")
if with_place < len(data["records"]) * 0.9:
    print("  ВНИМАНИЕ: у большинства записей нет места уровня. Одинаковые названия (два Pandemonium) не различить и они выпадут. "
          "Проверь, что обновлены wml.py, list_wml.py, lists_core.py (rev = 3 в list_wml.py) и что кеш wmlcache.json перекачался.")
if index.stats:
    print("  названия найдены: " + ", ".join(f"{k} {v}" for k, v in index.stats.items()))
if unresolved:
    top = sorted(unresolved.items(), key=lambda kv: -kv[1])
    print(f"  уровней WML без пары в levels_mobile.json: {len(top)}, чаще всего: "
          + ", ".join(f"{n} ({c})" for n, c in top[:10]))
    print("  (если это тот же уровень под другим названием, допиши его в mobile/level_aliases.json)")
(OUT / "mobile_levels_not_found.txt").write_text(
    "".join(f"{n} | записей: {c}\n" for n, c in sorted(unresolved.items(), key=lambda kv: -kv[1])), "utf-8")

# ---------- ids_mobile.json: ник -> ID игрока, ТОЛЬКО те, кто есть в cbd.txt ----------
# Ник берётся как на CoreBoard (из cbd.txt), ID = ID аккаунта на WML. Игроки, которых нет в cbd, в файл не попадают, а
# уже лежащие в нём и пропавшие из cbd убираются. Нет cbd.txt (--check-cbd не передан или файл пустой): файл не трогаем.
ids_path = DATA / "ids_mobile.json"
ids = read_json(ids_path, {})
if not isinstance(ids, dict):
    sys.exit("ids_mobile.json: ожидался словарь ник -> ID. Останавливаюсь, чтобы его не затереть.")
CBD_ROW = re.compile(r"^\s*\d+\)\s*(?:[\U0001F1E6-\U0001F1FF]{2}\s*)?(.*?)\s\(.*\)\s-\s(\d+(?:\.\d+)?)\s*$")
cbd_nicks = {}
if args.check_cbd and Path(args.check_cbd).exists():
    for line in Path(args.check_cbd).read_text("utf-8").splitlines():
        m = CBD_ROW.match(line)
        if m:
            cbd_nicks.setdefault(levelsearch.normalize(m.group(1)), m.group(1).strip())
if not cbd_nicks:
    print("ids_mobile.json: cbd.txt не передан или пустой (--check-cbd), файл не изменён")
else:
    kept = {n: v for n, v in ids.items() if levelsearch.normalize(FLAG_RE.sub("", n)) in cbd_nicks}
    removed = len(ids) - len(kept)
    taken = {levelsearch.normalize(FLAG_RE.sub("", n)): str(v).strip() for n, v in kept.items()}
    added, clashes = 0, []
    for aid, pl in sorted(data["players"].items(), key=lambda kv: (kv[1].get("r") is None, kv[1].get("r") or 0)):
        nick = src.display_nick(pl["n"]) or pl["n"]
        nk = levelsearch.normalize(nick)
        if nk not in cbd_nicks:
            continue
        if nk not in taken:
            kept[cbd_nicks[nk]] = str(aid)
            taken[nk] = str(aid)
            added += 1
        elif taken[nk] != str(aid):
            clashes.append(f"{nick}: в файле ID {taken[nk]}, на WML ID {aid}")
    if added or removed:
        tmp = ids_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(kept, ensure_ascii=False, indent=1) + "\n", "utf-8")
        tmp.replace(ids_path)
    ids = kept
    print(f"ids_mobile.json: ников {len(ids)} (в cbd {len(cbd_nicks)}), дописано {added}, убрано {removed}")
    missing = [n for k, n in cbd_nicks.items() if k not in taken]
    if missing:
        print(f"  в cbd есть, а ID нет (на WML такого ника нет, допиши руками \"ник\": WML-ID): {len(missing)}, первые 5: "
              + ", ".join(missing[:5]))
    if clashes:
        print(f"  ВНИМАНИЕ: ник уже занят другим ID у {len(clashes)} игроков (в файле оставлен прежний), первые 5:")
        for c in clashes[:5]:
            print("    " + c)

# ---------- очки ----------
def rate(bests):
    """bests: {WML-ID игрока: {ключ уровня: (процент, видео)}} -> список игроков с очками и местами."""
    out = []
    for uid, best in bests.items():
        pl = data["players"].get(uid)
        if not pl:
            continue
        recs = []
        for k, (pct, video) in best.items():
            lv = meta.get(k)
            if not lv:
                continue
            f = score(lv["place"], pct, lv["T"], lv["t"])
            if f is not None and f <= args.cap:
                recs.append({"level": lv["name"], "key": k, "place": lv["place"], "percent": pct, "f": f, "video": video})
        if not recs:
            continue
        recs.sort(key=lambda r: r["f"])
        top = recs[:N]
        while len(top) < N:
            top.append({"level": "-", "key": None, "place": None, "percent": None, "f": args.cap, "video": ""})
        out.append({"id": uid, "name": src.display_nick(pl["n"]) or pl["n"], "country": pl.get("c"),
                    "score": geo_mean([r["f"] for r in top]), "records": top})
    out.sort(key=lambda r: (r["score"], int(r["id"]) if r["id"].isdigit() else 0))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


result = rate(best_by)          # только WML: по нему compare.py пересобирает my_extras.json (иначе получится замкнутый круг)

# ---------- my_extras.json: записи, которые есть на CoreBoard, а на WML нет ----------
# ID игрока в my_extras.json = его WML-ID. Запись добавляется (или заменяет, если процент выше) к записям WML, и тогда очки игрока
# совпадают с CoreBoard. Без экстрас такой игрок остаётся с недобранными записями (очки выше, в топ не попадает).
extras_users = (read_json(args.extras, {}) or {}).get("users", {}) if args.extras else {}
best_full = {u: dict(b) for u, b in best_by.items()}
ex_applied, ex_skipped, ex_players = 0, [], set()
for uid, rows in extras_users.items():
    uid = str(uid)
    if uid not in data["players"]:
        ex_skipped.append(f"{uid}: игрока нет на WML")
        continue
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        k, pct = str(row[0]), float(row[1])
        video = site.normalize_link(row[2]) if len(row) > 2 and row[2] else ""
        if k not in meta:
            ex_skipped.append(f"{uid}: уровня {k} нет в levels_mobile.json")
            continue
        cur = best_full.setdefault(uid, {})
        if pct > cur.get(k, (-1, None))[0]:
            cur[k] = (pct, video)
            ex_applied += 1
            ex_players.add(uid)
full = rate(best_full)          # WML + экстрас: итоговый рейтинг
print(f"my_extras.json: записей добавлено {ex_applied} у {len(ex_players)} игроков, пропущено {len(ex_skipped)}")
for line in ex_skipped[:5]:
    print("  " + line)


def line_short(r):
    parts = []
    for rec in r["records"]:
        if rec["place"] is None:
            continue
        pct = "" if rec["percent"] >= 100 else f" {rec['percent']:g}%"
        parts.append(rec["level"] + pct)
    tail = f" [{r['id']}]" if args.ids else ""
    return f"{r['rank']}) {flag(r['country'])} {r['name']} ({', '.join(parts)}) - {r['score']:.2f}{tail}"


print(f"\nИгроков в рейтинге: {len(full)} (только по WML: {len(result)})\n")
for r in full[: args.show]:
    print(line_short(r))

# gdr.txt = итоговый рейтинг (WML + my_extras). gdr_wml.txt = только WML: по нему compare.py пересобирает my_extras.json
(OUT / "gdr.txt").write_text("\n".join(line_short(r) for r in full), "utf-8")
(OUT / "gdr.json").write_text(json.dumps([{**r, "score": round(r["score"], 2)} for r in full],
                                         ensure_ascii=False, indent=1), "utf-8")
(OUT / "gdr_wml.txt").write_text("\n".join(line_short(r) for r in result), "utf-8")
vids = {r["id"]: {rec["key"]: [rec["percent"], rec["video"]] for rec in r["records"] if rec["key"] and rec["video"]}
        for r in full}
(OUT / "gdr_videos.json").write_text(json.dumps({k: v for k, v in vids.items() if v}, ensure_ascii=False), "utf-8")
print(f"\nЗаписано: {OUT / 'gdr.txt'} (WML + my_extras), gdr_wml.txt (только WML), gdr.json, gdr_videos.json")

# ---------- проверка формулы по очкам CoreBoard ----------
if args.check_cbd:
    row = re.compile(r"^\s*\d+\)\s*(?:[\U0001F1E6-\U0001F1FF]{2}\s*)?(.*?)\s\(.*\)\s-\s(\d+(?:\.\d+)?)\s*$")
    cb = {}
    for line in Path(args.check_cbd).read_text("utf-8").splitlines():
        m = row.match(line)
        if m:
            cb.setdefault(levelsearch.normalize(m.group(1)), float(m.group(2)))
    ours = {levelsearch.normalize(r["name"]): r for r in full}   # сверяем итоговый рейтинг (WML + my_extras)
    pairs = [(ours[k], cb[k]) for k in cb if k in ours]
    ok = sum(1 for r, c in pairs if abs(r["score"] - c) <= 0.01)
    print(f"\nСверка с CoreBoard: ников совпало {len(pairs)} из {len(cb)}, очки сходятся (±0.01) у {ok}")
    bad = sorted((p for p in pairs if abs(p[0]["score"] - p[1]) > 0.01), key=lambda p: -abs(p[0]["score"] - p[1]))
    for r, c in bad[:10]:
        print(f"  {r['name']}: у нас {r['score']:.2f}, на CoreBoard {c:.2f}")
    if pairs and ok < len(pairs) * 0.5:
        print("  ВНИМАНИЕ: очки сходятся меньше чем у половины игроков: проверь формулу и T/t в levels_mobile.json")
