// Собирает points.json: таблица игроков Demonlist, в которой у всех игроков из links.json
// к очкам добавлены записи с Pointercrate. Запуск: node scripts/build-points.mjs
// Узнать, как это работает в расширении, можно в content.js (levelPoints / recordPoints):
// формулы здесь должны совпадать с ним.
import { readFile, writeFile } from "node:fs/promises";

const DL = "https://api.demonlist.org/";
const PC = "https://pointercrate.com/api/";
const CONC = Number(process.env.CONCURRENCY || 4);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Сайты могут отклонять запросы без нормального User-Agent
const HEADERS = {
  Accept: "application/json",
  "User-Agent": "gl-ext-points-builder (+https://github.com/gl-ext/gl-ext)",
};

async function getJson(url, tries = 6) {
  let last;
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url, { headers: HEADERS });
      if (r.ok) return await r.json();
      const body = (await r.text().catch(() => "")).replace(/\s+/g, " ").slice(0, 200);
      last = new Error(`${r.status} ${url}${body ? " — " + body : ""}`);
      if (r.status !== 429 && r.status < 500) break; // 403, 404 и т.п. — повторять бессмысленно
    } catch (e) { last = e; }
    await sleep(1000 * 2 ** i);
  }
  throw last;
}

async function pool(items, limit, fn) {
  const out = new Array(items.length);
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (i < items.length) { const k = i++; out[k] = await fn(items[k], k); }
  }));
  return out;
}

/* ---------- очки (те же формулы, что в content.js) ---------- */
export function levelPoints(h, H) {
  if (h <= 8) return 1040 - 40 * h;
  if (h <= 10) return 1200 - 60 * h;
  if (h <= 20) return 800 - 20 * h;
  if (h <= 50) return 480 - 4 * h;
  if (h <= 75) return 680 - 8 * h;
  if (h <= 150) return 155 - h;
  if (h <= 250) return 5 - 0.4 * Math.ceil((h - 150) / 10);
  return 1 - 0.01 * Math.ceil((95 * (h - 250)) / (H - 250));
}
export function recordPoints(h, H, c, tau) {
  const P = levelPoints(h, H);
  if (c >= 100) return P;
  if (tau == null || !Number.isFinite(Number(tau)) || Number(tau) >= 99) return null;
  return 0.25 * P * ((c - Number(tau)) / (99 - Number(tau)) + 1);
}

/* ---------- загрузка данных ---------- */
async function loadAllLevels() {
  const out = [];
  for (let offset = 0; ; ) {
    const d = await getJson(`${DL}level/classic/list?limit=100&offset=${offset}`);
    const arr = d?.data?.levels || [];
    if (!arr.length) break;
    out.push(...arr);
    offset += arr.length;
  }
  return out;
}

async function loadAllPcDemons() {
  const out = [], seen = new Set();
  for (let after = 0; ; ) {
    const arr = await getJson(`${PC}v2/demons/?limit=100&after=${after}`);
    if (!Array.isArray(arr) || !arr.length) break;
    let added = 0;
    for (const d of arr) if (!seen.has(d.id)) { seen.add(d.id); out.push(d); added++; }
    const last = Math.max(...arr.map((d) => Number(d.id) || 0));
    if (!added || arr.length < 100 || last <= after) break; // after в API Pointercrate — это id демона
    after = last;
  }
  return out;
}

async function loadLeaderboard() {
  const out = [];
  for (let offset = 0; ; ) {
    const d = await getJson(`${DL}leaderboard/user/list?limit=50&offset=${offset}`);
    const arr = d?.data?.users || [];
    if (!arr.length) break;
    out.push(...arr);
    offset += arr.length;
  }
  return out;
}

// Одна страница записей игрока на Demonlist (список отсортирован по id по убыванию)
async function fetchDlPage(userId, offset) {
  const d = await getJson(`${DL}user/record/list?user_id=${userId}&limit=50&offset=${offset}`);
  return { records: d?.data?.records || [], total: Number(d?.data?.total_count ?? 0) };
}

const DAY = 24 * 3600 * 1000;
const isPending = (st) => !!st && !["accepted", "rejected"].includes(String(st).toLowerCase());

