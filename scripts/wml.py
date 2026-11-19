#!/usr/bin/env python3
"""
World Mobile List -> наш формат, всё в одном файле.

Один запуск делает три шага:
  1. топ игроков (API сайта /api/v1/players/ranking/?after=N)          -> wml/wml_top.json
  2. рекорды каждого игрока (панель игрока, парсится из HTML)          -> wml/wml_records.jsonl
  3. перевод в наш формат (ID игрока по coreboard_id_map.json, ID уровня по названию, сверху топа вниз):
       wml/wml_achievements.json            {"users": {"ID": [[ID уровня, процент, "ссылка"], ...]}}
       wml/wml_achievements_with_nicks.json {"users": {"ID — ник": [[ID уровня, процент, "название", "ссылка"], ...]}}
       wml/wml_levels_not_found.txt         уровни, ID которых по названию не нашёлся
       wml/wml_players_no_id.txt            игроки, которых нет в coreboard_id_map.json (в JSON не попадают)

Запуск:
  python wml.py                  всё сразу
  python wml.py --limit 5        проверка: рекорды только у первых 5 игроков
  python wml.py --skip-top       не качать топ заново, взять wml/wml_top.json
  python wml.py --skip-records   не качать рекорды, только перевести готовый wml/wml_records.jsonl
  python wml.py --by-rank        игроки в итоговых JSON в порядке топа (по умолчанию по ID, как в других файлах)
  python wml.py --keep-banned    не выкидывать забаненных игроков (по умолчанию они пропускаются)

Шаг 2 докачивается: если прервать, при новом запуске уже скачанные игроки пропускаются.
Уровни WML сравниваются с глобалом по ID (ingame_id), а не только по названию.
Нужен только Python, ничего устанавливать не надо.
"""
import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.request

import levels

SITE = "https://worldmobilelist.com"
RANKING_URL = SITE + "/api/v1/players/ranking/?after={after}"   # по 50 игроков за запрос
PAGE_SIZE = 50
# Панель игрока (лёгкий запрос, который сайт делает при клике на игрока, около 9 КБ вместо 42 КБ у всей страницы)
PLAYER_URL = SITE + "/demonlist/statsviewer/pane/player/{id}"
# Страница уровня. В адресе стоит МЕСТО уровня в списке (/demonlist/3 = третий), а не его внутренний ID на сайте
# (у того же уровня в list-data может быть 1136): с внутренним ID откроется чужой уровень или 404.
LEVEL_URL = SITE + "/demonlist/{pos}"

LEVELS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/levels_tp.json"
_HERE = os.path.dirname(os.path.abspath(__file__))
# Карта игроков лежит в корне репозитория (папка выше scripts/), запасной вариант: рядом со скриптом
ID_MAP_FILE = next((p for p in (os.path.join(os.path.dirname(_HERE), "coreboard_id_map.json"),
                                os.path.join(_HERE, "coreboard_id_map.json")) if os.path.exists(p)),
                   os.path.join(os.path.dirname(_HERE), "coreboard_id_map.json"))
GLOBAL_LIST_URL = "https://api.demonlist.org/level/classic/list?limit={limit}&offset={offset}"

API_TRIES = 4

# Особые названия уровней и сам поиск уровней по названию лежат в levels.py (общий для всех скриптов).


# =========================
# Общие мелочи
# =========================

def http_get(url, timeout=30, json_accept=False):
    """GET с повторами (до API_TRIES раз). 404/403 и подобное не повторяются. Возвращает текст ответа."""
    import ssl
    import gzip
    last = None
    for attempt in range(1, API_TRIES + 1):
        try:
            # Язык принудительно английский: заголовки блоков ("Main list", "First victories") разбираются по ним
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://worldmobilelist.com/",
            }
            if json_accept:
                headers["Accept"] = "application/json"
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout, context=ctx) as r:
                raw = r.read()
            # Сервер может отдавать gzip-сжатое содержимое
            if raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
            return raw.decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:
            last = e
        print(f"    попытка {attempt}/{API_TRIES} не удалась: {last}")
        if attempt < API_TRIES:
            time.sleep(2 * attempt)
    raise last


