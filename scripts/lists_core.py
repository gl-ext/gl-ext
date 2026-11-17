"""Реестр листов-источников (Pointercrate, WML и любые будущие) и общие правила для всех скриптов.

КАК ДОБАВИТЬ НОВЫЙ ЛИСТ (например, XYZ):
  1. Положи рядом файл list_xyz.py (образец: list_wml.py). В нём класс на основе PageSource:
     задай name/prefix/tag/short и опиши два метода: top() и player(id). Внизу register(XYZ()).
  2. Всё. Файл подхватится сам: make-links.py начнёт склеивать аккаунты XYZ с людьми по общим видео
     и создавать записи xyz<id> для игроков без пары, gd-rating.py примет --src xyz, ключи, метки [XYZ] и имена файлов
     появятся сами. Менять сами скрипты не нужно.
Лист, у которого нет страниц игроков (данные идут по уровням, как у Pointercrate), описывается своим классом
на основе Source с методом fetch(): он возвращает те же данные, что PageSource (см. fetch ниже).

ВЫКЛЮЧАТЕЛИ ЛИСТА (чтобы выкатить новый лист и не сломать автоматику):
  enabled = False в классе листа      - лист выключен везде, пока не поставишь True;
  переменная окружения GL_LISTS_OFF   - выключить листы без правки кода: GL_LISTS_OFF=wml (или wml,xyz).
  Выключенный лист не качается и не принимает --src, но его ключи (wml5) и записи в links.json по-прежнему
  распознаются, поэтому уже собранные данные не портятся.

ПРАВИЛА КЛЮЧЕЙ ЗАПИСЕЙ links.json:
  начинается с цифры     - аккаунт глобала (is_global);
  pc7, wml5, xyz9 ...    - человек, которого нет в глобале; префикс (pc, wml, xyz) задаёт лист.
В блоке other_lists записи лежат аккаунты человека на каждом листе: {"pointercrate": [...], "wml": [...], ...}.
"""
import glob
import importlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # чтобы находились list_*.py и их зависимости (wml.py)


# ---------- описание листа ----------
class Source:
    name = None        # название листа: ключ в other_lists, значение для --src и --no-<name>
    prefix = None      # префикс ключа записи без глобала: wml -> wml5. Один префикс не должен начинаться с другого
    tag = None         # метка в рейтинге у игроков без глобала: [WML]
    short = None       # короткое имя для файлов: gdr_gl_wml
    fetchable = False  # умеет ли сам скачивать данные (у Pointercrate скрипты качают по-своему)
    enabled = True     # False = лист выключен (см. ВЫКЛЮЧАТЕЛИ ЛИСТА в начале файла)

    def display_nick(self, nick):
        """Ник для показа (без клана и т.п.). По умолчанию как есть."""
        return nick

    def fetch(self, store, workers=3, log=print):
        """Возвращает None (лист недоступен) или данные в едином виде:
        {"players": {id: {"n": ник, "r": место, "c": страна}},
         "records": [{"a": id игрока, "l": название уровня, "p": процент, "v": видео, "i": игровой ID уровня или None,
                      "pos": место уровня в листе или None (необязательно)}]}
        Названия уровней в ключи переводит тот, кто использует данные (levels.py), а не лист."""
        raise NotImplementedError


