# Рейтинг по геометрическому среднему N лучших записей (меньше = лучше).
# 100% = h (место уровня); иначе f = h*a*(T/c)^(ln(b/a)/ln(T/t)), T и t из levels_tp.json (только с GitHub).
# Источники: глобал (всегда), pointercrate (напрямую, в т.ч. игроки без линка, помечены [PC]),
# другие листы из реестра (сейчас wml; игроки без линка помечены [WML]), my_extras.
# Новый лист добавляется одним файлом list_<название>.py (см. lists_core.py), здесь менять ничего не нужно:
# --src <название>, метка, имя файла и склейка по links.json появятся сами.
# Названия уровней на других листах ищет levels.py (общий для всех скриптов).
# Для игроков, которых нет в глобале, флаг берётся с Pointercrate / из данных листа.
# links.json: аккаунты человека на других листах лежат в блоке other_lists ({"pointercrate": [...], "wml": [...]}).
# Ключ записи из цифр = аккаунт глобала, pc7 / wml5 = человек без глобала (префикс задаёт лист).
# Старый формат links.json (списки прямо в записи) тоже читается.
#
#   python gd-rating.py --src all
#   python gd-rating.py --src pointercrate,wml
#   python gd-rating.py --src pointercrate --levels 50
#   python gd-rating.py --add 13506:3409:91 --add 13506:x1:95
#   python gd-rating.py --long                 подробный вывод (по строке на запись)
import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import levels as levelsearch  # поиск уровней по названию (общий для всех скриптов)
import lists_core as L  # реестр листов

HERE = Path(__file__).resolve().parent
DL = "https://api.demonlist.org/"
PC = "https://pointercrate.com/api/"
RAW = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/"
FILE_SOURCES = {}
LIST_SOURCES = [s.name for s in L.sources()]  # листы из реестра, которые умеют качать данные сами (wml и т.д.)
ALL_SOURCES = {"pointercrate", "my_extras", *LIST_SOURCES}
SOURCE_ORDER = ["pointercrate", *LIST_SOURCES, "my_extras"]  # порядок в имени файла
SOURCE_SHORT = {"my_extras": "my", **{s.name: s.short for s in L.all_lists()}}
OTHER = "other_lists"  # блок аккаунтов человека на других листах в links.json
A, B = 2, 4

# Особые названия уровней (ID уровня в глобале -> как писать в рейтинге) лежат в levels.py, общем для всех скриптов.
SPECIAL_LEVEL_NAMES = levelsearch.SPECIAL_LEVEL_NAMES

p = argparse.ArgumentParser()
p.add_argument("--src", default="global",
               help="pointercrate, " + ", ".join(LIST_SOURCES) + ", my_extras или all (через запятую)")
p.add_argument("--n", type=int, default=6, help="сколько лучших записей берём")
p.add_argument("--cap", type=float, default=300, help="макс. стоимость записи; недостающие добираются этим значением")
p.add_argument("--cap-place", dest="cap_place", type=float, default=300, help="проверять уровни до этого места")
p.add_argument("--levels", type=int, default=0, help="только первые N уровней (быстрый тест)")
p.add_argument("--show", type=int, default=50, help="сколько игроков печатать")
p.add_argument("--long", action="store_true", help="подробный вывод (по строке на запись)")
p.add_argument("--conc", type=int, default=20, help="параллельных запросов")
p.add_argument("--save", type=int, default=10, help="сохранять кеш каждые N задач")
p.add_argument("--ttl", type=int, default=3600, help="секунд, пока кеш уровня свежий")
p.add_argument("--add", action="append", default=[], help="своя запись: ID_игрока:ID_уровня:процент")
p.add_argument("--ids", action="store_true", help="добавлять ID игрока в конец строки")
p.add_argument("--waiting", action="store_true",
               help="записать waiting_records.json: непринятые записи глобала (статус waiting), которые вошли бы в топ-N игрока")
p.add_argument("--waiting-ttl", dest="waiting_ttl", type=int, default=3 * 3600,
               help="секунд, пока список waiting-записей игрока в кеше считается свежим")
p.add_argument("--waiting-max-score", dest="waiting_max_score", type=float, default=250,
               help="в waiting-файлы попадают только игроки, у которых ПОСЛЕ принятия всех waiting-записей очков меньше этого")
p.add_argument("--min-score", dest="min_score", type=float, default=None,
               help="показывать игроков с очками не меньше этого (меньше = лучше)")
p.add_argument("--max-score", dest="max_score", type=float, default=None,
               help="показывать игроков с очками не больше этого, например 160; "
                    "подсчёт рекордов только с Pointercrate тогда тоже идёт только по ним")
p.add_argument("--pc-conc", dest="pc_conc", type=int, default=20,
               help="сколько запросов к Pointercrate одновременно (при 429 уменьшите)")
p.add_argument("--fresh-country", dest="fresh_country", action="store_true",
               help="не брать страны Pointercrate из кеша (перезапросить)")
p.add_argument("--list-conc", dest="list_conc", type=int, default=3,
               help="сколько страниц игроков других листов (wml и т.д.) качать одновременно (ходим бережнее)")
p.add_argument("--list-ttl", dest="list_ttl", type=int, default=7 * 86400,
               help="секунд, пока страница игрока другого листа считается свежей, даже если его версия не менялась")
args = p.parse_args()

N = args.n
wanted = [s.strip() for s in args.src.split(",") if s.strip()]
SOURCES = set(ALL_SOURCES) if "all" in wanted else set(wanted)
SOURCES.discard("global")


def get_json(url, tries=8):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gl-ext-top6"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429 and e.code < 500:
                break
        except Exception as e:
            last = e
        time.sleep(min(2 ** i, 30) + 1)
    raise last


def load_paged(path, key, step, stop=None):
    """Грузит список постранично. stop(arr) -> True, когда дальше качать не нужно."""
    out, offset = [], 0
    while True:
        d = get_json(f"{DL}{path}?limit={step}&offset={offset}")
        arr = (d.get("data") or {}).get(key) or []
        if not arr:
            return out
        out += arr
        if stop and stop(arr):
            return out
        offset += step


def levels_past_cap(arr):
    """Страница отсортирована по месту и её последний уровень уже ниже порога - дальше не нужно."""
    pl = [float(l["placement"]) for l in arr]
    return pl == sorted(pl) and pl[-1] > args.cap_place


# ---------- формула ----------
def score(h, c, T, t):
    if c >= 100:
        return h
    if not (t < T) or c < t or c <= 0:
        return None
    return h * A * (T / c) ** (math.log(B / A) / math.log(T / t))


def geo_mean(values):
    return math.exp(sum(math.log(x) for x in values) / len(values))


# ---------- файлы репозитория ----------
def load_repo_json(name):
    f = HERE / name
    try:
        if f.exists():
            print(f"{name}: беру ЛОКАЛЬНЫЙ файл {f}")
            return json.loads(f.read_text("utf-8"))
        print(f"{name}: беру с GitHub (raw кешируется до ~5 минут после правки)")
        return get_json(RAW + name)
    except Exception as e:
        print(f"{name}: {e}")
        return {}


def load_tp():
    """levels_tp.json берём ТОЛЬКО с GitHub (локальный файл не используется).
    Без него T/t были бы запасными для всех уровней и рейтинг получился бы неверным, поэтому при сбое выходим."""
    print("levels_tp.json: беру с GitHub (raw кешируется до ~5 минут после правки)")
    try:
        levels = get_json(RAW + "levels_tp.json").get("levels", {})
    except Exception as e:
        raise SystemExit(f"levels_tp.json: не удалось загрузить с GitHub ({e}). Останавливаюсь.")
    if not levels:
        raise SystemExit("levels_tp.json: на GitHub пустой список уровней. Останавливаюсь.")
    return levels


is_global = L.is_global  # аккаунт глобала: ID начинается с цифры (pc7, wml5 и т.п. глобалом не считаются)


