#!/usr/bin/env python3
"""
Разбирает таблицы CoreBoard (https://coreboard.pythonanywhere.com) в JSON.
Страницы отдают всю таблицу прямо в HTML, поэтому хватает одного запроса на страницу.

Запуск:
  python coreboard_parse.py              основной топ (world, можно коротко w)
  python coreboard_parse.py rc           Топ РК
  python coreboard_parse.py world rc     оба топа, каждый в своём файле
  python coreboard_parse.py all          оба топа, объединённые в один файл (cbd.json)
  python coreboard_parse.py -f x.html    разобрать сохранённый файл, ничего не качать
  python coreboard_parse.py -r           скачать заново, даже если файл свежий
Вместо world и rc можно написать путь страницы сайта, например main/rc.

Результат: cbd_w.json (основной топ), cbd_rc.json (Топ РК), cbd.json (оба топа вместе).
Рядом с каждым .json лежит .txt (cbd_w.txt, cbd_rc.txt, cbd.txt) в формате строк gd-rating:
его читает compare.py.
Скачанные страницы лежат рядом как cbd_w.html и cbd_rc.html.
  [{"rank": 1, "name": "Bacon", "country": "Казахстан", "flag": "KZ", "points": 13.39,
    "records": [{"level": "Tidal Wave", "percent": 100}, ...], "raw": "Tidal Wave, ..."}]

Разбор не привязан к конкретным тегам: берутся строки таблицы и в них ячейки. Игрок это
ячейка со ссылкой (class="player"), очки это ячейка class="pts", записи это class="rec",
место это class="rank". Если классов нет, берётся порядок колонок: место, игрок, страна,
очки, записи.

Сайт на бесплатном хостинге, поэтому скрипт ходит на него по одному запросу на страницу
и держит скачанное в файле час (меняется --ttl).
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://coreboard.pythonanywhere.com/"
HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser(description="Разбор таблиц CoreBoard в JSON")
ap.add_argument("pages", nargs="*", help="world, rc или all (оба топа в один файл); по умолчанию world")
ap.add_argument("-f", "--file", help="разобрать этот HTML-файл, ничего не качая")
ap.add_argument("-r", "--refresh", action="store_true", help="скачать заново")
ap.add_argument("--ttl", type=int, default=3600, help="сколько секунд считать скачанную страницу свежей")
ap.add_argument("-o", "--out", help="имя файла результата (когда результат один)")
args = ap.parse_args()

PCT = re.compile(r"^(.*?)(?:\s+(\d+(?:\.\d+)?)%)?$")


class Rows(HTMLParser):
    """Собирает строки таблиц: в каждой ячейки с классом, текстом и ссылкой."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = {"cls": (a.get("class") or "").split(), "text": "", "href": None, "link_cls": [],
                         "head": tag == "th"}
        elif tag == "a" and self.cell is not None:
            self.cell["href"] = a.get("href")
            self.cell["link_cls"] = (a.get("class") or "").split()

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.cell["text"] = self.cell["text"].strip()
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def split_country(s):
    """'🇺🇸United States' -> ('United States', 'US'); без флага -> (текст, '')."""
    s = s.strip()
    flag = ""
    while s and 0x1F1E6 <= ord(s[0]) <= 0x1F1FF:
        flag += chr(ord("A") + ord(s[0]) - 0x1F1E6)
        s = s[1:]
    return s.strip(), flag


def parse_records(raw):
    # Названия уровней могут содержать запятую, тогда разбиение по ", " ошибётся:
    # поэтому рядом сохраняется и исходная строка "raw".
    out = []
    for part in raw.split(", "):
        part = part.strip()
        if not part:
            continue
        m = PCT.match(part)
        out.append({"level": m.group(1).strip(), "percent": float(m.group(2)) if m.group(2) else 100})
    return out


def to_float(s):
    try:
        return float(s.replace(",", ".").strip())
    except ValueError:
        return None


def parse_page(page):
    p = Rows()
    p.feed(page)
    players = []
    for row in p.rows:
        if any(c["head"] for c in row) or len(row) < 4:
            continue
        by_cls = lambda name: next((c for c in row if name in c["cls"]), None)
        rank_c = by_cls("rank") or row[0]
        player_c = next((c for c in row if "player" in c["link_cls"]), None) or \
            next((c for c in row if c["href"]), None) or row[1]
        pts_c = by_cls("pts") or row[3]
        rec_c = by_cls("rec") or row[-1]
        used = {id(rank_c), id(player_c), id(pts_c), id(rec_c)}
        country_c = next((c for c in row if id(c) not in used), None)
        rank = to_float(rank_c["text"])
        points = to_float(pts_c["text"])
        if rank is None or points is None:
            continue
        cname, flag = split_country(country_c["text"]) if country_c else ("", "")
        raw = re.sub(r"\s+", " ", rec_c["text"]).strip()
        players.append({
            "rank": int(rank), "name": player_c["text"], "href": player_c["href"],
            "country": cname, "flag": flag, "points": points,
            "records": parse_records(raw), "raw": raw,
        })
    return players


