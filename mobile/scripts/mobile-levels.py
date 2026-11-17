#!/usr/bin/env python3
"""
Уровни МОБИЛЬНОЙ доски -> mobile/levels_mobile.json.

ID уровня = название уровня на CoreBoard (без "x"). Алиасы с CoreBoard ("Имя = Уровень (T=.., t=..)") получают ключ "xИмя".

Порядок работы:
  1. берём топ WML (по умолчанию 300 уровней, --limit);
  2. берём список уровней CoreBoard (/mobile/levels);
  3. сопоставляем построчно. Скобки в названиях не читаются (на CoreBoard название может быть "Имя (что-то)"):
       - на позиции i уровень WML и уровень CoreBoard называются одинаково -> пара;
       - иначе ищем этот уровень WML в списке CoreBoard сверху вниз (среди ещё не занятых) -> пара;
       - не нашли -> уровень остаётся без ID, в пары не попадает, попадает в отчёт;
  4. идём дальше к следующему уровню WML.
  Название, которое в топе встречается больше одного раза, сравнивается с другим таким же: первый сверху на WML = первый сверху на
  CoreBoard, второй = второй (при разном числе повторов берётся ближайший по месту, не выше предыдущего). Это идёт в отчёт.
  Если нужно вручную связать названия, которые пишутся по-разному: mobile/level_aliases.json {"название на WML": "название на CoreBoard"}.

Файлы:
  levels_mobile.json          {"levels": {"<название CoreBoard>": {"place": место на CoreBoard, "name", "T", "t", "wml": место на WML или null,
                                                                   "wml_name": название на WML}, "xАлиас": {...}}}
  mobile_levels_report.txt    отчёт: уровни WML без ID, уровни CoreBoard без пары на WML, места, где позиции не совпали

Запуск:
  python mobile/scripts/mobile-levels.py
  python mobile/scripts/mobile-levels.py --dry-run                 ничего не записывать, только показать отчёт
  python mobile/scripts/mobile-levels.py --wml-json wml.json       взять топ WML из файла (список [{"position":1,"name":".."}, ...] или просто [".."])
  python mobile/scripts/mobile-levels.py --cb-html page.html       взять страницу CoreBoard из файла
  python mobile/scripts/mobile-levels.py --force                   записать, даже если уровней стало подозрительно мало
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent                      # mobile/
LEVELS_FILE = DATA / "levels_mobile.json"
ALIASES_FILE = DATA / "level_aliases.json"
REPORT_FILE = DATA / "mobile_levels_report.txt"
CB_URL = "https://coreboard.pythonanywhere.com/mobile/levels"
WML_SITE = "https://worldmobilelist.com"
# ВНИМАНИЕ: адрес списка уровней WML не проверен (в остальных скриптах используется только рейтинг игроков).
# Если оба адреса не отвечают, дай файл через --wml-json или поправь здесь.
WML_LEVEL_URLS = [WML_SITE + "/api/v2/demons/listed/?limit=100&after={after}",
                  WML_SITE + "/api/v1/levels/ranking/?after={after}"]
UA = "gl-ext-top6"

NUM = r"\d+(?:\.\d+)?"
LEVEL_RE = re.compile(r"^(.*?) \(T=(" + NUM + r"), t=(" + NUM + r")\)$")
PAREN_RE = re.compile(r"\([^()]*\)")


def norm(s):
    """Название для сравнения: без круглых скобок и их содержимого, без лишних пробелов, без учёта регистра."""
    s = str(s or "")
    while True:
        t = PAREN_RE.sub(" ", s)
        if t == s:
            break
        s = t
    return re.sub(r"\s+", " ", s).strip().casefold()


def _strip_parens(s):
    while True:
        t = PAREN_RE.sub(" ", s)
        if t == s:
            break
        s = t
    return re.sub(r"\s+", " ", s).strip()


def _num(s):
    f = float(s)
    return int(f) if f == int(f) else f


def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def http_get(url):
    last = None
    for _ in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise RuntimeError(f"{url}: {last}")


# ---------- CoreBoard ----------
def parse_cb(page):
    """-> (main, aliases). main: [(place, name, T, t)] по месту, aliases: [(alias, like, T, t)]."""
    main = []
    for m in re.finditer(r'<li id="level-(\d+)">(.*?)</li>', page, re.S):
        mm = LEVEL_RE.match(_clean(m.group(2)))
        if not mm:
            print(f"  CoreBoard: пропускаю нераспознанную строку level-{m.group(1)}: {_clean(m.group(2))!r}")
            continue
        main.append((int(m.group(1)), mm.group(1), _num(mm.group(2)), _num(mm.group(3))))
    main.sort()
    names = {name for _, name, _, _ in main}
    aliases = []
    for m in re.finditer(r"<li>(.*?)</li>", page, re.S):
        mm = LEVEL_RE.match(_clean(m.group(1)))
        if not mm or " = " not in mm.group(1):
            continue
        body, T, t = mm.group(1), _num(mm.group(2)), _num(mm.group(3))
        for i in [x.start() for x in re.finditer(" = ", body)]:
            alias, like = body[:i], body[i + 3:]
            if like in names:
                aliases.append((alias, like, T, t))
                break
    return main, aliases


# ---------- WML ----------
def _wml_items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "levels", "demons", "results", "items", "ranking"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _wml_rows(items):
    rows = []
    for i, it in enumerate(items, 1):
        if isinstance(it, str):
            rows.append((i, it))
        elif isinstance(it, dict):
            name = next((it[k] for k in ("name", "level", "title") if it.get(k)), None)
            pos = next((it[k] for k in ("position", "place", "rank") if isinstance(it.get(k), int)), None)
            if name:
                rows.append((pos if pos is not None else i, str(name)))
    return rows


def fetch_wml(limit):
    """Топ уровней WML: [(место, название)] по возрастанию места, не больше limit."""
    errors = []
    for tpl in WML_LEVEL_URLS:
        rows, after = [], 0
        try:
            for _ in range(50):
                got = _wml_rows(_wml_items(json.loads(http_get(tpl.format(after=after)))))
                if not got:
                    break
                rows += got
                if len(rows) >= limit:
                    break
                after += len(got)
                time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{tpl}: {e}")
            continue
        if rows:
            rows.sort()
            return rows[:limit]
    sys.exit("Не удалось получить список уровней WML:\n  " + "\n  ".join(errors or ["пустой ответ"])
             + "\nCoreBoard не трогаю, levels_mobile.json не изменён. Дай топ файлом (--wml-json) или поправь WML_LEVEL_URLS.")


# ---------- сопоставление ----------
def align(wml, cb, aliases_map):
    """wml: [(место, название)], cb: [(место, название, T, t)] (оба по порядку).
    Возвращает (pairs, wml_no_id, cb_no_pair, shifts, dups): pairs {индекс cb: индекс wml}.

    Название, которое встречается в топе больше одного раза (на WML или на CoreBoard, скобки не читаются), не ищется «первое
    попавшееся»: уровни с таким названием сравниваются между собой по порядку. Первый сверху на WML = первый сверху на CoreBoard,
    второй = второй. Если число повторов разное, берётся свободный уровень CoreBoard с ближайшим местом, не выше пары предыдущего
    повтора. Остальные названия (одиночные): то же название на той же позиции, иначе поиск сверху вниз."""
    cb_norm = [norm(r[1]) for r in cb]
    target = [norm(aliases_map.get(norm(n), n)) for _, n in wml]
    wml_occ, cb_occ = {}, {}
    for i, t in enumerate(target):
        wml_occ.setdefault(t, []).append(i)
    for j, n in enumerate(cb_norm):
        cb_occ.setdefault(n, []).append(j)

    used = set()
    pairs, wml_no_id, shifts, dups = {}, [], [], []
    for i, (wplace, wname) in enumerate(wml):
        t = target[i]
        free = [k for k in cb_occ.get(t, []) if k not in used]
        j = None
        if len(wml_occ[t]) > 1 or len(cb_occ.get(t, [])) > 1:
            rank = wml_occ[t].index(i)
            same_count = len(wml_occ[t]) == len(cb_occ.get(t, []))
            if same_count and cb_occ[t][rank] in free:
                j = cb_occ[t][rank]                              # k-й повтор на WML = k-й повтор на CoreBoard
            elif free:
                prev = [pairs_inv for pairs_inv in (jj for jj, ii in pairs.items() if target[ii] == t)]
                low = max(prev) if prev else -1                  # не выше пары предыдущего повтора
                cands = [k for k in free if k > low] or free
                tn = re.sub(r"\s+", " ", aliases_map.get(norm(wname), wname)).strip()
                j = min(cands, key=lambda k: (abs(cb[k][0] - wplace), _strip_parens(cb[k][1]) != tn, k))
            if j is not None:
                dups.append(f"WML {wplace}. {wname} -> CoreBoard {cb[j][0]}. {cb[j][1]} "
                            f"(название повторяется: WML {len(wml_occ[t])}, CoreBoard {len(cb_occ.get(t, []))}, сопоставлено по позиции)")
        elif i < len(cb) and i not in used and cb_norm[i] == t:
            j = i                                                # на этой же позиции то же название
        elif free:
            j = free[0]                                          # иначе сверху вниз среди свободных
            shifts.append(f"WML {wplace}. {wname} -> CoreBoard {cb[j][0]}. {cb[j][1]}")
        if j is None:
            wml_no_id.append((wplace, wname))
            continue
        used.add(j)
        pairs[j] = i
    cb_no_pair = [cb[j] for j in range(len(cb)) if j not in used]
    return pairs, wml_no_id, cb_no_pair, shifts, dups


def save(entries, path):
    lines = ["{", '  "levels": {']
    for i, (k, v) in enumerate(entries):
        lines.append(f"    {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}"
                     + ("," if i < len(entries) - 1 else ""))
    lines += ["  }", "}", ""]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text("\n".join(lines), "utf-8")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Уровни мобильной доски: WML top + CoreBoard -> levels_mobile.json")
    ap.add_argument("--limit", type=int, default=300, help="сколько уровней брать из топа WML")
    ap.add_argument("--url", default=CB_URL)
    ap.add_argument("--cb-html", help="страница CoreBoard из файла вместо скачивания")
    ap.add_argument("--wml-json", help="топ WML из файла вместо скачивания")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    # 1. WML первым
    if args.wml_json:
        wml = sorted(_wml_rows(_wml_items(json.loads(Path(args.wml_json).read_text("utf-8")))))[:args.limit]
    else:
        wml = fetch_wml(args.limit)
    print(f"WML: уровней {len(wml)}")

    # 2. потом CoreBoard
    if args.cb_html:
        page = Path(args.cb_html).read_text("utf-8")
    else:
        try:
            page = http_get(args.url)
        except RuntimeError as e:
            sys.exit(f"Не удалось скачать CoreBoard: {e}. levels_mobile.json не тронут.")
    cb, aliases = parse_cb(page)
    print(f"CoreBoard: уровней {len(cb)}, алиасов {len(aliases)}")

    try:
        old_n = sum(1 for k in json.loads(LEVELS_FILE.read_text("utf-8"))["levels"] if not k.startswith("x"))
    except (OSError, ValueError, KeyError, AttributeError):
        old_n = 0
    if not cb or not wml or (not args.force and old_n and len(cb) < old_n * 0.8):
        sys.exit(f"Подозрительно мало уровней (WML {len(wml)}, CoreBoard {len(cb)}, в файле было {old_n}). "
                 f"levels_mobile.json не тронут. Проверь разметку или запусти с --force.")

    try:
        al = json.loads(ALIASES_FILE.read_text("utf-8")) or {}
    except (OSError, ValueError):
        al = {}
    aliases_map = {norm(k): v for k, v in al.items()}

    # 3. сопоставление
    pairs, wml_no_id, cb_no_pair, shifts, dups = align(wml, cb, aliases_map)

    entries, taken_keys, same_names = [], set(), []
    for j, (place, name, T, t) in enumerate(cb):
        e = {"place": place, "name": name, "T": T, "t": t}
        if j in pairs:
            e["wml"], e["wml_name"] = wml[pairs[j]][0], wml[pairs[j]][1]
        else:
            e["wml"], e["wml_name"] = None, None
        key = name                  # ID = название на CoreBoard
        if key in taken_keys:       # два уровня с совсем одинаковым названием на CoreBoard: второму ID "Название #место"
            key = f"{name} #{place}"
            same_names.append(f"CoreBoard {place}. {name} -> ID {key!r}")
        taken_keys.add(key)
        entries.append((key, e))
    place_of = {name: place for place, name, _, _ in cb}
    for alias, like, T, t in aliases:
        entries.append(("x" + alias, {"name": alias, "like": like, "place": place_of[like], "T": T, "t": t}))

    # 4. отчёт
    rep = [f"WML: {len(wml)}, CoreBoard: {len(cb)}, пар: {len(pairs)}",
           f"Уровни WML без ID (на CoreBoard не найдены, в пары не попали): {len(wml_no_id)}"]
    rep += [f"  WML {p}. {n}" for p, n in wml_no_id]
    rep.append(f"Уровни CoreBoard без пары на WML: {len(cb_no_pair)}")
    rep += [f"  CoreBoard {p}. {n}" for p, n, _, _ in cb_no_pair]
    rep.append(f"Позиции не совпали, пару нашли поиском сверху вниз: {len(shifts)}")
    rep += [f"  {s}" for s in shifts]
    rep.append(f"Повторяющиеся названия, сопоставлены по позиции (уровень выше / ниже): {len(dups)}")
    rep += [f"  {s}" for s in dups]
    if same_names:
        rep.append(f"Одинаковые названия на CoreBoard (ID второго с #места): {len(same_names)}")
        rep += [f"  {s}" for s in same_names]
    text = "\n".join(rep) + "\n"
    print(text if len(text) < 6000 else text[:6000] + "...\n(полный отчёт в mobile_levels_report.txt)")

    if args.dry_run:
        print("--dry-run: файлы не записаны.")
        return
    save(entries, LEVELS_FILE)
    REPORT_FILE.write_text(text, "utf-8")
    print(f"{LEVELS_FILE.name} и {REPORT_FILE.name} записаны.")


main()