def link_sites(entry):
    """{"pointercrate": [...], "wml": [...], ...} из записи links.json.
    Новый формат: аккаунты на других листах лежат в блоке other_lists. Старый тоже понимаем:
    списки прямо в записи (поля _name и channels пропускаем) или голый список (это Pointercrate)."""
    if not isinstance(entry, dict):
        return {"pointercrate": entry}
    sources = [{k: v for k, v in entry.items() if k not in ("_name", "channels", OTHER)}]
    if isinstance(entry.get(OTHER), dict):
        sources.append(entry[OTHER])
    sites = {}
    for src in sources:
        for site, ids in src.items():
            ids = ids if isinstance(ids, list) else [ids]
            sites.setdefault(site, []).extend(x for x in ids if x is not None)
    return sites


def load_links():
    """Возвращает функции и данные:
    owner(site, acc_id) - ID аккаунта на листе (pointercrate, wml, ...) -> главный ID человека (цифры, pc... и т.п.) или None;
    canon(uid)          - любой ID человека -> главный ID (склейки fix.json + доп. аккаунты из links.json);
    removals            - видео, которые у игрока не учитываются;
    accounts(uid)       - все аккаунты человека: {"dl": [...], "pointercrate": [...], "wml": [...], ...}.
    """
    links = load_repo_json("links.json")
    fix = load_repo_json("fix.json")
    absorb = {}
    for m in fix.get("merges", []):
        for a in m.get("absorb", []):
            absorb[str(a["id"])] = str(m["keep"])

    def canon0(i):
        i, seen = str(i), set()
        while i in absorb and i not in seen:  # цепочки склеек A->B->C
            seen.add(i)
            i = absorb[i]
        return i

    names = L.list_names()
    site_map = {n: {} for n in names}  # лист -> {ID аккаунта на листе: главный ID человека}
    alias = {}

    def empty():
        return {"dl": [], **{n: [] for n in names}}

    people = {}  # главный ID -> {"dl": [аккаунты глобала, главный первым], "pointercrate": [...], "wml": [...]}

    def grp(main):
        return people.setdefault(main, empty())

    def add_uniq(lst, x):
        x = str(x)
        if x not in lst:
            lst.append(x)

    for main_id, entry in links.items():
        main = canon0(main_id)
        g = grp(main)
        if is_global(main):
            add_uniq(g["dl"], main)
        if is_global(main_id):
            add_uniq(g["dl"], main_id)
        for site, ids in link_sites(entry).items():
            for x in ids:
                if site in site_map:
                    site_map[site][str(x)] = main
                    add_uniq(g[site], x)
                elif any(s in str(site).lower() for s in ("demonlist", "global")):
                    alias[str(x)] = main  # доп. аккаунты того же человека на Demonlist
                    add_uniq(g["dl"], x)
    for main_id in links:
        alias.setdefault(str(main_id), canon0(main_id))
    # аккаунты, склеенные через fix.json, тоже считаются аккаунтами этого человека
    for a in absorb:
        main = canon0(a)
        g = grp(main)
        add_uniq(g["dl"], main)
        add_uniq(g["dl"], a)

    def canon(uid):
        uid = str(uid)
        return canon0(alias.get(uid, uid))

    def accounts(uid):
        """Все известные аккаунты человека (главный аккаунт глобала первым).
        Если человека нет в links.json, ключи вида pc<id>, wml<id> и т.п. понимаем сами."""
        c = canon(uid)
        if c in people:
            return people[c]
        src, rest = L.key_parts(c)
        acc = empty()
        if src:
            acc[src.name] = [rest]
        else:
            acc["dl"] = [c]
        return acc

    def owner(site, acc_id):
        return site_map.get(site, {}).get(str(acc_id))

    # removals: ключ - ID глобала, videos - ID ютуб-видео, записи с ними у этого игрока не учитываются.
    # shared_with игнорируем.
    removals = {}
    for r in fix.get("removals", []):
        removals.setdefault(str(r["user"]), []).extend(str(v) for v in r.get("videos", []) if v)

    counts = ", ".join(f"аккаунтов {n} {len(m)}" for n, m in site_map.items())
    print(f"links.json: записей {len(links)}, {counts}, "
          f"доп. аккаунтов Demonlist {len(alias) - len(links)}, склеек fix.json {len(absorb)}, "
          f"игроков с удалением видео {len(removals)}")
    if not links:
        print("  ВНИМАНИЕ: links.json пустой или не загрузился - склейки аккаунтов не будет!")
    elif not any(site_map.values()):
        print("  ВНИМАНИЕ: в links.json не нашлось ни одного аккаунта на других листах - "
              "проверь формат файла (ожидается блок other_lists).")
    return owner, canon, removals, accounts


def is_removed(removals, uid, video):
    """True, если видео записи входит в removals именно этого игрока."""
    vids = removals.get(str(uid))
    return bool(vids and video and any(v in str(video) for v in vids))


def merge_accounts(best_by, info, canon):
    """Склеивает все ID одного человека в один: лучший процент по каждому уровню."""
    new_best, new_info = {}, {}
    for uid, m in best_by.items():
        cu = canon(uid)
        nb = new_best.setdefault(cu, {})
        for k, v in m.items():
            nb[k] = max(nb.get(k, -1), v)
        n = info.get(uid)
        if not n:
            continue
        if cu not in new_info or uid == cu:
            new_info[cu] = n
        elif n[1] and not new_info[cu][1]:
            new_info[cu] = (new_info[cu][0], n[1])
    for uid in new_best:
        new_info.setdefault(uid, ("#" + uid, None))
    return new_best, new_info


def load_extras():
    merged = {}

    def put(uid, lvl, pct):
        m = merged.setdefault(str(uid), {})
        m[str(lvl)] = max(m.get(str(lvl), -1), float(pct))

    for src in SOURCES:
        name = FILE_SOURCES.get(src)
        if not name:
            continue
        try:
            users = get_json(RAW + name).get("users", {})
        except Exception as e:
            print(f"{name}: {e}")
            continue
        for uid, rows in users.items():
            for r in rows:
                k = r[0] if r[0] is not None else (r[7] if str(r[7]).startswith("x") else None)
                if k is not None:
                    put(uid, k, r[3])
    if "my_extras" in SOURCES:
        try:
            raw = get_json(RAW + "my_extras.json").get("users", {})
        except Exception as e:
            print(f"my_extras.json: {e}")
            raw = {}
        for uid, rows in raw.items():
            for r in rows:
                if isinstance(r, list) and len(r) >= 2:
                    put(uid, r[0], r[1])
    return merged


def normalize_level_name(name):
    return str(name or "").casefold()


def load_my_extras_always(owner_of, canon):
    """my_extras.json с GitHub ВСЕГДА, независимо от --src. Нужен для waiting: запись не считается новой,
    если у игрока уже есть такая же или лучшая в экстрасах. Возвращает {главный ID человека: {ID уровня: процент}}."""
    try:
        raw = get_json(RAW + "my_extras.json").get("users", {})
    except Exception as e:
        print(f"my_extras.json (для waiting): {e}")
        return {}
    out = {}
    for uid, rows in raw.items():
        src, rest = L.key_parts(uid)
        if src and owner_of(src.name, rest):
            uid = owner_of(src.name, rest)
        m = out.setdefault(canon(uid), {})
        for r in rows:
            if isinstance(r, list) and len(r) >= 2:
                m[str(r[0])] = max(m.get(str(r[0]), -1), float(r[1]))
    return out