def get_html(path):
    cache = HERE / (f"cbd_{short_name(path)}.html")
    if not args.refresh and cache.exists() and time.time() - cache.stat().st_mtime < args.ttl:
        print(f"{path}: беру сохранённую страницу {cache.name} (свежая, до {args.ttl} с)")
        return cache.read_text("utf-8")
    print(f"{path}: скачиваю {BASE + path}")
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "Mozilla/5.0 (gd-rating helper)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8")
    cache.write_text(text, "utf-8")
    time.sleep(1)  # не нагружаем бесплатный хостинг, если страниц несколько
    return text


ALIASES = {"world": "main/world", "w": "main/world", "rc": "main/rc"}
# короткие имена для файлов: путь страницы -> кусок имени (cbd_w, cbd_rc)
SHORT = {"main/world": "w", "main/rc": "rc"}


def short_name(path):
    path = path.strip("/")
    return SHORT.get(path, path.replace("/", "_"))


def load_rows(path):
    rows = parse_page(get_html(path))
    if not rows:
        print(f"{path}: ни одной строки не найдено, разметка страницы другая. "
              f"Пришли кусок HTML вокруг первого игрока.")
    return rows


def merge(world, rc):
    """Один список: каждый игрок один раз, с местом в каждом из топов."""
    people = {}
    for src_name, rows in (("world", world), ("rc", rc)):
        for r in rows:
            p = people.get(r["name"].lower())
            if p is None:
                p = {"name": r["name"], "country": r["country"], "flag": r["flag"], "points": r["points"],
                     "world_rank": None, "rc_rank": None, "records": r["records"], "raw": r["raw"]}
                people[r["name"].lower()] = p
            p[src_name + "_rank"] = r["rank"]
            if src_name == "rc" and p["world_rank"] is not None:
                # игрок уже взят из основного топа: если в Топе РК данные другие, отмечаем
                if abs(p["points"] - r["points"]) > 0.005:
                    p["points_rc"] = r["points"]
                if p["raw"] != r["raw"]:
                    p["raw_rc"] = r["raw"]
    return sorted(people.values(), key=lambda p: (p["points"], p["world_rank"] or 10 ** 9))


def save(rows, out_name):
    """Пишет JSON и рядом .txt в формате строк gd-rating (его читает compare.py):
    '5) 🇵🇱 dovvin (ORBIT, Thinking Space II 88%) - 14.41'. ID в строке нет."""
    out = HERE / out_name
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
    lines = []
    for i, r in enumerate(rows, 1):
        flag = r.get("flag") or ""
        emoji = "".join(chr(0x1F1E6 + ord(c) - 65) for c in flag) if len(flag) == 2 else ""
        lines.append(f"{r.get('rank', i)}) {emoji + ' ' if emoji else ''}{r['name']} "
                     f"({r['raw']}) - {r['points']:.2f}")
    out.with_suffix(".txt").write_text("\n".join(lines), "utf-8")
    return out


def report(rows, page=None):
    if page is not None:
        pages = set(re.findall(r'href="([^"]*[?&]page=\d+[^"]*)"', page))
        if pages:
            print(f"  ВНИМАНИЕ: на странице есть ссылки на другие страницы ({len(pages)}), "
                  f"например: {sorted(pages)[:3]}")
    for r in rows[:3]:
        place = r.get("rank") or r.get("world_rank") or r.get("rc_rank")
        print(f"  {place}. {r['name']} ({r['flag'] or r['country']}) {r['points']}: "
              f"{len(r['records'])} записей")


def main():
    if args.file:
        page = Path(args.file).read_text("utf-8")
        rows = parse_page(page)
        if not rows:
            print("Ни одной строки не найдено, разметка страницы другая.")
            return
        out = save(rows, args.out or f"cbd_{Path(args.file).stem}.json")
        print(f"Игроков разобрано {len(rows)}, сохранено {out.name} и {out.with_suffix('.txt').name}")
        report(rows, page)
        return

    pages = args.pages or ["world"]
    if "all" in pages:
        world, rc = load_rows(ALIASES["world"]), load_rows(ALIASES["rc"])
        if not world and not rc:
            return
        rows = merge(world, rc)
        both = sum(1 for p in rows if p["world_rank"] and p["rc_rank"])
        only_rc = sum(1 for p in rows if p["rc_rank"] and not p["world_rank"])
        diff = sum(1 for p in rows if "points_rc" in p)
        out = save(rows, args.out or "cbd.json")
        print(f"Объединено: игроков {len(rows)} (основной топ {len(world)}, Топ РК {len(rc)}, "
              f"в обоих {both}, только в Топе РК {only_rc})")
        if diff:
            print(f"  у {diff} игроков очки в двух топах разные (поле points_rc)")
        print(f"Сохранено: {out.name} и {out.with_suffix('.txt').name}")
        report(rows)
        return

    for p in pages:
        path = ALIASES.get(p, p)
        rows = load_rows(path)
        if rows:
            name = short_name(path)
            out = save(rows, args.out if args.out and len(pages) == 1 else f"cbd_{name}.json")
            print(f"{p}: игроков разобрано {len(rows)} (места {rows[0]['rank']}-{rows[-1]['rank']}), "
                  f"сохранено {out.name} и {out.with_suffix('.txt').name}")
            report(rows)


main()