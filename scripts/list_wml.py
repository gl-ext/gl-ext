"""Лист World Mobile List. Образец для новых листов (копируй и меняй).
Низкоуровневая работа с сайтом (запросы, разбор страницы) лежит в wml.py, здесь только описание листа."""
import time
import urllib.error

from lists_core import PageSource, register
import wml as site


class WML(PageSource):
    name = "wml"      # other_lists.wml, --src wml, --no-wml
    prefix = "wml"    # игрок без глобала получает ключ wml<id>
    tag = "WML"       # метка [WML] в рейтинге
    short = "wml"     # gdr_gl_wml.txt
    rev = 3           # ревизия разбора страницы игрока. 2: блок «Created» больше не читается. 3: у рекорда сохраняется место уровня (pos)

    def display_nick(self, nick):
        return site.strip_clan(nick) or nick  # '[67] Kristage' -> 'Kristage'

    def top(self):
        """Топ по 50 игроков за запрос. Забаненных пропускаем (как в wml.py). version = очки игрока."""
        out, seen, after = [], set(), 0
        for _ in range(400):  # защита от бесконечного цикла
            items = site.items_of(site.get_json(site.RANKING_URL.format(after=after)))
            new = 0
            for it in items:
                if not isinstance(it, dict):
                    continue
                pl = site.normalize_player(it)
                pid = str(pl["id"]) if pl["id"] is not None else None
                if pid is None or pid in seen:
                    continue
                seen.add(pid)
                new += 1
                if not pl["banned"]:
                    out.append({"id": pid, "nick": str(pl["name"] or ""), "rank": pl["rank"], "version": pl["score"]})
            if not new:
                break
            after += site.PAGE_SIZE
            time.sleep(self.pause)
        return out

    def player(self, pid):
        try:
            page = site.http_get(site.PLAYER_URL.format(id=pid))
        except urllib.error.HTTPError as e:
            if e.code == 404:  # страницы игрока нет
                return {"country": None, "records": []}
            raise
        data = site.parse_panel(page)
        if data is None:
            raise RuntimeError("в ответе нет панели игрока")
        return {"country": data.get("country") or None,
                "records": [{"level": r["level"], "percent": r["percent"], "video": r.get("video"), "pos": r.get("pos")}
                            for r in data["records"]]}

    def fetch(self, store, workers=3, log=print):
        """Как у всех листов, плюс: если в WML два уровня с одним названием (например, Pandemonium),
        рекорды на нижнем из них не учитываются (он не тот уровень, что в глобале)."""
        data = super().fetch(store, workers, log)
        if not data:
            return data
        cut = site.duplicate_cutoffs((r["l"], r.get("pos")) for r in data["records"])
        if cut:
            dropped = {}
            kept = []
            for r in data["records"]:
                if site.is_lower_duplicate(cut, r["l"], r.get("pos")):
                    n = site.normalize(r["l"])
                    dropped[n] = dropped.get(n, 0) + 1
                else:
                    kept.append(r)
            data["records"] = kept
            site.report_duplicates(cut, dropped, log)
        return data


register(WML())
