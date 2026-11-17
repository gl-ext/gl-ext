#!/usr/bin/env python3
"""
Рейтинг МОБИЛЬНОЙ доски по записям с WML (аналог gd-rating.py, но без глобала и Pointercrate).

Формула та же, что в gd-rating.py: по каждому уровню стоимость f, из лучших N записей берётся геометрическое среднее
(меньше = лучше). 100% даёт f = place уровня; иначе f = place * a * (T/c)^(ln(b/a)/ln(T/t)), a=2, b=4.
place, T и t берутся из mobile/levels_mobile.json (его делает mobile-levels.py).

СВОЯ СИСТЕМА ID:
  игрок  - ID его аккаунта на WML (одно число на человека, ник может меняться);
  уровень - число из levels_mobile.json (1, 2, 3...).
mobile/ids_mobile.json (ник -> ID игрока) этот скрипт ДОПОЛНЯЕТ сам: каждый ник игрока WML (без клана) записывается
со своим WML-ID. Уже существующие строки не меняются, поэтому ники, которых на WML нет или которые на CoreBoard
написаны иначе, дописываются в файл руками: "ник на CoreBoard": WML-ID.
Названия уровней, которые на WML отличаются от CoreBoard, задаются в mobile/level_aliases.json:
  {"название на WML": "название на CoreBoard"}.

Результат (в папку scripts/):
  gdr.txt          строки как у gd-rating.py с ID в [скобках]: "5) 🇵🇱 ник (Уровень, Уровень 88%) - 14.18 [12345]"
  gdr.json         то же полностью (записи с ID уровней)
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

# ---------- ids_mobile.json: ник -> ID игрока (дополняем, чужие строки не трогаем) ----------
ids_path = DATA / "ids_mobile.json"
ids = read_json(ids_path, {})
if not isinstance(ids, dict):
    sys.exit("ids_mobile.json: ожидался словарь ник -> ID. Останавливаюсь, чтобы его не затереть.")
taken = {levelsearch.normalize(FLAG_RE.sub("", n)): str(v).strip() for n, v in ids.items()}
added, clashes = 0, []
for aid, pl in sorted(data["players"].items(), key=lambda kv: (kv[1].get("r") is None, kv[1].get("r") or 0)):
    nick = src.display_nick(pl["n"]) or pl["n"]
    nk = levelsearch.normalize(nick)
    if not nk:
        continue
    if nk not in taken:
        ids[nick] = str(aid)
        taken[nk] = str(aid)
        added += 1
    elif taken[nk] != str(aid):
        clashes.append(f"{nick}: в файле ID {taken[nk]}, на WML ID {aid}")
if added:
    tmp = ids_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ids, ensure_ascii=False, indent=1) + "\n", "utf-8")
    tmp.replace(ids_path)
print(f"ids_mobile.json: ников {len(ids)}, дописано {added}")
if clashes:
    print(f"  ВНИМАНИЕ: ник уже занят другим ID у {len(clashes)} игроков (в файле оставлен прежний), первые 5:")
    for c in clashes[:5]:
        print("    " + c)

# ---------- очки ----------
result = []
for uid, best in best_by.items():
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
    result.append({"id": uid, "name": src.display_nick(pl["n"]) or pl["n"], "country": pl.get("c"),
                   "score": geo_mean([r["f"] for r in top]), "records": top})
result.sort(key=lambda r: (r["score"], int(r["id"]) if r["id"].isdigit() else 0))
for i, r in enumerate(result, 1):
    r["rank"] = i


def line_short(r):
    parts = []
    for rec in r["records"]:
        if rec["place"] is None:
            continue
        pct = "" if rec["percent"] >= 100 else f" {rec['percent']:g}%"
        parts.append(rec["level"] + pct)
    tail = f" [{r['id']}]" if args.ids else ""
    return f"{r['rank']}) {flag(r['country'])} {r['name']} ({', '.join(parts)}) - {r['score']:.2f}{tail}"


print(f"\nИгроков в рейтинге: {len(result)}\n")
for r in result[: args.show]:
    print(line_short(r))

(OUT / "gdr.txt").write_text("\n".join(line_short(r) for r in result), "utf-8")
(OUT / "gdr.json").write_text(json.dumps([{**r, "score": round(r["score"], 2)} for r in result],
                                         ensure_ascii=False, indent=1), "utf-8")
vids = {r["id"]: {rec["key"]: [rec["percent"], rec["video"]] for rec in r["records"] if rec["key"] and rec["video"]}
        for r in result}
(OUT / "gdr_videos.json").write_text(json.dumps({k: v for k, v in vids.items() if v}, ensure_ascii=False), "utf-8")
print(f"\nЗаписано: {OUT / 'gdr.txt'}, gdr.json, gdr_videos.json")

# ---------- проверка формулы по очкам CoreBoard ----------
if args.check_cbd:
    row = re.compile(r"^\s*\d+\)\s*(?:[\U0001F1E6-\U0001F1FF]{2}\s*)?(.*?)\s\(.*\)\s-\s(\d+(?:\.\d+)?)\s*$")
    cb = {}
    for line in Path(args.check_cbd).read_text("utf-8").splitlines():
        m = row.match(line)
        if m:
            cb.setdefault(levelsearch.normalize(m.group(1)), float(m.group(2)))
    ours = {levelsearch.normalize(r["name"]): r for r in result}
    pairs = [(ours[k], cb[k]) for k in cb if k in ours]
    ok = sum(1 for r, c in pairs if abs(r["score"] - c) <= 0.01)
    print(f"\nСверка с CoreBoard: ников совпало {len(pairs)} из {len(cb)}, очки сходятся (±0.01) у {ok}")
    bad = sorted((p for p in pairs if abs(p[0]["score"] - p[1]) > 0.01), key=lambda p: -abs(p[0]["score"] - p[1]))
    for r, c in bad[:10]:
        print(f"  {r['name']}: у нас {r['score']:.2f}, на CoreBoard {c:.2f}")
    if pairs and ok < len(pairs) * 0.5:
        print("  ВНИМАНИЕ: очки сходятся меньше чем у половины игроков: проверь формулу и T/t в levels_mobile.json")
