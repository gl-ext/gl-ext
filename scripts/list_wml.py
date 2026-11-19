"""Лист World Mobile List, версия «по уровням».

У WML нет своего API, поэтому идём от уровней, а не от игроков:
  1. со страницы /demonlist/ берём список уровней, оставляем первые TOP_LEVELS (100);
  2. у каждого уровня открываем его страницу /demonlist/<место в списке> и берём:
       - ID уровня в игре (ingame_id, кнопка «ID»),
       - все записи (в том числе скрытые за «Show all»): игрок, страна, процент, видео,
       - того, кто прошёл уровень первым (first beaten by), у него записи в таблице может не быть;
  3. отдаём данные в едином виде. Поле "i" у каждой записи = ingame_id уровня, и gd-rating.py сверяет
     его с ingame_id уровней глобала. Название уровня при этом решает только для уровней,
     которых в глобале нет вообще, поэтому рекорды не перескакивают на другой уровень
     только из-за одинакового названия.

Игроки берутся только те, у кого есть рекорд на этих уровнях (полного рейтинга WML мы не качаем).
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from lists_core import Source, register
import wml as site


class WML(Source):
    name = "wml"      # other_lists.wml, --src wml, --no-wml
    prefix = "wml"    # игрок без глобала получает ключ wml<id>
    tag = "WML"       # метка [WML] в рейтинге
    short = "wml"     # gdr_gl_wml.txt
    fetchable = True

    TOP_LEVELS = 100  # сколько верхних уровней WML обходим
    pause = 0.4       # пауза после каждого запроса, секунд
    max_failed = 10   # сколько страниц уровней можно не скачать и всё равно продолжить
    rev = 5           # ревизия разбора страницы: поправил парсер, увеличь число, и кеш перекачается сам
    ttl = 7 * 86400   # используется только gd-rating.py (--list-ttl), здесь страницы качаем каждый раз

    def display_nick(self, nick):
        return site.strip_clan(nick) or nick  # '[67] Kristage' -> 'Kristage'

    def _key(self, level_id):
        return f"{self.name}:L{level_id}"  # L, чтобы не путать с кешем игроков старой версии (wml:<id>)

    def fetch(self, store, workers=3, log=print):
        # 1. список уровней
        try:
            entries = site.fetch_wml_level_list()  # [(id уровня на WML, название)] в порядке списка
        except Exception as e:
            log(f"  {self.name}: список уровней не скачался: {e}")
            return None
        if not entries:
            log(f"  {self.name}: список уровней пустой, лист пропускаю")
            return None
        entries = entries[:self.TOP_LEVELS]

        # 2. страница каждого уровня
        def job(wid, name, pos):
            key = self._key(wid)
            try:
                page = site.parse_level_page(site.http_get(site.LEVEL_URL.format(pos=pos)))
                if page is None:
                    raise RuntimeError("в ответе нет страницы уровня")
                # Защита от сдвига списка (пока качаем, могли добавить уровень) и от ошибки в адресе:
                # название из списка должно быть в заголовке страницы, иначе это другой уровень
                if page["title"] and site.normalize(name) not in site.normalize(page["title"]):
                    raise RuntimeError(f"на месте #{pos} другой уровень (заголовок {page['title']!r})")
                entry = {"at": time.time(), "rev": self.rev, "name": name, "ingame": page["ingame"],
                         "first": page["first"], "recs": page["records"]}
                store.put(key, entry)
                return wid, entry
            except Exception as e:
                old = store.get(key)  # сайт не ответил: берём прошлую копию уровня (даже старую)
                if old and old.get("rev") == self.rev:
                    return wid, old
                log(f"    уровень {name!r} (WML {wid}): не скачался: {e}")
                return wid, None
            finally:
                time.sleep(self.pause)

        log(f"  {self.name}: уровней {len(entries)}, качаю страницы")
        got, failed = {}, []
        names = dict(entries)
        ex = ThreadPoolExecutor(max(1, workers))
        try:
            futs = [ex.submit(job, wid, name, pos) for pos, (wid, name) in enumerate(entries, 1)]
            for n, fut in enumerate(as_completed(futs), 1):
                wid, entry = fut.result()
                if entry is None:
                    failed.append(wid)
                else:
                    got[wid] = entry
                if n % 25 == 0:
                    store.save()
                    log(f"  {self.name}: {n}/{len(entries)}")
        except KeyboardInterrupt:
            print("\nОстанавливаюсь, сохраняю прогресс (подожди пару секунд)...")
            ex.shutdown(wait=False, cancel_futures=True)
            store.save()
            raise SystemExit(1)
        ex.shutdown()
        store.save()

        if len(failed) > self.max_failed:
            log(f"  {self.name}: не скачано уровней: {len(failed)}. Лист в этом запуске пропускаю.")
            return None
        if failed:
            log(f"  {self.name}: не скачано {len(failed)} уровней (повторю в следующий раз): "
                + ", ".join(f"{names[w]} ({w})" for w in failed))

        # 3. сборка в единый вид
        players, records, no_ingame = {}, [], []
        for pos, (wid, name) in enumerate(entries, 1):  # pos = место уровня в списке WML
            e = got.get(wid)
            if e is None:
                continue
            iid = e.get("ingame")
            if not iid:
                no_ingame.append(name)  # без ID сравнить с глобалом нельзя, а гадать по названию не будем
                continue

            best = {}  # ID игрока на WML -> запись (по уровню у игрока остаётся лучший процент)
            for r in e.get("recs") or []:
                pid = str(r["id"])
                if pid not in best or r["percent"] > best[pid]["percent"]:
                    best[pid] = r
                players.setdefault(pid, {"n": r["nick"], "r": None, "c": r.get("cc")})
                if not players[pid].get("c") and r.get("cc"):
                    players[pid]["c"] = r["cc"]

            first = e.get("first")  # прошёл первым: в таблице записей его может не быть, добавляем сами
            if first and str(first["id"]) not in best:
                pid = str(first["id"])
                best[pid] = {"id": pid, "nick": first["nick"], "percent": 100, "video": None}
                players.setdefault(pid, {"n": first["nick"], "r": None, "c": None})

            for pid, r in best.items():
                records.append({"a": pid, "l": name, "p": r["percent"], "v": r.get("video"),
                                "i": str(iid), "pos": pos})

        if no_ingame:
            log(f"  {self.name}: у {len(no_ingame)} уровней нет ID в игре, их записи пропущены: "
                + ", ".join(no_ingame[:10]))
        if not records:
            log(f"  {self.name}: записей нет, лист пропускаю")
            return None
        log(f"  {self.name}: уровней {len(got)}, игроков {len(players)}, записей {len(records)}")
        return {"players": players, "records": records}


register(WML())