def apply_cli_adds(best_by, info, canon):
    """Записи из --add ID_игрока:ID_уровня:процент. Применяются САМЫМИ ПОСЛЕДНИМИ,
    после склейки аккаунтов и после corrections.json, поэтому правки их не затрагивают.
    ID игрока приводится к главному через canon() (подойдёт ID любого его аккаунта)."""
    added = 0
    for item in args.add:
        uid, lvl, pct = item.split(":")
        cu = canon(uid)
        m = best_by.setdefault(cu, {})
        m[str(lvl)] = max(m.get(str(lvl), -1), float(pct))
        info.setdefault(cu, ("#" + cu, None))
        added += 1
    if added:
        print(f"--add: добавлено записей {added} (после corrections.json)")


def apply_corrections(best_by, info, canon):
    """Правки из corrections.json (применяются ПОСЛЕ склейки аккаунтов, всегда, при любом --src).
    Формат: {"delete": {"users": {ID: [[уровень, процент, ""], ...]}}, "add": {"users": {...}}}
    ID из файла приводятся к главному ID человека через canon(), так что подойдёт ID любого его аккаунта.
    delete - убрать запись, если у игрока по этому уровню лучший процент совпадает.
    add    - ИСПРАВЛЕНИЕ завышенной записи: добавляется, только если у этого игрока
             по этому же уровню запись реально была удалена через delete.
             Иначе (удалять было нечего) add пропускается, чтобы не появлялись лишние рекорды.
    Всё, что не сработало, печатается с причиной - так видно, где опечатка."""
    data = load_repo_json("corrections.json")
    if not data:
        print("corrections.json: пусто или не загрузился (см. строку с ошибкой выше) - правок НЕ будет")
        return

    def users_of(section):
        sec = data.get(section) or {}
        # допускаем и {"users": {...}}, и сразу {ID: [...]}
        return sec.get("users", sec) if isinstance(sec, dict) else {}

    deleted_keys = set()   # пары (главный ID игрока, ID уровня), которые реально удалены
    deleted = added = skipped = 0
    for uid, rows in users_of("delete").items():
        cu = canon(uid)
        mine = best_by.get(cu)
        for r in rows:
            if not isinstance(r, list) or len(r) < 2:
                continue
            k = str(r[0])
            if (cu, k) in deleted_keys:
                continue  # этот рекорд уже удалён через другой аккаунт того же человека
            if mine is None:
                print(f"  delete {uid} (-> {cu}): игрока нет в топе")
            elif k not in mine:
                print(f"  delete {uid} (-> {cu}): уровня {k} у него нет")
            elif abs(mine[k] - float(r[1])) > 1e-9:
                print(f"  delete {uid} (-> {cu}) уровень {k}: у него {mine[k]:g}%, а в файле {float(r[1]):g}% - не совпало")
            else:
                del mine[k]
                deleted_keys.add((cu, k))
                deleted += 1
    for uid, rows in users_of("add").items():
        cu = canon(uid)
        for r in rows:
            if not isinstance(r, list) or len(r) < 2:
                continue
            k = str(r[0])
            if (cu, k) not in deleted_keys:
                print(f"  add {uid} (-> {cu}) уровень {k}: пропущено - у него не удалялась запись по этому уровню")
                skipped += 1
                continue
            mine = best_by.setdefault(cu, {})
            mine[k] = max(mine.get(k, -1), float(r[1]))
            info.setdefault(cu, ("#" + cu, None))
            added += 1
    print(f"corrections.json: удалено записей {deleted}, добавлено {added}, add пропущено {skipped}")


# ---------- кеш ----------
CACHE_FILE = HERE / "levelcache.json"
cache = json.loads(CACHE_FILE.read_text("utf-8")) if CACHE_FILE.exists() else {}
lock = threading.Lock()
# Статусы записей глобала, которые считаются "ждут проверки". Все статусы, которые видит скрипт, печатаются в логе.
WAITING_STATUSES = {"waiting"}
waiting_raw = {}   # ID уровня -> [[ID аккаунта, ник (или None), процент, видео], ...] (записи waiting; заполняет load_waiting)
status_seen = {}   # статус -> сколько записей


def save_cache():
    """Запись через временный файл; под блокировкой, чтобы потоки не мешали друг другу."""
    with lock:
        data = json.dumps(cache)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(data, "utf-8")
        os.replace(tmp, CACHE_FILE)


def level_records(lvl_id):
    def page(offset):
        d = get_json(f"{DL}level/classic/record/list?level_id={lvl_id}&limit=50&offset={offset}")["data"]
        return d.get("records") or [], int(d.get("total_count") or 0)

    first, total = page(0)
    c = cache.get(str(lvl_id))
    # len(x) >= 5: в старом кеше не было ссылки на видео - такие уровни перекачиваем
    if (c and c["total"] == total and time.time() - c["at"] < args.ttl
            and all(len(x) >= 5 for x in c["recs"])):
        return c["recs"]
    records = list(first)
    while len(records) < total:
        more, _ = page(len(records))
        if not more:
            break
        records += more
    out = []
    for r in records:
        status = str(r.get("status") or "accepted").lower()
        u = r.get("user") or {}
        if status != "accepted":
            continue
        if u.get("id") is None:
            continue
        pct = r.get("percent")
        out.append([str(u["id"]), u.get("username"), u.get("country"),
                    float(pct if pct is not None else 100), r.get("video_url")])
    with lock:
        cache[str(lvl_id)] = {"total": total, "at": time.time(), "recs": out}
    return out


def user_waiting(uid):
    """Записи игрока в статусе waiting: GET user/record/list?user_id=<id>&limit=50&offset=...
    Возвращает [[ID уровня, процент, видео], ...]. Кешируется на --waiting-ttl секунд."""
    key = "w" + str(uid)
    c = cache.get(key)
    if c and time.time() - c["at"] < args.waiting_ttl:
        with lock:
            for st, n in (c.get("st") or {}).items():
                status_seen[st] = status_seen.get(st, 0) + n
        return c["wait"]
    wait, st_count, offset = [], {}, 0
    while True:
        try:
            d = get_json(f"{DL}user/record/list?user_id={uid}&limit=50&offset={offset}").get("data") or {}
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):  # такого игрока на глобале нет
                break
            raise
        recs = d.get("records") or []
        for r in recs:
            status = str(r.get("status") or "accepted").lower()
            st_count[status] = st_count.get(status, 0) + 1
            lvl = (r.get("level") or {}).get("id")
            if status in WAITING_STATUSES and lvl is not None:
                pct = r.get("percent")
                wait.append([str(lvl), float(pct if pct is not None else 100), r.get("video_url")])
        offset += len(recs)
        if not recs or offset >= int(d.get("total_count") or 0):
            break
    with lock:
        cache[key] = {"at": time.time(), "wait": wait, "st": st_count}
        for st, n in st_count.items():
            status_seen[st] = status_seen.get(st, 0) + n
    return wait


def load_waiting(result, accounts_of):
    """Качает waiting-записи по каждому аккаунту глобала у игроков рейтинга и кладёт их в waiting_raw."""
    uids = sorted({str(a) for r in result for a in accounts_of(r["id"])["dl"]})
    print(f"\nЗаписи waiting: запрашиваю аккаунтов глобала {len(uids)}")
    res = run_tasks([(u,) for u in uids], lambda it: user_waiting(it[0]), "игроков (waiting)")
    for (uid,), wait in res:
        for lvl, pct, video in wait or []:
            waiting_raw.setdefault(lvl, []).append([uid, None, pct, video])


def verifier_of(lvl):
    v = lvl.get("verification")
    if not v:
        key = "v" + str(lvl["id"])
        c = cache.get(key)
        if c and time.time() - c["at"] < 7 * 86400:
            v = c["v"]
        else:
            d = get_json(f"{DL}level/classic/get?id={lvl['id']}")["data"]
            v = d.get("verification")
            with lock:
                cache[key] = {"at": time.time(), "v": v}
    if v and v.get("user_id") is not None:
        return [str(v["user_id"]), v.get("username"), v.get("video_url")]
    return None