class PageSource(Source):
    """Лист с топом игроков и страницей каждого игрока (WML и похожие). Нужно описать top() и player()."""
    fetchable = True
    pause = 0.4        # пауза после каждого запроса, секунд
    ttl = 7 * 86400    # через сколько страницу игрока качать заново, даже если версия не менялась
    max_failed = 20    # сколько игроков можно не скачать и всё равно продолжить
    save_every = 25
    rev = 1            # ревизия разбора страницы игрока: поправил парсер, увеличь число, и кеш листа перекачается сам

    def top(self):
        """-> [{"id": строка, "nick": ник, "rank": место или None, "version": что угодно или None}].
        version - то, что меняется, когда у игрока меняются рекорды (например, его очки). Пока она та же,
        страницу игрока заново не качаем. None - качаем по ttl. Исключение = лист недоступен."""
        raise NotImplementedError

    def player(self, pid):
        """-> {"country": код страны или None, "records": [{"level": название, "percent": число, "video": ссылка или None, "pos": место уровня или None}]}.
        Исключение = сбой (повторим позже, пока возьмём старый кеш игрока)."""
        raise NotImplementedError

    def _key(self, pid):
        return f"{self.name}:{pid}"

    def fetch(self, store, workers=3, log=print):
        try:
            top = self.top()
        except Exception as e:
            log(f"  {self.name}: топ не скачался: {e}")
            return None
        if not top:
            log(f"  {self.name}: топ пустой, лист пропускаю")
            return None

        now = time.time()
        entries, todo = {}, []
        for p in top:
            c = store.get(self._key(p["id"]))
            if (c and c.get("recs") is not None and p.get("version") is not None
                    and c.get("rev", 1) == self.rev
                    and c.get("ver") == p["version"] and now - c.get("at", 0) < self.ttl):
                entries[p["id"]] = c
            else:
                todo.append(p)
        log(f"  {self.name}: игроков {len(top)}, из кеша {len(entries)}, скачать {len(todo)}")
        self.downloaded = 0

        def job(p):
            key = self._key(p["id"])
            try:
                data = self.player(p["id"])
                entry = {"at": time.time(), "ver": p.get("version"), "rev": self.rev, "c": data.get("country"),
                         "recs": [[r["level"], r["percent"], r.get("video"), r.get("pos")] for r in data["records"]]}
                store.put(key, entry)
                self.downloaded += 1
                return p, entry
            except Exception:
                old = store.get(key)  # сайт не ответил: берём старый кеш игрока, даже просроченный
                # (но не разобранный старым парсером: в нём могут быть неверные записи)
                return p, (old if old and old.get("recs") is not None and old.get("rev", 1) == self.rev else None)
            finally:
                time.sleep(self.pause)

        for round_no in range(2):
            if not todo:
                break
            if round_no:
                log(f"  {self.name}: не скачано {len(todo)}, пауза 10 с и повтор")
                time.sleep(10)
            ex = ThreadPoolExecutor(max(1, workers))
            failed, done = [], 0
            try:
                for fut in as_completed([ex.submit(job, p) for p in todo]):
                    p, entry = fut.result()
                    if entry is None:
                        failed.append(p)
                    else:
                        entries[p["id"]] = entry
                    done += 1
                    if done % self.save_every == 0:
                        store.save()
                        log(f"  {self.name}: {done}/{len(todo)}")
            except KeyboardInterrupt:
                print("\nОстанавливаюсь, сохраняю прогресс (подожди пару секунд)...")
                ex.shutdown(wait=False, cancel_futures=True)
                store.save()
                print("Сохранено. Запусти скрипт снова, чтобы продолжить.")
                raise SystemExit(1)
            ex.shutdown()
            store.save()
            todo = failed

        if len(todo) > self.max_failed:
            log(f"  {self.name}: не скачано игроков: {len(todo)}. Прогресс сохранён, лист в этом запуске пропускаю.")
            return None
        if todo:
            log(f"  {self.name}: не скачано {len(todo)} игроков (повторю в следующий раз): "
                + ", ".join(f"{p['id']} ({p['nick']})" for p in todo))

        players, records = {}, []
        for p in top:
            e = entries.get(p["id"])
            if e is None:
                continue
            players[p["id"]] = {"n": p["nick"], "r": p.get("rank"), "c": e.get("c")}
            for level, percent, video, *rest in e["recs"]:  # 4-й элемент (место уровня в листе) есть не у всех листов
                records.append({"a": p["id"], "l": level, "p": percent, "v": video, "i": None,
                                "pos": rest[0] if rest else None})
        log(f"  {self.name}: игроков {len(players)}, записей {len(records)}")
        return {"players": players, "records": records}


# ---------- хранилища кеша ----------
class JsonStore:
    """Кеш в json-файле (для скриптов, у которых своих кешей нет)."""

    def __init__(self, path):
        self.path, self.lock, self.data = path, threading.Lock(), {}
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        with self.lock:
            self.data[key] = value

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)


class DictStore:
    """Обёртка над уже существующим кешем-словарём (например, levelcache.json у gd-rating).
    save - функция скрипта, которая сама сохраняет кеш на диск (и сама берёт свою блокировку)."""

    def __init__(self, data, lock, save):
        self.data, self.lock, self._save = data, lock, save

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        with self.lock:
            self.data[key] = value

    def save(self):
        self._save()


