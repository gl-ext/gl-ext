#!/usr/bin/env python3
"""
Список уровней МОБИЛЬНОЙ доски CoreBoard -> mobile/levels_mobile.json (аналог levels_tp.json).

У мобильных уровней нет ID глобала, поэтому здесь СВОЯ система ID уровней:
  - у каждого уровня число 1, 2, 3... (ключ в levels_mobile.json);
  - уровень находится по названию: пока он есть на странице, его ID не меняется, даже если место сдвинулось;
  - новый уровень получает следующий свободный ID (поле "next_id" в файле), ID удалённых уровней не переиспользуются;
  - если один уровень пропал, а новый появился на том же месте, это считается переименованием, ID сохраняется;
  - алиасы ("Имя = Уровень (T=.., t=..)") получают ключ "xИмя", как в levels_tp.json.

Формат файла:
  {"next_id": 120,
   "levels": {"1": {"place": 1, "name": "...", "T": 99, "t": 66}, ..., "xАлиас": {"name", "like", "place", "T", "t"}}}

Страница уровней: https://coreboard.pythonanywhere.com/mobile/levels (адрес меняется --url). Разбор такой же, как у основной
доски: <li id="level-N">Имя (T=.., t=..)</li>. Если разметка другая, скрипт ничего не запишет и скажет об этом.

Запуск:
  python mobile/scripts/mobile-levels.py                записать mobile/levels_mobile.json
  python mobile/scripts/mobile-levels.py --dry-run      только показать, что изменилось
  python mobile/scripts/mobile-levels.py --html page.html   разобрать сохранённую страницу, ничего не качая
  python mobile/scripts/mobile-levels.py --force        записать, даже если уровней стало подозрительно мало
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent                      # mobile/
LEVELS_FILE = DATA / "levels_mobile.json"
DEFAULT_URL = "https://coreboard.pythonanywhere.com/mobile/levels"
UA = "gl-ext-top6"

NUM = r"\d+(?:\.\d+)?"
LEVEL_RE = re.compile(r"^(.*?) \(T=(" + NUM + r"), t=(" + NUM + r")\)$")


def norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip()).casefold()


def _num(s):
    f = float(s)
    return int(f) if f == int(f) else f


def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def load_old():
    try:
        data = json.loads(LEVELS_FILE.read_text("utf-8"))
        if isinstance(data.get("levels"), dict):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {"next_id": 1, "levels": {}}


def parse_page(page):
    """-> (main, aliases). main: [(place, name, T, t)], aliases: [(alias, like, T, t)]."""
    main = []
    for m in re.finditer(r'<li id="level-(\d+)">(.*?)</li>', page, re.S):
        mm = LEVEL_RE.match(_clean(m.group(2)))
        if not mm:
            print(f"  пропускаю нераспознанную строку level-{m.group(1)}: {_clean(m.group(2))!r}")
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


def assign_ids(main, old):
    """Сопоставляет уровни страницы со старыми ID. Возвращает (rows, next_id, new, renamed, removed)."""
    old_levels = {k: v for k, v in old["levels"].items() if not str(k).startswith("x")}
    by_name = {}
    for k, v in old_levels.items():
        by_name.setdefault(norm(v.get("name")), k)
    next_id = max([int(old.get("next_id") or 1)] + [int(k) + 1 for k in old_levels if str(k).isdigit()])

    page_names = {norm(name) for _, name, _, _ in main}
    new_rows = [r for r in main if norm(r[1]) not in by_name]
    gone = {k: v for k, v in old_levels.items() if norm(v.get("name")) not in page_names}

    # один пропавший и один новый уровень на ОДНОМ месте = переименование, ID сохраняем
    renamed, rename_to = [], {}
    for place, name, T, t in new_rows:
        same = [k for k, v in gone.items() if v.get("place") == place and k not in rename_to.values()]
        peers = [r for r in new_rows if r[0] == place]
        if len(same) == 1 and len(peers) == 1:
            rename_to[norm(name)] = same[0]
            renamed.append(f"{place}. {gone[same[0]]['name']} -> {name} (ID {same[0]})")

    rows, used, new = [], set(), []
    for place, name, T, t in main:
        key = by_name.get(norm(name)) or rename_to.get(norm(name))
        if key is None or key in used:
            key = str(next_id)
            next_id += 1
            new.append(f"{place}. {name} -> ID {key}")
        used.add(key)
        rows.append((key, {"place": place, "name": name, "T": T, "t": t}))
    removed = [f"{v.get('place')}. {v.get('name')} (ID {k})" for k, v in gone.items() if k not in rename_to.values()]
    return rows, next_id, new, renamed, removed


def save(entries, next_id, path):
    lines = ["{", f'  "next_id": {next_id},', '  "levels": {']
    for i, (k, v) in enumerate(entries):
        lines.append(f"    {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}"
                     + ("," if i < len(entries) - 1 else ""))
    lines += ["  }", "}", ""]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text("\n".join(lines), "utf-8")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Уровни мобильной доски CoreBoard -> levels_mobile.json")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--html", help="разобрать сохранённую страницу вместо скачивания")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="не останавливаться, если уровней мало")
    args = ap.parse_args()

    if args.html:
        page = Path(args.html).read_text("utf-8")
    else:
        try:
            req = urllib.request.Request(args.url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                page = r.read().decode("utf-8")
        except Exception as e:
            sys.exit(f"Не удалось скачать {args.url}: {e}. levels_mobile.json не тронут.")

    old = load_old()
    main_rows, aliases = parse_page(page)
    old_main = sum(1 for k in old["levels"] if not str(k).startswith("x"))
    if not main_rows or (not args.force and old_main and len(main_rows) < old_main * 0.8) \
            or (not args.force and not old_main and len(main_rows) < 20):
        sys.exit(f"Со страницы разобрано {len(main_rows)} уровней (в файле было {old_main}): похоже на сбой или "
                 f"другую разметку. levels_mobile.json не тронут. Пришли кусок HTML вокруг первого уровня "
                 f"или запусти с --force.")

    rows, next_id, new, renamed, removed = assign_ids(main_rows, old)
    entries = list(rows)
    name2place = {name: place for place, name, _, _ in main_rows}
    for alias, like, T, t in aliases:
        entries.append(("x" + alias, {"name": alias, "like": like, "place": name2place[like], "T": T, "t": t}))

    print(f"Уровней: {len(rows)}, алиасов: {len(aliases)}, следующий свободный ID: {next_id}")
    for title, items in (("Новые уровни (выдан новый ID)", new), ("Переименованы (ID сохранён)", renamed),
                         ("Пропали со страницы (их ID больше не используется)", removed)):
        if items:
            print(title + ":")
            for s in items:
                print("  " + s)

    if args.dry_run:
        print("--dry-run: файл не записан.")
        return
    save(entries, next_id, LEVELS_FILE)
    print(f"{LEVELS_FILE.name} записан.")


main()
