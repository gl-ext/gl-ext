"""Поиск уровней по названию (МОБИЛЬНАЯ версия, лежит в mobile/scripts и подменяет общий levels.py).

Отличие от основного: нет списка глобала и нет особых названий глобала (SPECIAL_LEVEL_NAMES пуст).
Ключ уровня = название уровня на CoreBoard (из mobile/levels_mobile.json; "x..." у алиасов).
Названия, которые на WML пишут иначе, чем на CoreBoard, задаются в mobile/level_aliases.json, а не здесь.

Порядок поиска find(name), от точного к грубому (одинаковые по регистру названия, как "Erebus" и "ErebuS", не путаются):
  1. точное название (только пробелы схлопнуты, регистр важен): сначала названия WML из пар, затем названия CoreBoard и алиасы;
  2. без учёта регистра;
  3. без учёта регистра и без круглых скобок.
Шаги 2 и 3 срабатывают, только если название указывает ровно на один уровень. Если подходят разные уровни, find() возвращает None
(уровень попадает в отчёт «без пары», а не приписывается наугад).
"""
import re
from collections import Counter

SPECIAL_LEVEL_NAMES = {}  # для совместимости с compare.py (там int(ID)): в мобильной версии пусто

ORIGIN_TP, ORIGIN_SPECIAL, ORIGIN_GLOBAL = "levels_mobile.json", "алиасы", "глобал"

PAREN_RE = re.compile(r"\([^()]*\)")


def normalize(name):
    """Название для сравнения: без лишних пробелов и без учёта регистра."""
    return re.sub(r"\s+", " ", str(name or "").strip()).casefold()


def exact(name):
    """Название с учётом регистра, только пробелы схлопнуты."""
    return re.sub(r"\s+", " ", str(name or "").strip())


def loose(name):
    """Как normalize, но без круглых скобок и их содержимого."""
    s = str(name or "")
    while True:
        t = PAREN_RE.sub(" ", s)
        if t == s:
            break
        s = t
    return normalize(s)


class _Layer:
    """Название -> ключ. Если одно и то же название указывает на разные ключи, оно считается неоднозначным."""

    def __init__(self, fn):
        self.fn = fn
        self.map = {}
        self.ambiguous = set()

    def add(self, name, entry):
        n = self.fn(name)
        if not n:
            return
        if n not in self.map:
            self.map[n] = entry
        elif self.map[n][0] != entry[0]:
            self.ambiguous.add(n)

    def get(self, name):
        n = self.fn(name)
        if n in self.ambiguous:
            return None
        return self.map.get(n)


class LevelIndex:
    """Название уровня -> ключ уровня (название на CoreBoard или x... у алиасов)."""

    def __init__(self, global_loader=None):
        self._exact = {}                      # точные названия (приоритет у пар WML)
        self._exact_amb = set()               # точное название указывает на разные уровни: не угадываем
        self._fold = _Layer(normalize)
        self._loose = _Layer(loose)
        self.stats = Counter()

    def _add(self, name, key, origin, priority=False):
        e = exact(name)
        if e and (e not in self._exact):
            self._exact[e] = (str(key), origin)
        elif e and self._exact[e][0] != str(key):
            self._exact_amb.add(e)
        entry = (str(key), origin)
        self._fold.add(name, entry)
        self._loose.add(name, entry)

    def add_wml(self, wml_name, key):
        """Название уровня на WML, уже сопоставленное с CoreBoard (поле wml_name в levels_mobile.json). Вызывать ПЕРВЫМ."""
        self._add(wml_name, key, ORIGIN_TP)

    def add_tp(self, tp_levels):
        """tp_levels: словарь levels из levels_mobile.json: ключ -> {"name": ..., "wml_name": ...}."""
        for key, e in (tp_levels or {}).items():
            if e.get("wml_name"):
                self.add_wml(e["wml_name"], key)
        for key, e in (tp_levels or {}).items():
            self._add(e.get("name"), key, ORIGIN_TP)

    def add_alias(self, name, key):
        """Название уровня на WML, которое отличается от названия на CoreBoard (mobile/level_aliases.json)."""
        self._add(name, key, ORIGIN_SPECIAL)

    def add_special(self, extra=None):
        """extra: {ключ уровня: название}. Дополнительные названия для уже известных ключей."""
        for key, name in (extra or {}).items():
            self._add(name, key, ORIGIN_SPECIAL)

    def find(self, name):
        if exact(name) in self._exact_amb:
            return None
        hit = self._exact.get(exact(name)) or self._fold.get(name) or self._loose.get(name)
        if hit is None:
            return None
        self.stats[hit[1]] += 1
        return hit[0]

    def is_ambiguous(self, name):
        """Название подходит сразу нескольким уровням (для отчёта)."""
        if exact(name) in self._exact_amb:
            return True
        return exact(name) not in self._exact and (normalize(name) in self._fold.ambiguous
                                                   or loose(name) in self._loose.ambiguous)