def get_json(url):
    payload = json.loads(http_get(url, timeout=20, json_accept=True))
    time.sleep(0.2)  # небольшая пауза, чтобы не нагружать сервер
    return payload


def normalize(value):
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def normalize_nick(nick):
    nick = re.sub(r'^[\U0001F1E6-\U0001F1FF]{2}\s*', '', nick.strip())
    return normalize(nick)


def strip_clan(nick):
    """'[67] Kristage' -> 'Kristage'."""
    return re.sub(r'^\[[^\]]*\]\s*', '', nick.strip())


YOUTUBE_ID = re.compile(
    r'(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^\s]*?&)?v=|shorts/|live/|embed/|v/))([A-Za-z0-9_-]{11})')


def normalize_link(link):
    """Как в командах: https://www.youtube.com/watch?v=ID. Не YouTube и пустое остаётся как есть."""
    link = str(link).strip()
    m = YOUTUBE_ID.search(link)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else link


def pick(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


# =========================
# Одноимённые уровни в WML (например, два Pandemonium)
# =========================
# Наш поиск уровня идёт по названию и всегда даёт ОДИН уровень (тот, что выше и совпадает с глобалом).
# Если в WML два уровня с таким названием, рекорд на нижнем нельзя засчитывать: он не про тот уровень.
# Какой из двух у игрока, видно по месту в списке (#pos в панели игрока): выше = меньше число.
DUP_GAP = 15        # места одного названия ближе друг к другу считаются одним уровнем (список за неделю чуть сдвигается)
DUP_NAMES = None    # None = проверять все повторяющиеся названия; или множество, например {"pandemonium"}

# Кэш ingame_id уровней WML: id уровня на WML -> ingame_id (с data-copy на странице уровня WML)
WML_LEVEL_INGAME_CACHE = os.path.join("wml", "wml_level_ingame_by_id.json")


def duplicate_cutoffs(pairs):
    """pairs: (название, место) всех рекордов. Возвращает {название: (последнее место верхнего уровня,
    места верхнего, места нижних)} только для названий, под которыми в WML нашлось несколько уровней."""
    by_name, shown = {}, {}
    for name, pos in pairs:
        if pos is None:
            continue
        n = normalize(name)
        by_name.setdefault(n, set()).add(int(pos))
        shown.setdefault(n, name)
    cut = {}
    for n, places in by_name.items():
        if DUP_NAMES is not None and n not in {normalize(x) for x in DUP_NAMES}:
            continue
        places = sorted(places)
        upper = [places[0]]
        for p in places[1:]:
            if p - upper[-1] > DUP_GAP:
                break
            upper.append(p)
        lower = places[len(upper):]
        if lower:
            cut[n] = (upper[-1], upper, lower, shown[n])
    return cut


def is_lower_duplicate(cut, name, pos):
    """True, если рекорд на нижнем из одноимённых уровней (его не учитываем). Без места рекорд остаётся."""
    if pos is None:
        return False
    c = cut.get(normalize(name))
    return bool(c) and int(pos) > c[0]


def report_duplicates(cut, dropped, log=print):
    for n, (_end, upper, lower, shown) in sorted(cut.items()):
        log(f"  WML: одноимённые уровни {shown!r}: учитываю #{upper[0]}"
            + (f"..#{upper[-1]}" if upper[-1] != upper[0] else "")
            + f", не учитываю нижний (#{', #'.join(map(str, lower))}); отброшено записей: {dropped.get(n, 0)}")


# =========================
# Шаг 1. Топ игроков
# =========================

def items_of(payload):
    """Список игроков из ответа API: сразу список или внутри словаря (data/players/results...)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "players", "results", "items", "ranking"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def normalize_player(item):
    country = pick(item, "nationality", "country", "country_code", "cc")
    if isinstance(country, dict):
        country = pick(country, "country_code", "code", "cc", "name")
    return {
        "rank": pick(item, "rank", "position", "place"),
        "id": pick(item, "id", "player_id", "user_id"),
        "name": pick(item, "name", "nickname", "username", "nick"),
        "score": round(pick(item, "score", "points", "value"), 2) if isinstance(pick(item, "score", "points", "value"), (int, float)) else pick(item, "score", "points", "value"),
        "country": country,
        "banned": bool(item.get("banned", False)),
    }


def step_top(out_dir, delay, max_pages, keep_banned=False):
    print("=== Шаг 1. Топ игроков ===")
    raw, seen, after = [], set(), 0
    for _ in range(max_pages):
        items = items_of(get_json(RANKING_URL.format(after=after)))
        new = [it for it in items if isinstance(it, dict) and str(pick(it, "id", "player_id", "user_id")) not in seen]
        print(f"after={after}: получено {len(items)}, новых {len(new)}")
        if not new:
            break
        for it in new:
            seen.add(str(pick(it, "id", "player_id", "user_id")))
        raw.extend(new)
        after += PAGE_SIZE
        time.sleep(delay)

    players = [normalize_player(it) for it in raw]
    banned = [p for p in players if p["banned"]]
    if banned and not keep_banned:
        print(f"Забаненных в топе: {len(banned)}, пропускаю (чтобы оставить: --keep-banned)")
        players = [p for p in players if not p["banned"]]
    with open(os.path.join(out_dir, "wml_top_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "wml_top.json"), "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, indent=1)
    print(f"Игроков в топе: {len(players)}\n")
    return players


# =========================
# Шаг 2. Рекорды каждого игрока
# =========================

SECTION = re.compile(
    r'<section class="panel pf-group"><h3>(?P<title>.*?)<small>.*?</small></h3>'
    r'<div class="chips">(?P<body>.*?)</div></section>', re.S)
CHIP = re.compile(
    r'<a class="chip[^"]*" href="(?P<video>[^"]*)"[^>]*>'
    r'<i>#(?P<pos>\d+)</i><span>(?P<name>.*?)</span>(?:<em>(?P<pct>\d+)%</em>)?</a>', re.S)
# Блоки панели игрока, которые не рекорды и в записи не попадают (названия в нижнем регистре).
# "Created" - уровни, которые человек создал, а не прошёл; "First victories" - лишь пометка к тем, что
# уже есть в Main / Extended / Unbounded list (уникального там ничего нет). Появится ещё такой блок: допиши его сюда.
SKIP_SECTIONS = {"created", "first victories"}
NAME = re.compile(r'<div class="pf-name"><h2>(.*?)</h2>', re.S)
COUNTRY = re.compile(r'<div class="pf-id">.*?<span data-cc="([A-Z]{2})"', re.S)


def parse_panel(page):
    """Рекорды игрока из HTML панели. Возвращает словарь или None, если панели игрока нет."""
    pane = page[page.find('id="pane-box"'):] if 'id="pane-box"' in page else page
    name = NAME.search(pane)
    if not name and "pf-group" not in pane:
        return None

    # Какой из блоков Main / Extended / Unbounded list не важен: берём уровень, процент и видео.
    # In progress отличается процентом (меньше 100). Пропускаем блоки, где нет рекордов (SKIP_SECTIONS).
    records = []
    for sec in SECTION.finditer(pane):
        title = html.unescape(sec["title"]).strip().lower()
        if title in SKIP_SECTIONS:
            continue
        for c in CHIP.finditer(sec["body"]):
            records.append({
                "level": html.unescape(c["name"]).strip(),
                "percent": int(c["pct"]) if c["pct"] is not None else 100,
                "video": html.unescape(c["video"]).strip(),
                "pos": int(c["pos"]),
            })

    cc = COUNTRY.search(pane)
    return {
        "nick": html.unescape(name.group(1)).strip() if name else "",
        "country": cc.group(1) if cc else "",
        "records": records,
    }


# =========================
# Страница УРОВНЯ (используется в list_wml.py)
# =========================
# Записи уровня лежат в <ol id="records"> целиком: те, что скрыты за кнопкой «Show all», тоже есть в HTML
# (у них просто атрибут hidden), так что второй запрос не нужен.
LEVEL_ID = re.compile(r'id="copy-id"[^>]*data-copy="(\d+)"')
LEVEL_RECORDS = re.compile(r'<ol[^>]*id="records"[^>]*>(.*?)</ol>', re.S)
LEVEL_ROW = re.compile(r'<li class="r rec[^"]*"[^>]*>(.*?)</li>', re.S)
ROW_PLAYER = re.compile(r'<a class="r-main" href="/demonlist/player/(\d+)"[^>]*>\s*<b>(.*?)</b>', re.S)
ROW_PERCENT = re.compile(r'<b class="tn">(\d+)%</b>')
ROW_VIDEO = re.compile(r'<a class="rec-video" href="([^"]*)"')
ROW_COUNTRY = re.compile(r'data-cc="([A-Z]{2})"')
PAGE_TITLE = re.compile(r'<title>(.*?)</title>', re.S)
LEVEL_FIRST = re.compile(r'id="by"[^>]*>.*?first beaten by\s*<a href="/demonlist/player/(\d+)"[^>]*>(.*?)</a>', re.S)


def parse_level_page(page):
    """Страница уровня WML -> {"ingame": ID уровня в игре или None,
                               "first": {"id", "nick"} или None (кто прошёл первым),
                               "records": [{"id": ID игрока на WML, "nick", "cc", "percent", "video"}]}.
    Возвращает None, если это не страница уровня (нет ни кнопки с ID, ни блока записей)."""
    m_id = LEVEL_ID.search(page)
    m_rec = LEVEL_RECORDS.search(page)
    if not m_id and not m_rec:
        return None

    records = []
    for row in LEVEL_ROW.finditer(m_rec.group(1) if m_rec else ""):
        body = row.group(1)
        pl = ROW_PLAYER.search(body)
        if not pl:
            continue
        pct = ROW_PERCENT.search(body)
        vid = ROW_VIDEO.search(body)
        cc = ROW_COUNTRY.search(body)
        records.append({
            "id": pl.group(1),
            "nick": html.unescape(pl.group(2)).strip(),
            "cc": cc.group(1) if cc else None,
            "percent": int(pct.group(1)) if pct else 100,
            "video": html.unescape(vid.group(1)).strip() if vid else None,
        })

    first = LEVEL_FIRST.search(page)
    title = PAGE_TITLE.search(page)
    return {
        "title": html.unescape(title.group(1)).strip() if title else "",
        "ingame": m_id.group(1) if m_id else None,
        "first": {"id": first.group(1), "nick": html.unescape(first.group(2)).strip()} if first else None,
        "records": records,
    }


def step_records(players, out_dir, url_template, limit, delay):
    print("=== Шаг 2. Рекорды игроков ===")
    path = os.path.join(out_dir, "wml_records.jsonl")
    todo = players[:limit] if limit else players

    done = set()
    if os.path.exists(path):  # докачка
        with open(path, encoding="utf-8") as f:
            done = {str(json.loads(line)["id"]) for line in f if line.strip()}
        print(f"Уже скачано раньше: {len(done)}")

    failed = []
    with open(path, "a", encoding="utf-8") as out:
        for i, p in enumerate(todo, 1):
            pid = str(p["id"])
            if pid in done:
                continue
            try:
                data = parse_panel(http_get(url_template.format(id=pid)))
            except Exception as e:
                print(f"[{i}/{len(todo)}] {p.get('name')} ({pid}): ОШИБКА {e}")
                failed.append(pid)
                continue
            if data is None:
                print(f"[{i}/{len(todo)}] {p.get('name')} ({pid}): панели игрока в ответе нет")
                failed.append(pid)
                continue
            data.update({"id": pid, "rank": p.get("rank"), "score": p.get("score")})
            out.write(json.dumps(data, ensure_ascii=False) + "\n")
            out.flush()
            print(f"[{i}/{len(todo)}] {data['nick'] or p.get('name')}: рекордов {len(data['records'])}")
            time.sleep(delay)

    print(f"Не удалось: {len(failed)}" + (f" ({', '.join(failed[:20])}...)" if failed else "") + "\n")

    with open(path, encoding="utf-8") as f:
        wanted = {str(p["id"]) for p in todo}
        return [rec for rec in (json.loads(line) for line in f if line.strip()) if str(rec["id"]) in wanted]


# =========================
# Шаг 3. Перевод в наш формат
# =========================

class LevelResolver:
    """Название уровня -> ID. Сам поиск лежит в levels.py (общий для всех скриптов), здесь обёртка:
    числовые ID отдаются числами, а x... строками, как и раньше.
    Порядок поиска: levels_tp.json, особые названия, глобал (список качается один раз, если понадобится)."""

    def __init__(self, levels_data):
        self.index = levels.LevelIndex(global_loader=self._load_global)
        self.index.add_tp(levels_data)
        self.index.add_special()

    @staticmethod
    def _load_global():
        return levels.fetch_global_levels(get_json, GLOBAL_LIST_URL)

    @property
    def stats(self):
        return self.index.stats

    def find(self, name):
        key = self.index.find(name)
        if key is None:
            return None
        return int(key) if key.isdigit() else key


def load_wml_level_cache():
    """Кэш: id уровня на WML -> ingame_id."""
    if os.path.exists(WML_LEVEL_INGAME_CACHE):
        with open(WML_LEVEL_INGAME_CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_wml_level_cache(cache):
    os.makedirs(os.path.dirname(WML_LEVEL_INGAME_CACHE), exist_ok=True)
    with open(WML_LEVEL_INGAME_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def fetch_wml_level_list():
    """Список уровней WML со страницы /demonlist/ в порядке списка: [(wml_level_id, название), ...]."""
    page = http_get(SITE + "/demonlist/")
    m = re.search(r'id="list-data" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        print("  Не найден #list-data на странице списка WML")
        return []
    data = json.loads(m.group(1))
    entries = [(str(e[0]), e[2]) for e in data.get("levels", [])]
    print(f"  Уровней в списке WML: {len(entries)}")
    return entries


def make_wml_ctx(entries):
    """Из списка WML: место -> (id, название) и название -> (id, название) (при повторе берётся верхний)."""
    by_pos, by_name = {}, {}
    for i, (wid, name) in enumerate(entries, 1):
        by_pos[i] = (wid, name)
        by_name.setdefault(normalize(name), (wid, name))
    return {"by_pos": by_pos, "by_name": by_name, "ingame": {}}


def locate_wml_level(ctx, name, pos):
    """id уровня на WML для рекорда. Сначала по месту (#pos), если там то же название; иначе по названию.
    Возвращает (wml_id или None, найден ли по месту)."""
    if pos is not None:
        e = ctx["by_pos"].get(int(pos))
        if e and normalize(e[1]) == normalize(name):
            return e[0], True
    e = ctx["by_name"].get(normalize(name))
    return (e[0], False) if e else (None, False)


def fetch_wml_ingame_id(wml_level_id):
    """Страница уровня WML -> ingame_id из data-copy."""
    page = http_get(f"{SITE}/demonlist/{wml_level_id}")
    m = re.search(r'id="copy-id"[^>]*data-copy="(\d+)"', page)
    return m.group(1) if m else None


def build_wml_ingame_cache(wml_ids):
    """ingame_id для нужных уровней WML (по id уровня на WML). Скачанное сохраняется в кэш."""
    cache = load_wml_level_cache()
    todo = sorted(i for i in wml_ids if i not in cache)
    if todo:
        print(f"  Загружаю ingame_id для {len(todo)} уровней WML...")
    try:
        for i, wid in enumerate(todo, 1):
            try:
                iid = fetch_wml_ingame_id(wid)
            except Exception as e:
                print(f"    уровень WML {wid}: ОШИБКА {e}")
                continue
            if iid:
                cache[wid] = iid
            else:
                print(f"    уровень WML {wid}: ingame_id на странице не найден")
            if i % 20 == 0 or i == len(todo):
                print(f"    {i}/{len(todo)}")
            time.sleep(0.2)
    finally:
        if todo:
            save_wml_level_cache(cache)
    return cache


def player_id_sort_key(player_key):
    """Числовые ID по возрастанию, затем pc-ID (как в compare.py)."""
    pid = str(player_key).split(" — ", 1)[0].strip().casefold()
    if pid.startswith("pc"):
        suffix = pid[2:]
        return (1, 0, int(suffix), suffix) if suffix.isdigit() else (1, 1, suffix)
    if pid.isdigit():
        return (0, 0, int(pid), pid)
    return (0, 1, pid)


def save_json(filename, users):
    with open(filename, "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        items = list(users.items())
        for i, (key, records) in enumerate(items):
            f.write(f'    {json.dumps(key, ensure_ascii=False)}: [\n')
            for j, rec in enumerate(records):
                f.write(f'      {json.dumps(rec, ensure_ascii=False)}{"," if j < len(records) - 1 else ""}\n')
            f.write(f'    ]{"," if i < len(items) - 1 else ""}\n')
        f.write('  }\n}\n')


def convert(players, nick_to_id, resolver, wml_ctx, global_by_ingame, global_name_to_ingame):
    """Возвращает (users, users_nicks, not_found, no_id, stats). Игроки идут от верха топа до низа.
    Уровень сравнивается с глобалом по ID (ingame_id): если ingame_id уровня WML есть в глобале, это он и есть,
    название значения не имеет. Если его в глобале нет, уровень ищется по названию (levels_tp.json и т.д.),
    но только если одноимённого другого уровня в глобале нет."""
    users, users_nicks, not_found, no_id, name_cache = {}, {}, {}, [], {}
    cut = duplicate_cutoffs((r["level"], r.get("pos")) for p in players for r in p.get("records", []))
    dropped, skipped_ingame = {}, {}
    stats = {"по ID в глобале": 0, "не в глобале, по названию": 0, "ingame_id WML неизвестен, по названию": 0,
             "пропущено (другой уровень с тем же названием)": 0}
    ingame = wml_ctx["ingame"]

    for p in sorted(players, key=lambda x: (x.get("rank") is None, x.get("rank") or 0)):
        wml_nick = p.get("nick") or ""
        clean = strip_clan(wml_nick)
        gid = nick_to_id.get(normalize_nick(clean)) or nick_to_id.get(normalize_nick(wml_nick))
        if not gid:
            no_id.append(f"#{p.get('rank')} {wml_nick} (WML id {p.get('id')}), рекордов: {len(p.get('records', []))}")
            continue

        best = {}  # ID уровня -> (процент, название, ссылка): по уровню остаётся лучший процент
        for r in p.get("records", []):
            name = r["level"]
            wml_lid, via_pos = locate_wml_level(wml_ctx, name, r.get("pos"))
            wml_iid = ingame.get(wml_lid) if wml_lid else None

            if wml_iid is not None and wml_iid in global_by_ingame:
                global_level = global_by_ingame[wml_iid]
                if global_level.get("name") == name:
                    level_id = int(wml_iid)  # сравнение по ID: уровень есть в глобале под тем же названием
                    stats["по ID в глобале"] += 1
                else:
                    # ingame_id есть в глобале, но под другим названием — другой уровень с тем же именем в WML
                    skipped_ingame[name] = skipped_ingame.get(name, 0) + 1
                    stats["пропущено (другой уровень с тем же названием)"] += 1
                    continue
            else:
                if wml_iid is None:
                    # ID не узнали (нет страницы/кнопки с ID): как раньше, по названию и месту в списке
                    if not via_pos and is_lower_duplicate(cut, name, r.get("pos")):
                        dropped[normalize(name)] = dropped.get(normalize(name), 0) + 1
                        continue
                    stats["ingame_id WML неизвестен, по названию"] += 1
                else:
                    g = global_name_to_ingame.get(name)
                    if g is not None and g != wml_iid:        # в глобале под этим названием другой уровень
                        skipped_ingame[name] = skipped_ingame.get(name, 0) + 1
                        stats["пропущено (другой уровень с тем же названием)"] += 1
                        continue
                    stats["не в глобале, по названию"] += 1
                if name not in name_cache:
                    name_cache[name] = resolver.find(name)
                level_id = name_cache[name]
                if level_id is None:
                    nf = not_found.setdefault(name, [0, clean])
                    nf[0] += 1
                    continue
                # название нашло глобальный уровень с другим ID, чем у уровня WML: это не тот уровень
                if wml_iid is not None and str(level_id) != wml_iid and str(level_id) in global_by_ingame:
                    skipped_ingame[name] = skipped_ingame.get(name, 0) + 1
                    stats["пропущено (другой уровень с тем же названием)"] += 1
                    continue

            percent = int(r.get("percent", 100))
            if level_id not in best or percent > best[level_id][0]:
                best[level_id] = (percent, name, normalize_link(r.get("video", "")))

        if best:
            rows = sorted(best.items(), key=lambda kv: str(kv[0]))
            users.setdefault(gid, []).extend([[lid, pct, link] for lid, (pct, _n, link) in rows])
            users_nicks.setdefault(f"{gid} — {clean}", []).extend(
                [[lid, pct, n, link] for lid, (pct, n, link) in rows])
    report_duplicates(cut, dropped)
    for n, cnt in sorted(skipped_ingame.items(), key=lambda kv: -kv[1]):
        print(f"  WML: пропущено записей на '{n}' (ID не совпал с глобалом): {cnt}")
    return users, users_nicks, not_found, no_id, stats


def load_nick_to_id():
    """coreboard_id_map.json ({ID CoreBoard: {nick, gdr_id}}) -> {ник (нормализованный): наш ID (gdr_id)}."""
    try:
        with open(ID_MAP_FILE, encoding="utf-8") as f:
            id_map = json.load(f)
    except (OSError, ValueError) as error:
        raise SystemExit(f"{ID_MAP_FILE}: не удалось прочитать ({error}). Останавливаюсь.")
    nick_to_id = {}
    for row in id_map.values():
        if isinstance(row, dict) and row.get("nick") and row.get("gdr_id"):
            nick_to_id.setdefault(normalize_nick(str(row["nick"])), str(row["gdr_id"]).strip().casefold())
    return nick_to_id


def step_convert(records, out_dir, by_rank):
    print("=== Шаг 3. Перевод в наш формат ===")
    print(f"Игроков с рекордами: {len(records)}")
    print(f"levels_tp.json: беру с GitHub (raw кешируется до ~5 минут после правки); игроки: {ID_MAP_FILE}")
    nick_to_id = load_nick_to_id()
    resolver = LevelResolver(get_json(LEVELS_URL)["levels"])

    # 1. Список уровней WML (по порядку списка = по местам)
    wml_ctx = make_wml_ctx(fetch_wml_level_list())

    # 2. id уровней WML, которые встречаются в рекордах
    needed, total, by_pos = set(), 0, 0
    for p in records:
        for r in p.get("records", []):
            total += 1
            lid, via_pos = locate_wml_level(wml_ctx, r["level"], r.get("pos"))
            by_pos += via_pos
            if lid:
                needed.add(lid)
    if total and by_pos / total < 0.9:
        print(f"  ВНИМАНИЕ: по месту в списке совпало только {by_pos} из {total} рекордов "
              f"(список WML успел сдвинуться или порядок в list-data другой), остальные ищутся по названию")

    # 3. ingame_id этих уровней на WML (кэш в файле, докачивает недостающие)
    wml_ctx["ingame"] = build_wml_ingame_cache(needed)

    # 4. Глобал: ingame_id -> уровень, и название -> ingame_id
    global_levels = levels.fetch_global_levels(get_json, GLOBAL_LIST_URL)
    global_by_ingame, global_name_to_ingame = {}, {}
    for l in global_levels:
        iid = pick(l, "ingame_id", "id")
        if iid is None:
            continue
        iid = str(iid)
        global_by_ingame[iid] = l
        if l.get("name") is not None:
            global_name_to_ingame.setdefault(l["name"], iid)
    print(f"  Уровней в глобале: {len(global_by_ingame)}")

    users, users_nicks, not_found, no_id, stats = convert(
        records, nick_to_id, resolver, wml_ctx, global_by_ingame, global_name_to_ingame)

    if not by_rank:
        users = dict(sorted(users.items(), key=lambda kv: player_id_sort_key(kv[0])))
        users_nicks = dict(sorted(users_nicks.items(), key=lambda kv: player_id_sort_key(kv[0])))

    p = lambda n: os.path.join(out_dir, n)
    save_json(p("wml_achievements.json"), users)
    save_json(p("wml_achievements_with_nicks.json"), users_nicks)
    with open(p("wml_levels_not_found.txt"), "w", encoding="utf-8") as f:
        for name, (cnt, example) in sorted(not_found.items(), key=lambda kv: -kv[1][0]):
            f.write(f"{name} | рекордов: {cnt} | например у {example}\n")
    with open(p("wml_players_no_id.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(no_id) + ("\n" if no_id else ""))

    print(f"\nИгроков с рекордами в JSON: {len(users)}; записей: {sum(map(len, users.values()))}")
    print(f"Игроков без ID в coreboard_id_map.json: {len(no_id)}")
    print(f"Уровней, не найденных по названию: {len(not_found)}")
    print("Как сопоставлены рекорды:", {k: v for k, v in stats.items() if v})
    print("Названия найдены:", {k: v for k, v in resolver.stats.items() if v})
    print(f"Файлы лежат в папке {out_dir}")


# =========================
# Запуск
# =========================

def main():
    ap = argparse.ArgumentParser(description="World Mobile List: топ, рекорды и перевод в наш формат одним запуском")
    ap.add_argument("--out-dir", default="wml", help="папка для всех файлов (создаётся сама). По умолчанию wml")
    ap.add_argument("--url", default=PLAYER_URL, help="адрес панели игрока, {id} заменяется на ID сайта")
    ap.add_argument("--limit", type=int, default=0, help="рекорды только у первых N игроков (для проверки)")
    ap.add_argument("--delay", type=float, default=0.4, help="пауза между запросами к сайту, секунд")
    ap.add_argument("--max-pages", type=int, default=400, help="защита от бесконечного цикла при чтении топа")
    ap.add_argument("--keep-banned", action="store_true", help="не выкидывать забаненных игроков из топа")
    ap.add_argument("--skip-top", action="store_true", help="не качать топ, взять <out-dir>/wml_top.json")
    ap.add_argument("--skip-records", action="store_true", help="не качать рекорды, перевести готовый wml_records.jsonl")
    ap.add_argument("--by-rank", action="store_true", help="игроки в итоговых JSON в порядке топа, а не по ID")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    top_path = os.path.join(args.out_dir, "wml_top.json")
    rec_path = os.path.join(args.out_dir, "wml_records.jsonl")

    if args.skip_records:
        if not os.path.exists(rec_path):
            raise SystemExit(f"Нет {rec_path}: сначала запустите без --skip-records")
        with open(rec_path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
    else:
        if args.skip_top:
            if not os.path.exists(top_path):
                raise SystemExit(f"Нет {top_path}: сначала запустите без --skip-top")
            with open(top_path, encoding="utf-8") as f:
                players = json.load(f)
            print(f"Топ взят из {top_path}: {len(players)} игроков\n")
        else:
            players = step_top(args.out_dir, args.delay, args.max_pages, args.keep_banned)

        if not players or any(p.get("id") is None for p in players):
            raise SystemExit("В ответе API нет ID игроков (или он пустой). Посмотрите wml_top_raw.json: "
                             "нужно поправить названия полей в normalize_player().")
        records = step_records(players, args.out_dir, args.url, args.limit, args.delay)

    step_convert(records, args.out_dir, args.by_rank)


if __name__ == "__main__":
    main()