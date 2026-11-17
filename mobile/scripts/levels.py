"""Поиск уровней по названию (МОБИЛЬНАЯ версия, лежит в mobile/scripts и подменяет общий levels.py).

Отличие от основного: нет списка глобала и нет особых названий глобала (SPECIAL_LEVEL_NAMES пуст).
Ключ уровня = свой ID из mobile/levels_mobile.json (числа 1, 2, 3...; x... у алиасов).
Названия, которые на WML пишут иначе, чем на CoreBoard, задаются в mobile/level_aliases.json, а не здесь.
"""
import re
from collections import Counter

SPECIAL_LEVEL_NAMES = {}  # для совместимости с compare.py (там int(ID)): в мобильной версии пусто

ORIGIN_TP, ORIGIN_SPECIAL, ORIGIN_GLOBAL = "levels_mobile.json", "алиасы", "глобал"


def normalize(name):
    """Название для сравнения: без круглых скобок (и их содержимого), без лишних пробелов, без учёта регистра."""
    s = str(name or "")
    while True:
        t = re.sub(r"\([^()]*\)", " ", s)
        if t == s:
            break
        s = t
    return re.sub(r"\s+", " ", s).strip().casefold()


class LevelIndex:
    """Название уровня -> ключ уровня (свой ID строкой или x... у алиасов)."""

    def __init__(self, global_loader=None):
        self._names = {}
        self.stats = Counter()

    def _add(self, name, key, origin):
        n = normalize(name)
        if n and n not in self._names:  # побеждает первое найденное
            self._names[n] = (str(key), origin)

    def add_tp(self, tp_levels):
        """tp_levels: словарь levels из levels_mobile.json: ключ -> {"name": ...}."""
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
        hit = self._names.get(normalize(name))
        if hit is None:
            return None
        self.stats[hit[1]] += 1
        return hit[0]