# ---------- реестр ----------
_registry = []
_loaded = False


def register(src):
    """Добавляет лист. Проверяет, что название и префикс не пересекаются с уже добавленными."""
    for other in _registry:
        if src.name == other.name:
            raise ValueError(f"лист {src.name!r} уже зарегистрирован")
        if src.prefix.startswith(other.prefix) or other.prefix.startswith(src.prefix):
            raise ValueError(f"префиксы листов {src.name!r} ({src.prefix}) и {other.name!r} ({other.prefix}) "
                             f"пересекаются: ключи записей станет не отличить")
    if src.prefix[:1].isdigit():
        raise ValueError(f"префикс {src.prefix!r} не должен начинаться с цифры: так выглядят аккаунты глобала")
    _registry.append(src)
    return src


def _ensure():
    """Один раз подхватывает все list_*.py рядом с этим файлом."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for path in sorted(glob.glob(os.path.join(HERE, "list_*.py"))):
        mod = os.path.splitext(os.path.basename(path))[0]
        try:
            importlib.import_module(mod)
        except Exception as e:  # сломанный лист не должен ронять остальные
            print(f"Лист {mod}: не удалось подключить ({e}), пропускаю")


class _Pointercrate(Source):
    name, prefix, tag, short = "pointercrate", "pc", "PC", "pc"
    # fetchable = False: рекорды Pointercrate скрипты качают сами (по демонам, а не по игрокам)


register(_Pointercrate())


def all_lists():
    """Все листы (включая Pointercrate), в порядке регистрации."""
    _ensure()
    return list(_registry)


def _off_by_env():
    raw = os.environ.get("GL_LISTS_OFF", "").replace(";", ",").replace(" ", ",")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def is_enabled(src):
    """Включён ли лист: enabled в его классе и нет в переменной окружения GL_LISTS_OFF."""
    return bool(src.enabled) and src.name.lower() not in _off_by_env()


def sources():
    """Включённые листы, которые умеют сами скачивать данные (всё, кроме Pointercrate).
    Выключенные сюда не попадают, но all_lists() и ключи вида wml5 их по-прежнему знают."""
    return [s for s in all_lists() if s.fetchable and is_enabled(s)]


def list_names():
    return tuple(s.name for s in all_lists())


def get(name):
    return next((s for s in all_lists() if s.name == name), None)


# ---------- ключи записей ----------
def is_global(key):
    """Аккаунт глобала: ключ начинается с цифры."""
    return str(key)[:1].isdigit()


def source_of_key(key):
    """Лист, которому принадлежит ключ вида pc7 / wml5, или None (глобал и незнакомые ключи)."""
    k = str(key)
    return next((s for s in all_lists() if k.startswith(s.prefix)), None)


def tag_of(key):
    """Метка для вывода: ' [WML]' у человека без глобала, иначе пусто."""
    s = source_of_key(key)
    return f" [{s.tag}]" if s and not is_global(key) else ""


def key_parts(key):
    """'wml5' -> (лист WML, '5'). Для глобала и незнакомых ключей (None, ключ)."""
    s = source_of_key(key)
    return (s, str(key)[len(s.prefix):]) if s and not is_global(key) else (None, str(key))


# ---------- записи links.json ----------
OTHER_BLOCK = "other_lists"  # блок аккаунтов человека на других листах в записи links.json


def accounts_of(entry):
    """Аккаунты человека на всех листах из записи links.json: {лист: [ID строками]}.
    Читает и новый формат (блок other_lists), и старый (списки прямо в записи), поэтому скрипты,
    которые только читают links.json, не зависят от того, какой формат сейчас в репозитории."""
    out = {}
    if not isinstance(entry, dict):
        return out
    block = entry.get(OTHER_BLOCK) if isinstance(entry.get(OTHER_BLOCK), dict) else {}
    for s in all_lists():
        vals = block.get(s.name)
        if not isinstance(vals, list):
            vals = entry.get(s.name)
        out[s.name] = [str(v).strip() for v in vals if str(v).strip()] if isinstance(vals, list) else []
    return out