// Записи игрока с кешем. Новая запись (в том числе улучшенный процент на том же уровне) получает
// новый, больший id, а список идёт по убыванию id — поэтому достаточно прочитать верхние страницы,
// пока не дойдём до уже известных id. Ещё не принятые записи (waiting) перечитываются всегда:
// их статус меняется без смены id. Целостность проверяем по total_count; если не сходится,
// порядок не по убыванию или кеш старше суток — качаем всё заново.
// Возвращает { entry, records } — новый кеш и записи в виде, который понимает playerDelta.
export async function loadDlRecordsCached(userId, entry, { fetchPage = fetchDlPage, now = Date.now() } = {}) {
  const first = await fetchPage(userId, 0);
  const total = first.total;

  const build = (ids, best, pending, fullAt) => ({
    entry: { total, ids, best, pending, fullAt },
    records: Object.entries(best).map(([id, percent]) => ({ status: "accepted", percent, level: { id: Number(id) } })),
  });

  const readAll = async () => {
    const all = [...first.records];
    for (let offset = all.length; offset < total; offset += 50) {
      const pg = await fetchPage(userId, offset);
      if (!pg.records.length) break;
      all.push(...pg.records);
    }
    const best = {}, ids = [], pending = [];
    for (const r of all) {
      ids.push(r.id);
      if (isPending(r.status)) pending.push(r.id);
      if (String(r.status || "accepted").toLowerCase() === "accepted" && r.level?.id != null) {
        best[r.level.id] = Math.max(best[r.level.id] ?? -1, Number(r.percent));
      }
    }
    return build(ids, best, pending, now);
  };

  const desc = (arr) => arr.every((r, i) => i === 0 || arr[i - 1].id > r.id);
  const maxId = entry?.ids?.length ? Math.max(...entry.ids) : 0;
  if (!entry || !desc(first.records) || now - (entry.fullAt || 0) > DAY) return readAll();

  // все записи с id >= T перечитываем: новые (id > maxId) и ещё не принятые
  const T = entry.pending?.length ? Math.min(...entry.pending) : maxId + 1;
  const fresh = [];
  let page = first, offset = 0;
  for (;;) {
    if (!desc(page.records)) return readAll();
    for (const r of page.records) if (r.id >= T) fresh.push(r);
    const last = page.records[page.records.length - 1];
    offset += page.records.length;
    if (!last || last.id < T || offset >= total || page.records.length < 50) break;
    page = await fetchPage(userId, offset);
  }

  const oldIds = entry.ids.filter((id) => id < T);
  if (oldIds.length + fresh.length !== total) return readAll(); // что-то удалили или пропустили

  const best = { ...entry.best };
  const pending = [];
  for (const r of fresh) {
    if (isPending(r.status)) pending.push(r.id);
    if (String(r.status || "accepted").toLowerCase() === "accepted" && r.level?.id != null) {
      best[r.level.id] = Math.max(best[r.level.id] ?? -1, Number(r.percent));
    }
  }
  return build([...oldIds, ...fresh.map((r) => r.id)], best, pending, entry.fullAt);
}

async function loadPcRecords(pcId) {
  try {
    const d = await getJson(`${PC}v1/players/${pcId}/`);
    return (d?.data?.records || []).filter((r) => !r.status || String(r.status).toUpperCase() === "APPROVED");
  } catch (e) {
    console.warn(`Pointercrate ${pcId}: ${e.message}`);
    return null; // не удалось — игрока лучше не трогать, чем посчитать неверно
  }
}

/* ---------- расчёт ---------- */
// Разница очков одного игрока от добавления записей Pointercrate.
// dlRecords — записи Demonlist, pcRecords — записи Pointercrate (с нескольких аккаунтов),
// levels — Map "id уровня" и Map "ingame_id" -> уровень Demonlist, demons — Map "id демона" -> level_id.
export function playerDelta({ dlRecords, pcRecords, byIngame, byId, demons, H }) {
  const best = new Map(); // id уровня на Demonlist -> { percent, level }
  for (const r of dlRecords) {
    if (r.status && String(r.status).toLowerCase() !== "accepted") continue;
    const id = r.level?.id;
    const lvl = byId.get(id);
    if (id == null || !lvl) continue;
    const pct = Number(r.percent);
    if (Number.isFinite(pct) && pct > (best.get(id)?.percent ?? -1)) best.set(id, { percent: pct, level: lvl });
  }
  const pcBest = new Map();
  for (const r of pcRecords) {
    const ingame = demons.get(r.demon?.id);
    const lvl = ingame != null ? byIngame.get(String(ingame)) : null;
    const pct = Number(r.progress);
    if (!lvl || !Number.isFinite(pct)) continue;
    if (pct > (pcBest.get(lvl.id)?.percent ?? -1)) pcBest.set(lvl.id, { percent: pct, level: lvl });
  }
  let delta = 0;
  for (const [id, pc] of pcBest) {
    const old = best.get(id);
    if (old && old.percent >= pc.percent) continue; // запись Demonlist не хуже — ничего не меняется
    const np = recordPoints(pc.level.placement, H, pc.percent, pc.level.list_percent);
    const op = old ? recordPoints(pc.level.placement, H, old.percent, pc.level.list_percent) : 0;
    if (np == null || op == null) continue;
    delta += np - op;
  }
  return delta;
}

// Итоговый порядок: по очкам (убывание); при равных — выше тот, кто был выше на Demonlist.
export function rankUsers(users) {
  const cmp = (a, b) => (a === b ? 0 : a < b ? -1 : 1);
  return [...users].sort(
    (a, b) => b.points - a.points || cmp(a.placement, b.placement) || cmp(Number(a.id), Number(b.id))
  );
}