def clean_country(c):
    """'Unknown' и пустое значение на глобале означают 'страны нет'."""
    c = (c or "").strip()
    return None if c.lower() in ("", "unknown") else c


def dl_profile(uid):
    """Профиль игрока на глобале: GET user/get?id=<id> -> data.username, data.country.
    Возвращает (ник, страна) или None, если профиля нет. Кешируется на 30 дней."""
    key = "u" + str(uid)
    c = cache.get(key)
    if c and time.time() - c["at"] < 30 * 86400 and c.get("n"):
        return (c["n"], clean_country(c.get("c")))
    try:
        d = get_json(f"{DL}user/get?id={uid}").get("data") or {}
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):  # такого игрока на глобале нет
            return None
        raise
    name = d.get("username")
    if not name:
        return None
    country = d.get("country")
    with lock:
        cache[key] = {"at": time.time(), "n": name, "c": country}
    return (name, clean_country(country))


# ---------- Pointercrate ----------
def pc_demons():
    out, seen, after = [], set(), 0
    while True:
        arr = get_json(f"{PC}v2/demons/?limit=100&after={after}")
        if not isinstance(arr, list) or not arr:
            return out
        new = [d for d in arr if d["id"] not in seen]
        for d in new:
            seen.add(d["id"])
        out += new
        last = max(int(d["id"]) for d in arr)
        if not new or len(arr) < 100 or last <= after:
            return out
        after = last


def pc_records(demon_id):
    key = "pc" + str(demon_id)
    c = cache.get(key)
    if c and time.time() - c["at"] < args.ttl and all(len(x) >= 4 for x in c["recs"]):
        return c["recs"]
    out, after = [], 0
    while True:
        arr = get_json(f"{PC}v1/records/?demon_id={demon_id}&limit=100&after={after}")
        if not isinstance(arr, list) or not arr:
            break
        for r in arr:
            st = str(r.get("status") or "APPROVED").upper()
            pl = r.get("player") or {}
            if st == "APPROVED" and pl.get("id") is not None and r.get("progress") is not None:
                out.append([str(pl["id"]), pl.get("name"), float(r["progress"]), r.get("video")])
        last = max(int(r["id"]) for r in arr)
        if len(arr) < 100 or last <= after:
            break
        after = last
    with lock:
        cache[key] = {"at": time.time(), "recs": out}
    return out


pc_sem = threading.Semaphore(max(1, args.pc_conc))  # не заваливаем Pointercrate запросами
pc_style = ["/"]   # какой вариант URL работает: со слэшем в конце или без


