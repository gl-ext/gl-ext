"""Поиск уровней по названию. ОДНО место для всех скриптов (gd-rating, wml, compare, add-video-links и т.д.).

Зачем отдельный файл: раньше список особых названий и сам поиск были скопированы в несколько скриптов
и расходились. Теперь дописал название здесь, и оно работает везде.

Как пользоваться:
    import levels
    index = levels.LevelIndex.build(tp=levels_tp["levels"], levels=список_уровней_глобала)
    key = index.find("Sonic Wave Infinity")     # ключ уровня (ID глобала строкой или x...), либо None
    print(index.stats)                           # откуда нашлись названия

Порядок поиска: levels_tp.json, потом особые названия, потом список глобала. Побеждает первое найденное.
Список глобала можно не передавать: тогда он скачивается один раз и только если какое-то название не нашлось
(global_loader).
"""
import re
from collections import Counter

# Особые названия: ID уровня в глобале -> как его называют в рейтинге и на листах.
# Нужны, когда у нескольких уровней одинаковое название или на листах уровень называют иначе, чем в API.
# Чтобы добавить ещё один, допишите строку по тому же образцу.
SPECIAL_LEVEL_NAMES = {
    "3299": "Sakupen circles (Nick24)",
    "3351": "Thinking Space II (Legacy)",
    "2234": "Deimos (Endlevel)",
}

ORIGIN_TP, ORIGIN_SPECIAL, ORIGIN_GLOBAL = "levels_tp.json", "особые названия", "глобал"


def normalize(name):
    """Название для сравнения: без лишних пробелов и без учёта регистра."""
    return re.sub(r"\s+", " ", str(name or "").strip()).casefold()


class LevelIndex:
    """Название уровня -> ключ уровня (ID глобала строкой или x... для уровней, которых нет в глобале)."""

    def __init__(self, global_loader=None):
        self._names = {}  # нормализованное название -> (ключ, откуда)
        self._global_loader = global_loader  # функция без аргументов -> список уровней глобала; вызывается один раз
        self._global_tried = False
        self.stats = Counter()

    def _add(self, name, key, origin):
        n = normalize(name)
        if n and n not in self._names:  # побеждает первое найденное
            self._names[n] = (str(key), origin)

    def add_tp(self, tp_levels):
        """tp_levels: словарь из levels_tp.json (поле levels): ключ -> {"name": ...}."""
        for key, e in (tp_levels or {}).items():
            self._add(e.get("name"), key, ORIGIN_TP)

    def add_special(self, extra=None):
        for key, name in {**SPECIAL_LEVEL_NAMES, **(extra or {})}.items():
            self._add(name, key, ORIGIN_SPECIAL)

    def add_global(self, levels):
        """levels: список уровней глобала ({"id", "name"}). Для уровней с особым названием берётся оно,
        чтобы старое название из API не перехватывало чужой уровень."""
        for l in levels or []:
            if l.get("id") is not None:
                self._add(SPECIAL_LEVEL_NAMES.get(str(l["id"]), l.get("name")), l["id"], ORIGIN_GLOBAL)

    def find(self, name):
        n = normalize(name)
        hit = self._names.get(n)
        if hit is None and self._global_loader and not self._global_tried:
            self._global_tried = True
            try:
                self.add_global(self._global_loader())
            except Exception as e:
                print(f"  Не удалось загрузить список глобала: {e}")
            hit = self._names.get(n)
        if hit is None:
            return None
        self.stats[hit[1]] += 1
        return hit[0]

    @classmethod
    def build(cls, tp=None, levels=None, extra_special=None, global_loader=None):
        index = cls(global_loader=global_loader)
        index.add_tp(tp)
        index.add_special(extra_special)
        index.add_global(levels)
        return index


def fetch_global_levels(get_json, url_template, limit=100, log=print):
    """Постранично качает список уровней глобала. get_json - функция скрипта (url -> словарь),
    url_template - адрес с {limit} и {offset}. Возвращает список {"id", "name"}."""
    out, seen, offset = [], set(), 0
    log("Загружаю список уровней глобала (для названий, которых нет в levels_tp.json)...")
    while True:
        part = (get_json(url_template.format(limit=limit, offset=offset)).get("data") or {}).get("levels") or []
        new = [l for l in part if l.get("id") is not None and l["id"] not in seen]
        if not new:
            break
        for l in new:
            seen.add(l["id"])
            out.append({"id": l["id"], "name": l.get("name")})
        offset += limit
    log(f"  Уровней загружено из глобала: {len(out)}")
    return out