async function main() {
  const links = JSON.parse(await readFile(new URL("../links.json", import.meta.url), "utf8"));
  let fix = { merges: [] };
  try { fix = JSON.parse(await readFile(new URL("../fix.json", import.meta.url), "utf8")); } catch (_) {}

  const absorb = new Map();
  for (const m of fix.merges || []) for (const a of m.absorb || []) absorb.set(String(a.id), String(m.keep));
  const canon = (id) => absorb.get(String(id)) ?? String(id);

  // основной ID на Demonlist -> все аккаунты Pointercrate
  const pcByUser = new Map();
  for (const [mainId, entry] of Object.entries(links)) {
    let sites = entry;
    if (typeof entry !== "object" || Array.isArray(entry) || entry === null) sites = { pointercrate: [].concat(entry) };
    const ids = [].concat(sites.pointercrate || []);
    if (!ids.length) continue;
    const k = canon(mainId);
    pcByUser.set(k, [...new Set([...(pcByUser.get(k) || []), ...ids.map(String)])]);
  }
  console.log(`Игроков со связями: ${pcByUser.size}`);

  const [levels, demonsArr, board] = await Promise.all([loadAllLevels(), loadAllPcDemons(), loadLeaderboard()]);
  const H = Math.max(...levels.map((l) => Number(l.placement) || 0));
  console.log(`Уровней: ${levels.length}, H=${H}, демонов PC: ${demonsArr.length}, игроков: ${board.length}`);

  const byIngame = new Map(levels.map((l) => [String(l.ingame_id), l]));
  const byId = new Map(levels.map((l) => [l.id, l]));
  const demons = new Map(demonsArr.map((d) => [d.id, d.level_id]));

  const users = board.map((u, i) => ({ id: String(u.id), placement: Number(u.placement) || i + 1, base: Number(u.points), points: Number(u.points) }));
  const byUserId = new Map(users.map((u) => [u.id, u]));
  // Игроков с 0 очков в таблице нет, но записи Pointercrate могут дать им очки и место в рейтинге
  // (на глобале дают очки и низкие места). Добавляем их с нулевой базой, ниже всех при равенстве.
  let unranked = 0;
  for (const id of pcByUser.keys()) {
    if (!byUserId.has(id)) {
      const u = { id, placement: Infinity, base: 0, points: 0 };
      users.push(u);
      byUserId.set(id, u);
      unranked++;
    }
  }
  if (unranked) console.log(`Игроков со связями без очков на Demonlist: ${unranked}`);

  // кеш записей Demonlist (cache.json); FULL=1 — игнорировать его и перекачать всё
  let cache = { users: {} };
  if (!process.env.FULL) {
    try { cache = JSON.parse(await readFile(new URL("../cache.json", import.meta.url), "utf8")); } catch (_) {}
  }
  cache.users ||= {};
  const newCache = { users: {} };

  const targets = [...pcByUser.entries()].filter(([id]) => byUserId.has(id));
  let done = 0, failed = 0;
  await pool(targets, CONC, async ([dlId, pcIds]) => {
    try {
      const pcLists = await Promise.all(pcIds.map(loadPcRecords));
      if (pcLists.some((l) => l === null)) { failed++; return; }
      const { entry, records: dlRecords } = await loadDlRecordsCached(dlId, cache.users[dlId]);
      newCache.users[dlId] = entry;
      const delta = playerDelta({ dlRecords, pcRecords: pcLists.flat(), byIngame, byId, demons, H });
      byUserId.get(dlId).points = Math.round((byUserId.get(dlId).base + delta) * 100) / 100;
    } catch (e) {
      failed++;
      console.warn(`Игрок ${dlId}: ${e.message}`);
    }
    if (++done % 25 === 0) console.log(`  обработано ${done}/${targets.length}`);
  });
  console.log(`Готово: ${done}, ошибок: ${failed}`);

  // игроков, которых не удалось обработать, оставляем со старым кешем
  for (const [id, e] of Object.entries(cache.users)) if (!newCache.users[id] && pcByUser.has(id)) newCache.users[id] = e;
  await writeFile(new URL("../cache.json", import.meta.url), JSON.stringify(newCache));

  const ranked = rankUsers(
    users.map((u) => ({ ...u, points: Math.round(u.points * 100) / 100 })).filter((u) => u.points > 0)
  );
  const out = {
    generated: new Date().toISOString(),
    lowest: H,
    users: ranked.map((u) => [u.id, u.points]), // порядок в массиве и есть итоговое место
  };
  await writeFile(new URL("../points.json", import.meta.url), JSON.stringify(out));
  console.log(`points.json: ${out.users.length} игроков`);
}

if (import.meta.url === `file://${process.argv[1]}`) main().catch((e) => { console.error(e); process.exit(1); });