def pc_fetch(url, tries=4):
    """GET к Pointercrate. Возвращает (HTTP-код, текст ответа).
    429 и 5xx повторяем (с учётом Retry-After); остальные коды возвращаем как есть.
    Исключение - только если сеть не отвечала все попытки."""
    last = None
    for i in range(tries):
        try:
            with pc_sem:
                req = urllib.request.Request(url, headers={"User-Agent": "gl-ext-top6",
                                                           "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:   # сюда попадаем уже ВНЕ семафора
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if e.code == 429 or e.code >= 500:
                last = (e.code, body)
                try:
                    wait = float(e.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    wait = 3 * (i + 1)
                time.sleep(min(wait, 60))
                continue
            return e.code, body
        except Exception as e:
            last = e
            time.sleep(min(2 ** i, 10))
    if isinstance(last, tuple):
        return last
    raise last


def pc_player_country(pid):
    """Код страны (ISO-2) игрока Pointercrate или None. Кешируется на 30 дней.
    Если запрос не удался - бросает исключение (и ничего не кеширует)."""
    pid = str(pid)
    key = "pcn3" + pid  # новый ключ: старые pcn/pcn2 могли хранить "страны нет" от кривого ответа
    c = cache.get(key)
    if c and not args.fresh_country and time.time() - c["at"] < 30 * 86400:
        return c["c"]

    variants = [f"{PC}v1/players/{pid}/", f"{PC}v1/players/{pid}"]
    if pc_style[0] == "":
        variants.reverse()
    status, body, errs = None, "", []
    for u in variants:
        try:
            st, bd = pc_fetch(u)
        except Exception as e:
            errs.append(f"{u} -> сеть: {str(e)[:60]}")
            status = None
            continue
        errs.append(f"{u} -> HTTP {st}")
        status, body = st, bd
        if st == 200:
            pc_style[0] = "/" if u.endswith("/") else ""
            break
    if status != 200:
        raise RuntimeError("; ".join(errs))

    try:
        d = json.loads(body)
    except ValueError:
        raise RuntimeError("ответ не JSON")
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        d = d["data"]
    if not isinstance(d, dict) or "id" not in d:
        return None  # структура не та - НЕ кешируем, чтобы не запомнить ложное "страны нет"
    nat = d.get("nationality")
    code = nat.get("country_code") if isinstance(nat, dict) else None
    with lock:
        cache[key] = {"at": time.time(), "c": code}
    return code


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600}ч {sec % 3600 // 60}м" if sec >= 3600 else f"{sec // 60}м {sec % 60}с"


def run_tasks(items, fn, label, workers=None):
    total = len(items)
    state = {"done": 0}
    errors = []
    failed = set()  # id() задач, которые не удались
    t0 = time.time()

    def item_name(item):
        return str(item[0] if isinstance(item, tuple) else item.get("name", item))

    def progress(final=False):
        d = state["done"]
        left = (total - d) / (d / max(time.time() - t0, 1)) if d else 0
        print(f"\r  {label} {d}/{total} ({d * 100 // max(total, 1)}%)  ошибок: {len(errors)}  "
              f"осталось ~{fmt_time(left)}   ", end="\n" if final else "", flush=True)

    def wrap(item):
        res = None
        try:
            res = fn(item)
        except Exception as e:
            with lock:
                errors.append((item_name(item), str(e)[:80]))
                failed.add(id(item))
        with lock:
            state["done"] += 1
            d = state["done"]
        if d % args.save == 0:
            save_cache()
        progress()
        return item, res

    ex = ThreadPoolExecutor(workers or args.conc)
    try:
        results = list(ex.map(wrap, items))
        ex.shutdown()
    except KeyboardInterrupt:
        print("\n\nОстановлено. Сохраняю кеш...")
        ex.shutdown(wait=False, cancel_futures=True)
        save_cache()
        raise SystemExit
    finally:
        save_cache()
    progress(final=True)

    # Автоповтор упавших задач (обрывы SSL, 5xx и т.п.): по одной, с паузой, до 3 кругов
    for rnd in range(1, 4):
        bad = [i for i, (it, _) in enumerate(results) if id(it) in failed]
        if not bad:
            break
        print(f"  Повтор {rnd}/3: неудачных {len(bad)}, пауза 5с...")
        time.sleep(5)
        errors.clear()
        for i in bad:
            it = results[i][0]
            try:
                results[i] = (it, fn(it))
                failed.discard(id(it))
            except Exception as e:
                errors.append((item_name(it), str(e)[:80]))
        save_cache()
        print(f"  После повтора {rnd}: осталось неудачных {len(failed)}")

    if errors:
        print(f"  Не скачались {len(errors)} (запусти ещё раз, докачает только их):")
        for name, msg in errors[:10]:
            print(f"    {name}: {msg}")
    return results


# ---------- вывод ----------
COUNTRY = {
    "unitedstates": "US", "usa": "US", "newzealand": "NZ", "russia": "RU", "armenia": "AM", "finland": "FI",
    "spain": "ES", "southkorea": "KR", "kazakhstan": "KZ", "brazil": "BR", "poland": "PL", "portugal": "PT",
    "moldova": "MD", "canada": "CA", "germany": "DE", "peru": "PE", "chile": "CL", "unitedkingdom": "GB",
    "australia": "AU", "japan": "JP", "romania": "RO", "norway": "NO", "denmark": "DK", "netherlands": "NL",
    "austria": "AT", "ukraine": "UA", "israel": "IL", "estonia": "EE", "uzbekistan": "UZ", "serbia": "RS",
    "france": "FR", "italy": "IT", "belgium": "BE", "china": "CN", "colombia": "CO", "argentina": "AR",
    "philippines": "PH", "panama": "PA", "albania": "AL", "czechrepublic": "CZ", "czechia": "CZ",
    "lithuania": "LT", "sweden": "SE", "hungary": "HU", "belarus": "BY", "mexico": "MX", "india": "IN",
    "venezuela": "VE", "pakistan": "PK", "greece": "GR", "ireland": "IE", "unitedarabemirates": "AE",
    "uae": "AE", "indonesia": "ID", "georgia": "GE", "nigeria": "NG", "paraguay": "PY", "iceland": "IS",
    "costarica": "CR", "slovakia": "SK", "uruguay": "UY", "turkey": "TR", "bosniaandherzegovina": "BA",
    "kosovo": "XK", "laos": "LA", "southafrica": "ZA", "latvia": "LV", "bulgaria": "BG", "ecuador": "EC",
    "vietnam": "VN", "cambodia": "KH", "iran": "IR", "dominicanrepublic": "DO", "malta": "MT",
    "palestine": "PS", "turkmenistan": "TM", "algeria": "DZ", "cuba": "CU", "egypt": "EG", "taiwan": "TW",
    "azerbaijan": "AZ", "tunisia": "TN", "kyrgyzstan": "KG", "croatia": "HR", "morocco": "MA",
    "bolivia": "BO", "switzerland": "CH", "hongkong": "HK", "slovenia": "SI", "iraq": "IQ", "thailand": "TH",
    "nicaragua": "NI", "jamaica": "JM", "luxembourg": "LU", "guatemala": "GT", "ghana": "GH",
    "puertorico": "PR", "mongolia": "MN", "montenegro": "ME", "elsalvador": "SV", "syria": "SY",
    "somalia": "SO", "greenland": "GL", "saudiarabia": "SA", "malaysia": "MY", "afghanistan": "AF",
    "northmacedonia": "MK", "macedonia": "MK", "bahrain": "BH", "maldives": "MV", "libya": "LY",
    "senegal": "SN", "capeverde": "CV", "cyprus": "CY", "honduras": "HN", "singapore": "SG", "yemen": "YE",
    "lebanon": "LB", "oman": "OM", "kenya": "KE", "kuwait": "KW", "jordan": "JO", "ethiopia": "ET",
    "myanmar": "MM", "bangladesh": "BD", "qatar": "QA", "tajikistan": "TJ", "bahamas": "BS", "mali": "ML",
    "togo": "TG", "zambia": "ZM", "gabon": "GA", "andorra": "AD", "guam": "GU", "haiti": "HT",
    "england": "GB", "scotland": "GB", "wales": "GB", "canaryislands": "ES",
}


def flag(country):
    """Принимает либо название страны (Demonlist), либо готовый ISO-код из 2 букв (Pointercrate)."""
    raw = str(country or "").strip()
    if len(raw) == 2 and raw.isalpha():
        code = raw.upper()
    else:
        code = COUNTRY.get("".join(ch for ch in raw.lower() if ch.isalpha()))
    if not code or len(code) != 2:
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code)


def line_short(i, r, with_id=False):
    """5) 🇵🇱 dovvin (ORBIT, Thinking Space II 88%, ...) - 14.18 [id 12345]"""
    parts = []
    for rec in r["records"]:
        if rec["place"] is None:
            continue
        pct = "" if rec["percent"] >= 100 else f" {rec['percent']:g}%"
        parts.append(rec["level"] + pct)
    tail = f" [{r['id']}]" if with_id else ""
    return f"{i}) {flag(r['country'])} {r['name']} ({', '.join(parts)}) - {r['score']:.2f}{tail}"


def fmt_record(r):
    if r["place"] is None:
        return f"       {'—':<34} {'':>7}  {r['f']:8.2f}   (добор до {N} записей)"
    pct = f"{r['percent']:g}%"
    tag = " [x]" if r.get("special") else ""
    return f"    #{int(r['place']):<4} {(r['level'] + tag):<34} {pct:>7}  {r['f']:8.2f}"


def save_waiting(meta, best_by, info, result, canon_id, accounts_of, removals, owner_of):
    """waiting_records.json: для каждого человека с аккаунтом глобала (аккаунты из links.json и fix.json считаются его)
    все непринятые записи (статус waiting), которые, будь они приняты, попали бы в его топ-N лучших записей.
    Запись считается подходящей, если она лучше принятой у человека на этом уровне (или принятой нет) и уровень остаётся
    в его топ-N после добавления. Записи группируются по аккаунту глобала, с которого они поданы.
    Сравнение идёт со ВСЕМИ записями человека: глобал, Pointercrate, другие листы и my_extras (его подгружаем всегда).
    Два файла в формате missing_achievements.json / missing_achievements_with_nicks.json ({"users": {...}}).
    В них только записи БОЛЬШЕ всех, что у человека уже есть (глобал, Pointercrate, другие листы, my_extras):
    записи, которые равны или меньше имеющихся, ничего не добавляют и в файлы не попадают."""
    print("\nСтатусы записей глобала: " + (", ".join(f"{s} {n}" for s, n in sorted(status_seen.items())) or "нет данных"))
    if not any(s in status_seen for s in WAITING_STATUSES):
        print(f"  ВНИМАНИЕ: записей со статусом {sorted(WAITING_STATUSES)} не найдено. Проверь названия статусов выше "
              f"и константу WAITING_STATUSES.")

    def top_of(best):
        recs = []
        for k, pct in best.items():
            lv = meta.get(k)
            if not lv:
                continue
            f = score(lv["place"], pct, lv["T"], lv["t"])
            if f is not None and f <= args.cap:
                recs.append((f, k.startswith("x"), k, pct))
        recs.sort(key=lambda r: (r[0], r[1]))
        return recs[:N]

    def person_score(best):
        """Очки человека по набору его лучших записей: так же, как в result (топ-N, недостающие добираются cap)."""
        fs = [r[0] for r in top_of(best)]
        fs += [args.cap] * (N - len(fs))
        return geo_mean(fs)

    by_person = {}  # главный ID человека -> {ID аккаунта глобала: [(ID уровня, ник, процент, видео)]}
    for lvl_id, recs in waiting_raw.items():
        if lvl_id not in meta:
            continue
        for uid, name, pct, video in recs:
            if is_removed(removals, uid, video):
                continue
            by_person.setdefault(canon_id(uid), {}).setdefault(uid, []).append((lvl_id, name, pct, video))

    extras_all = load_my_extras_always(owner_of, canon_id)
    ranks = {r["id"]: r["rank"] for r in result}

    def fmt_pct(x):
        return int(x) if x == int(x) else x

    def pick_best(items):
        """Записи одного человека (с разных его аккаунтов) -> по одной на уровень: больший процент, при равном - с видео."""
        best = {}
        for lvl_id, pct, video in items:
            link = video if isinstance(video, str) and video.startswith("http") else ""
            cur = best.get(lvl_id)
            if cur is None or (pct, bool(link)) > (cur[0], bool(cur[1])):
                best[lvl_id] = (pct, link)
        return best

    # person -> {"nick", "rank", "new": [(уровень, процент, видео)]}
    people = {}       # у них очков меньше порога и БЕЗ waiting -> обычные waiting_records*.json
    only_waiting = {}  # у них меньше порога только С waiting (без него >= порога) -> waiting_new_players.json
    skipped_high = 0   # даже с waiting очков не меньше порога: никуда не попадают
    for person, accs in by_person.items():
        base = dict(best_by.get(person, {}))
        for k, pct in extras_all.get(person, {}).items():  # лучшее из всего, включая my_extras
            base[k] = max(base.get(k, -1), pct)
        new = []
        for acc, items in accs.items():
            for lvl_id, name, pct, video in items:
                best = base.get(lvl_id)
                if best is not None and pct <= best:  # равна или меньше того, что уже есть: ничего не добавляет
                    continue
                top = top_of({**base, lvl_id: pct})
                if any(t[2] == lvl_id and t[3] == pct for t in top):  # попадает в топ-N
                    new.append((lvl_id, pct, video))
        if not new:
            continue
        # Очки, которые получатся, если принять ВСЕ waiting-записи сразу (лучшая запись на уровень).
        final_best = dict(base)
        for lvl_id, (pct, _link) in pick_best(new).items():
            final_best[lvl_id] = max(final_best.get(lvl_id, -1), pct)
        final_score = person_score(final_best)
        if final_score >= args.waiting_max_score:
            skipped_high += 1
            continue
        nick = (info.get(person) or (None, None))[0] or next(
            (n for items in accs.values() for _, n, _, _ in items if n), person)
        entry = {"nick": nick, "rank": ranks.get(person), "new": new,
                 "final_best": final_best, "final_score": final_score}
        if person_score(base) < args.waiting_max_score:
            people[person] = entry
        else:
            only_waiting[person] = entry

    order = sorted(people, key=lambda u: (people[u]["rank"] is None, people[u]["rank"] or 0, u))

    def build(with_nicks):
        """Как в missing_achievements(.json / _with_nicks.json): ID (или 'ID — ник') -> [[ID уровня, процент, X]],
        где X - ссылка на видео, а в файле с никами - название уровня. Записи внутри игрока по названию уровня."""
        out = {}
        for u in order:
            best = pick_best(people[u]["new"])
            if not best:
                continue
            rows = sorted(best.items(), key=lambda kv: normalize_level_name(meta[kv[0]]["name"]))
            key = f"{u} — {people[u]['nick']}" if with_nicks else u
            out[key] = [[lvl, fmt_pct(pct), meta[lvl]["name"] if with_nicks else link] for lvl, (pct, link) in rows]
        return out

    def write(path, users):
        lines = ['{', '  "users": {']
        items = list(users.items())
        for i, (key, rows) in enumerate(items):
            lines.append(f'    {json.dumps(key, ensure_ascii=False)}: [')
            for j, r in enumerate(rows):
                lines.append(f'      {json.dumps(r, ensure_ascii=False)}' + ("," if j < len(rows) - 1 else ""))
            lines.append("    ]" + ("," if i < len(items) - 1 else ""))
        lines += ['  }', '}', '']
        path.write_text("\n".join(lines), "utf-8")

    for fname, with_nicks in (("waiting_records.json", False), ("waiting_records_with_nicks.json", True)):
        write(HERE / fname, build(with_nicks))
    n_rec = sum(len(r) for r in build(False).values())
    print(f"waiting_records.json и waiting_records_with_nicks.json: игроков {len(people)}, новых записей {n_rec} "
          f"(топ-{N}, больше всех имеющихся; очков с waiting меньше {args.waiting_max_score:g})")

    # waiting_new_players.json: игроки, которые набирают меньше порога ТОЛЬКО с waiting-записями.
    # Формат как у missing_players.json, но ключ пока наш ID: ID CoreBoard и уникальные ники выдаёт cbd-update.py
    # (там известен последний ID из missing_players.json). Записи: итоговый топ-N игрока, сначала самые сложные;
    # ссылка есть у waiting-записей (видео из заявки), у уже принятых записей её нет.
    new_players = {}
    for person in sorted(only_waiting, key=lambda u: (only_waiting[u]["final_score"], u)):
        e = only_waiting[person]
        links = pick_best(e["new"])  # уровень -> (процент, ссылка)
        records = []
        for _f, _special, k, pct in top_of(e["final_best"]):
            w = links.get(k)
            records.append([meta[k]["name"], fmt_pct(pct), w[1] if w and w[0] == pct else ""])
        new_players[person] = {"nick": e["nick"], "country_code": (info.get(person) or (None, None))[1] or None,
                               "gdr_id": person, "records": records}
    lines = ['{', '  "users": {']
    items = list(new_players.items())
    for i, (key, val) in enumerate(items):
        lines.append(f"    {json.dumps(key, ensure_ascii=False)}: {json.dumps(val, ensure_ascii=False)}"
                     + ("," if i < len(items) - 1 else ""))
    lines += ['  }', '}', '']
    (HERE / "waiting_new_players.json").write_text("\n".join(lines), "utf-8")
    print(f"waiting_new_players.json: игроков {len(new_players)} (меньше {args.waiting_max_score:g} только с waiting). "
          f"Не прошли порог даже с waiting: {skipped_high}")


def main():
    print("Источники: global" + "".join(" + " + s for s in sorted(SOURCES)))
    if args.add:
        print("Свои записи:", ", ".join(args.add))

    print("Загружаю список уровней...")
    owner_of, canon_id, removals, accounts_of = load_links()
    # качаем только до --cap-place (по умолчанию топ-300), остальные уровни нигде не используются
    levels = load_paged("level/classic/list", "levels", 100, stop=levels_past_cap)
    extras = load_extras()
    tp = load_tp()

    meta, fallback = {}, 0
    for l in levels:
        k = str(l["id"])
        e = tp.get(k) or {}
        if e.get("T") and e.get("t"):
            T, t = float(e["T"]), float(e["t"])
        else:
            T, t = 100.0, float(l["list_percent"])
            fallback += 1
        # Особое название (если есть) берётся вместо названия из API
        meta[k] = {"name": SPECIAL_LEVEL_NAMES.get(k, l["name"]),
                   "place": float(l["placement"]), "T": T, "t": t}
    for k, e in tp.items():
        if k.startswith("x") and e.get("T") and e.get("t"):
            meta[k] = {"name": e["name"], "place": float(e["place"]), "T": float(e["T"]), "t": float(e["t"])}
    if fallback:
        print(f"Уровней без T/t в levels_tp.json (запасные T=100, list_percent): {fallback}")

    top_levels = sorted((l for l in levels if float(l["placement"]) <= args.cap_place),
                        key=lambda l: float(l["placement"]))
    if args.levels:
        top_levels = top_levels[: args.levels]
    print(f"Уровней для проверки: {len(top_levels)}\n")

    def fetch_dl(l):
        recs = level_records(l["id"])
        ver = verifier_of(l)
        if ver:
            recs = [r for r in recs if r[0] != ver[0]] + [[ver[0], ver[1], None, 100.0, ver[2]]]
        return recs

    print("Demonlist:")
    fetched = run_tasks(top_levels, fetch_dl, "уровней")

    best_by, info, pc_only = {}, {}, set()
    dl_pairs, pc_pairs = {}, {}  # (ID игрока, ID уровня) -> лучший процент, отдельно по каждому сайту
    removed_n = 0
    dl_seen = set()  # игроки, чей ник и страна уже пришли из записей глобала
    for l, recs in fetched:
        k = str(l["id"])
        for uid, name, country, pct, video in recs or []:
            country = clean_country(country)
            if is_removed(removals, uid, video):
                removed_n += 1
                continue
            m = best_by.setdefault(uid, {})
            m[k] = max(m.get(k, -1), pct)
            dl_pairs[(uid, k)] = max(dl_pairs.get((uid, k), -1), pct)
            if country:
                dl_seen.add(uid)
            if uid not in info or country:
                info[uid] = (name, country)

    if "pointercrate" in SOURCES:
        print("\nPointercrate:")
        by_ingame = {str(l["ingame_id"]): l for l in top_levels}
        try:
            demons = pc_demons()
        except Exception as e:
            print("Не удалось загрузить демонов Pointercrate:", e)
            demons = []
        targets = [(d, by_ingame[str(d["level_id"])]) for d in demons
                   if d.get("level_id") is not None and str(d["level_id"]) in by_ingame]
        print(f"Демонов Pointercrate, найденных в глобале (по игровому ID): {len(targets)}")
        pc_results = run_tasks([(f"{d['name']}", d, l) for d, l in targets],
                               lambda it: pc_records(it[1]["id"]), "демонов")
        for (_, d, l), recs in pc_results:
            k = str(l["id"])
            for pid, pname, pct, video in recs or []:
                main_id = owner_of("pointercrate", pid)
                if main_id and is_removed(removals, main_id, video):
                    removed_n += 1
                    continue
                uid = main_id or "pc" + pid  # человек из links.json (цифры, pc..., wml...) или игрок без линка
                if is_global(uid):
                    info.setdefault(uid, (pname, None))
                else:
                    pc_only.add(uid)  # человека нет в глобале: метку [PC] добавим при выводе
                    info.setdefault(uid, (pname or "?", None))
                m = best_by.setdefault(uid, {})
                m[k] = max(m.get(k, -1), pct)
                pc_pairs[(uid, k)] = max(pc_pairs.get((uid, k), -1), pct)
        print(f"Игроков Pointercrate без линка: {len(pc_only)}")

        # Страны игроков, которых нет в глобале, ищет find_country ниже (по их аккаунтам Pointercrate и WML)

    # Другие листы из реестра (wml и т.д.): данные качает адаптер листа, уровни ищет levels.py
    list_pairs = {}      # лист -> {(ID человека, ключ уровня): лучший процент}
    list_countries = {}  # лист -> {ID аккаунта: страна}
    list_players = {}    # лист -> сколько игроков в данных
    wanted_lists = [src for src in L.sources() if src.name in SOURCES]
    if wanted_lists:
        index = levelsearch.LevelIndex.build(tp=tp, levels=levels)  # названия уровней -> ключи уровней
        by_ingame = {str(l["ingame_id"]): str(l["id"]) for l in levels if l.get("ingame_id") is not None}
    for src in wanted_lists:
        print(f"\n{src.name}:")
        src.ttl = args.list_ttl
        data = src.fetch(L.DictStore(cache, lock, save_cache), workers=args.list_conc)
        if not data:
            print(f"Лист {src.name} недоступен, источник в этом запуске пропускаю")
            continue
        pairs = list_pairs.setdefault(src.name, {})
        countries = list_countries.setdefault(src.name, {})
        list_players[src.name] = len(data["players"])
        linked_n = free_n = n_recs = 0
        for aid, pl in data["players"].items():
            main_id = owner_of(src.name, aid)
            uid = main_id or src.prefix + aid  # человек из links.json или игрок листа без линка
            nick = src.display_nick(pl["n"]) or pl["n"]
            countries[aid] = pl.get("c")
            if main_id:
                linked_n += 1
                info.setdefault(uid, (nick, None))
            else:
                free_n += 1  # метку [тег] добавим при выводе
                info[uid] = (nick, pl.get("c"))
        unresolved, found = {}, {}
        for r in data["records"]:
            aid = str(r["a"])
            main_id = owner_of(src.name, aid)
            uid = main_id or src.prefix + aid
            name = r["l"]
            if name not in found:  # поиск по названию делаем один раз на название
                found[name] = by_ingame.get(str(r["i"])) if r.get("i") is not None else None
                found[name] = found[name] or index.find(name)
            k = found[name]
            if k is None:
                unresolved[name] = unresolved.get(name, 0) + 1
                continue
            if main_id and is_removed(removals, main_id, r.get("v")):
                removed_n += 1
                continue
            pct = float(r["p"])
            m = best_by.setdefault(uid, {})
            m[k] = max(m.get(k, -1), pct)
            pairs[(uid, k)] = max(pairs.get((uid, k), -1), pct)
            n_recs += 1
        print(f"Игроков {src.name} с линком в links.json: {linked_n}, без линка [{src.tag}]: {free_n}; записей: {n_recs}")
        if unresolved:
            top = sorted(unresolved.items(), key=lambda kv: -kv[1])[:10]
            print(f"  уровней {src.name} без пары в списке (всего названий {len(unresolved)}), чаще всего: "
                  + ", ".join(f"{n} ({c})" for n, c in top))
        if index.stats:
            print("  названия найдены: " + ", ".join(f"{k} {v}" for k, v in index.stats.items()))

    for uid, m in extras.items():
        # my_extras мог записать игрока как "pc<ID Pointercrate>", "wml<ID WML>" и т.п., когда линка ещё не было.
        # Если линк в links.json с тех пор появился, переводим на его главный ID.
        src, rest = L.key_parts(uid)  # pc<id>, wml<id> и т.п.: ищем человека в links.json
        if src and owner_of(src.name, rest):
            uid = owner_of(src.name, rest)
        b = best_by.setdefault(uid, {})
        for k, pct in m.items():
            b[k] = max(b.get(k, -1), pct)
        info.setdefault(uid, ("#" + uid, None))

    print(f"Убрано записей по removals (fix.json): {removed_n}")

    # Склеиваем аккаунты одного человека (links.json + fix.json) после сбора всех источников
    before = len(best_by)
    best_by, info = merge_accounts(best_by, info, canon_id)
    print(f"Склейка аккаунтов: {before} -> {len(best_by)}")

    # Рекорды (игрок + уровень), которые есть только на Pointercrate. Считаем после склейки
    # аккаунтов: если у человека есть этот уровень на глобале (хоть с другого аккаунта),
    # это уже не "только Pointercrate".
    def canon_pairs(d):
        out = {}
        for (u, k), pct in d.items():
            cu = canon_id(u)
            out[(cu, k)] = max(out.get((cu, k), -1), pct)
        return out

    dlp, pcp = canon_pairs(dl_pairs), canon_pairs(pc_pairs)
    # "только на листе" = рекорд (игрок + уровень) есть на этом листе, но нет на глобале и на других подключённых листах
    all_pairs = {"pointercrate": pcp, **{n: canon_pairs(pr) for n, pr in list_pairs.items()}}
    only_sets = {}
    for lname, pr in all_pairs.items():
        others = [dlp] + [o for n, o in all_pairs.items() if n != lname]
        only_sets[lname] = {key for key in pr if not any(key in o for o in others)}
    only_pc_set = only_sets["pointercrate"]
    if "pointercrate" in SOURCES:
        no_link = sum(1 for (u, _) in only_pc_set if u.startswith("pc"))
        better = sum(1 for key, pct in pcp.items() if key in dlp and pct > dlp[key])
        print(f"Рекордов только на Pointercrate (игрок + уровень, по всем игрокам): {len(only_pc_set)}")
        print(f"  у игроков без линка [PC]: {no_link}, у игроков, связанных с глобалом: {len(only_pc_set) - no_link}")
        print(f"  уровень есть на обоих сайтах, но процент на Pointercrate выше: {better}")

    # Правки из corrections.json: применяются всегда, при любом источнике
    apply_corrections(best_by, info, canon_id)

    # Свои записи из --add: самым последним шагом, после corrections.json
    apply_cli_adds(best_by, info, canon_id)

    # Вся информация об игроке (ник, страна) - с глобала, если он там есть.
    # Из Pointercrate остаются только те, кого на глобале нет (pc-only).
    need = [u for u in best_by if is_global(u) and u not in dl_seen]
    if need:
        print("Профили игроков на глобале:")
        res = run_tasks([(u,) for u in need], lambda it: dl_profile(it[0]), "профилей")
        got = 0
        for (u,), prof in res:
            if prof:
                info[u] = prof
                got += 1
        print(f"Профиль найден у {got} из {len(need)}")
        if need and got == 0:
            print("  ВНИМАНИЕ: ни один профиль не загрузился. Ники и флаги этих игроков "
                  "могут быть из Pointercrate или пустыми.")

    # Если у главного аккаунта страны нет - ищем её у остальных аккаунтов этого человека:
    # сначала по всем аккаунтам Demonlist (главный первым), затем по аккаунтам Pointercrate.
    # Белый флаг остаётся только если страны нет нигде.
    country_errors = []

    def find_country(uid):
        acc = accounts_of(uid)
        for d in acc["dl"]:
            if not is_global(d):
                continue
            try:
                prof = dl_profile(d)
            except Exception as e:
                country_errors.append(f"DL {d} (игрок {uid}): {str(e)[:80]}")
                continue
            if prof and clean_country(prof[1]):
                return prof[1]
        for pid in acc["pointercrate"]:
            try:
                code = pc_player_country(pid)
            except Exception as e:
                country_errors.append(f"PC {pid} (игрок {uid}): {str(e)[:80]}")
                continue
            if code:
                return code
        for lname, countries in list_countries.items():  # страна из данных других листов (уже скачаны выше)
            for aid in acc.get(lname, []):
                if countries.get(str(aid)):
                    return countries[str(aid)]
        return None

    no_flag = [u for u in best_by if flag((info.get(u) or ("", None))[1]) == "🏳️"]
    if no_flag:
        print("Поиск страны по другим аккаунтам игроков без флага:")
        res = run_tasks([(u,) for u in no_flag], lambda it: find_country(it[0]), "игроков (страна)")
        got = 0
        for (u,), code in res:
            if code:
                info[u] = (info[u][0], code)
                got += 1
        print(f"Страна найдена у {got} из {len(no_flag)}")
        still = [u for u in no_flag if not info[u][1]]
        if still:
            print(f"  Без страны остались {len(still)}")
        if country_errors:
            print(f"  Ошибок запросов при поиске страны: {len(country_errors)} (первые 5):")
            for msg in country_errors[:5]:
                print(f"    {msg}")

    result = []
    for uid, best in best_by.items():
        recs = []
        for k, pct in best.items():
            lv = meta.get(k)
            if not lv:
                continue
            f = score(lv["place"], pct, lv["T"], lv["t"])
            if f is not None and f <= args.cap:
                recs.append({"level": lv["name"], "place": lv["place"], "percent": pct, "f": f,
                             "special": k.startswith("x"), "key": k})
        if not recs:
            continue
        recs.sort(key=lambda r: (r["f"], r["special"]))
        top = recs[:N]
        while len(top) < N:
            top.append({"level": "-", "place": None, "percent": None, "f": args.cap, "special": False})
        name, country = info[uid]
        name = f"{name}{L.tag_of(uid)}"  # человека нет в глобале: помечаем, откуда он ([PC], [WML], ...)
        result.append({"id": uid, "name": name, "country": country, "pc_only": uid in pc_only,
                       "score": geo_mean([r["f"] for r in top]), "records": top})

    result.sort(key=lambda r: r["score"])
    for i, r in enumerate(result, 1):
        r["rank"] = i  # место во всём рейтинге; фильтр по очкам его не меняет

    if args.waiting:
        load_waiting(result, accounts_of)
        save_waiting(meta, best_by, info, result, canon_id, accounts_of, removals, owner_of)

    # Фильтр по очкам (меньше = лучше): оставляем игроков с очками от --min-score до --max-score
    suffix = ""
    if args.min_score is not None or args.max_score is not None:
        lo = args.min_score if args.min_score is not None else float("-inf")
        hi = args.max_score if args.max_score is not None else float("inf")
        total_players = len(result)
        result = [r for r in result if lo <= r["score"] <= hi]
        lo_txt = f"{args.min_score:g}" if args.min_score is not None else "0"
        hi_txt = f"{args.max_score:g}" if args.max_score is not None else "max"
        suffix = f"_score{lo_txt}-{hi_txt}"
        print(f"\nФильтр по очкам {lo_txt}-{hi_txt}: осталось игроков {len(result)} из {total_players}")

    # Сколько записей, которые есть только на Pointercrate, у оставшихся игроков
    for r in result:
        r["pc_only_in_top"] = sum(1 for rec in r["records"]
                                  if rec.get("key") and (r["id"], rec["key"]) in only_pc_set)
        r["only_in_top"] = {lname: sum(1 for rec in r["records"] if rec.get("key") and (r["id"], rec["key"]) in only)
                            for lname, only in only_sets.items() if lname in list_pairs}

    print(f"\nТоп по {N} лучшим записям (меньше = лучше), игроков: {len(result)}\n")
    if "pointercrate" in SOURCES:
        ids = {r["id"] for r in result}
        total_pc = sum(1 for (u, _) in only_pc_set if u in ids)
        in_top = sum(r["pc_only_in_top"] for r in result)
        with_pc = sum(1 for r in result if r["pc_only_in_top"])
        # все записи этих игроков (игрок + уровень, уровни до --cap-place, после склейки и правок)
        total_all = sum(len(best_by.get(r["id"], {})) for r in result)
        share_all = f" ({total_pc * 100 / total_all:.1f}%)" if total_all else ""
        # записи, которые реально вошли в топ-N игроков (без пустых добивок до N)
        top_all = sum(1 for r in result for rec in r["records"] if rec.get("place") is not None)
        share_top = f" ({in_top * 100 / top_all:.1f}%)" if top_all else ""
        print(f"Записей в {N} лучших у этих игроков: {top_all}")
        print(f"  из них только на Pointercrate: {in_top}{share_top}, "
              f"игроков с такими записями: {with_pc} из {len(result)}")
        print(f"Для сравнения, вообще всех записей у этих игроков: {total_all}, "
              f"из них только на Pointercrate: {total_pc}{share_all}\n")

    for lname in list_pairs:  # другие листы: сколько записей есть только на них
        only = only_sets[lname]
        ids = {r["id"] for r in result}
        total_l = sum(1 for (u, _) in only if u in ids)
        in_top_l = sum(r["only_in_top"][lname] for r in result)
        with_l = sum(1 for r in result if r["only_in_top"][lname])
        top_all_l = sum(1 for r in result for rec in r["records"] if rec.get("place") is not None)
        share_l = f" ({in_top_l * 100 / top_all_l:.1f}%)" if top_all_l else ""
        print(f"Записей, которые есть только на {lname} (нет на глобале и на других подключённых листах): "
              f"всего у этих игроков {total_l}, в {N} лучших {in_top_l}{share_l}, игроков с такими записями: {with_l}\n")

    # в консоль: первые --show игроков
    for r in result[: args.show]:
        i = r["rank"]
        if args.long:
            print(f"{i:3}. {str(r['name']):<28} {r['score']:8.2f}  {r['country'] or ''}")
            for rec in r["records"]:
                print(fmt_record(rec))
            print()
        else:
            print(line_short(i, r, args.ids))

    # в txt: весь топ целиком (или весь отфильтрованный)
    # Имя файла по набору источников: gdr_gl (только глобал), gdr_gl_pc, gdr_gl_wml, gdr_gl_my,
    # их сочетания (gdr_gl_pc_wml и т.п.), а когда подключено всё, просто gdr
    if SOURCES == ALL_SOURCES:
        base = "gdr"
    else:
        order = sorted(SOURCES, key=lambda x: (SOURCE_ORDER.index(x) if x in SOURCE_ORDER else 99, x))
        base = "gdr_gl" + "".join("_" + SOURCE_SHORT.get(x, x) for x in order)
    txt = HERE / f"{base}{suffix}.txt"
    txt.write_text("\n".join(line_short(r["rank"], r, args.ids) for r in result), "utf-8")
    print(f"\nТекст для копирования (весь топ, {len(result)} игроков): {txt.name}")

    out = HERE / f"{base}{suffix}.json"
    out.write_text(json.dumps(
        [{**r, "score": round(r["score"], 2)} for r in result],
        ensure_ascii=False, indent=1), "utf-8")
    print(f"Полный результат: {out.name}")


main()
