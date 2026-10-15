/* GBP/JPY Tagesplan – liest data/dashboard.json, data/live.json, data/archive.json, data/backtest.json */
"use strict";

const FLAGGE = { GBP: "🇬🇧", JPY: "🇯🇵", USD: "🇺🇸" };
const TREND = { 1: "Aufwärtstrend", "-1": "Abwärtstrend", 0: "kein klarer Trend" };
const NEWS = { "-2": "klar belastend", "-1": "leicht belastend", 0: "neutral", 1: "leicht stützend", 2: "klar stützend" };
const S = { dash: null, live: null, archiv: null, bt: null, newsFilter: "ALL", offen: null, trOffen: null };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const zahl = (v, d = 3) => (v == null || isNaN(v) ? "–" : Number(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d }));
const vz = (v, d = 0, suf = "") => (v == null || isNaN(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d }) + suf);
const uhr = (d) => d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Berlin" });
const berlinTag = (d) => d.toLocaleDateString("sv-SE", { timeZone: "Europe/Berlin" });
const datumLang = (iso) => { const [y, m, d] = iso.split("-"); return `${d}.${m}.${y}`; };
const wochentag = (iso) => ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"][new Date(iso + "T12:00:00").getDay()];
const PIP = 0.01;

async function lade(name) {
  const r = await fetch(`data/${name}?t=${Date.now()}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${name}: ${r.status}`);
  return r.json();
}
const livePreis = () => S.live?.pairs?.gbpjpy?.price ?? S.dash?.report?.price ?? null;
/* Minuten seit FX-Tagesbeginn (23:00 Berlin) -> Zeitstempel */
const fxZeit = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return S.dash.fx_window[0] + ((h * 60 + m - 23 * 60 + 1440) % 1440) * 60; };

/* ---------------- Tooltip ---------------- */
const tip = $("tip");
function zeigeTip(x, y, html) {
  tip.innerHTML = html; tip.hidden = false;
  tip.style.left = Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, x + 12)) + "px";
  tip.style.top = Math.max(8, y - tip.offsetHeight - 12) + "px";
}
const versteckeTip = () => { tip.hidden = true; };

/* ---------------- Kopf ---------------- */
function renderKopf() {
  const p = livePreis(), vor = S.dash?.prev_close?.gbpjpy?.close;
  $("k-preis").textContent = zahl(p);
  const ch = p && vor ? (p / vor - 1) * 100 : null;
  const el = $("k-chg");
  el.className = "chg " + (ch == null ? "flach" : ch > 0 ? "auf" : ch < 0 ? "ab" : "flach");
  el.textContent = ch == null ? "" : `${ch > 0 ? "▲" : ch < 0 ? "▼" : ""} ${vz(ch, 2, " %")}`;
  const live = S.live, jetzt = Date.now() / 1000;
  let t;
  if (!live) t = "Live-Kurse fehlen";
  else if (live.market_closed) t = `<span class="punkt alt"></span>Markt geschlossen`;
  else {
    const alt = jetzt - live.updated;
    t = alt > 180 ? `<span class="punkt alt"></span>Live-Kurse ${Math.round(alt / 60)} Min alt` : `<span class="punkt"></span>live ${uhr(new Date(live.updated * 1000))}`;
  }
  $("stand").innerHTML = t;
}

/* ---------------- Order-Zettel ---------------- */
function statusPill(su) {
  const p = livePreis(), o = su.orders?.[0];
  const st = su.state;
  if (st === "tp" || st === "sl" || st === "zeit") {
    const tr = su.trade || {};
    const k = st === "tp" ? "tp" : st === "sl" ? "sl" : "neutral";
    const txt = st === "tp" ? "Ziel erreicht" : st === "sl" ? "Stop erreicht" : "zum Tagesende geschlossen";
    return `<span class="status ${k}">${txt} · ${vz(tr.pips, 0, " Pips")} um ${esc(tr.t_exit || "")}</span>`;
  }
  if (st === "verfallen") return `<span class="status neutral">Verfallen – kein Ausbruch bis ${esc(su.ende)}</span>`;
  if (st === "ausgelöst") {
    const tr = su.trade || {};
    const pl = p && tr.entry ? (tr.richtung * (p - tr.entry)) / PIP : null;
    return `<span class="status laeuft">Läuft seit ${esc(tr.t_entry || "")} · ${vz(pl, 0, " Pips")}</span>`;
  }
  if (!o || !p) return `<span class="status wartet">Wartet auf Ausbruch</span>`;
  const abstand = Math.min(...su.orders.map((x) => Math.abs(x.entry - p) / PIP));
  return `<span class="status wartet">Wartet · noch ${zahl(abstand, 0)} Pips bis Entry</span>`;
}
function renderTicket() {
  const el = $("ticket"), su = S.dash?.setup, r = S.dash?.report, h = S.dash?.setup_historie?.setups?.london_trend?.gesamt;
  const histZeile = h ? `<div>Backtest 2012–22: ${h.trefferquote.toLocaleString("de-DE")} % Treffer, Ø ${vz(h.avg_pips, 1)} Pips je Trade nach Kosten, längste Verlustserie ${h.max_verlustserie}</div>` : "";
  if (!su) {
    const vor = (r?.vorschau || "").split("\n").filter(Boolean);
    el.innerHTML = `<div class="t-kopf"><div class="t-art">London-Ausbruch in Trendrichtung</div>
      <div class="t-richtung keins">Setup um 09:00</div>
      <span class="status neutral">Asien-Spanne bildet sich noch</span></div>
      <div class="grund">${vor.length ? vor.map((z) => `<p>${esc(z)}</p>`).join("") : "<p>Vorschau kommt mit dem Morgenreport um 06:45.</p>"}</div>
      <div class="t-fuss">${histZeile}</div>`;
    return;
  }
  if (su.status !== "aktiv") {
    el.innerHTML = `<div class="t-kopf"><div class="t-art">${esc(su.name)}</div><div class="t-richtung keins">Heute kein Setup</div>
      <span class="status neutral">${esc(TREND[su.trend] || "")}</span></div>
      <div class="grund"><p>${esc(su.grund)}.</p>${su.spanne ? `<p>Asien-Spanne ${zahl(su.spanne[0])} – ${zahl(su.spanne[1])}</p>` : ""}
      <p>Die Regel handelt nur, wenn Trend und Spanne passen. An solchen Tagen gab es im Backtest keinen Vorteil.</p></div>
      <div class="t-fuss">${histZeile}</div>`;
    return;
  }
  const o = (su.trade && su.state !== "wartet") ? su.orders.find((x) => x.richtung === su.trade.richtung) || su.orders[0] : su.orders[0];
  const long = o.richtung > 0;
  const zeilen = [["Entry", "var(--entry)", o.entry, long ? "Kauf-Stop" : "Verkauf-Stop"],
    ["Stop", "var(--stop)", o.sl, `−${o.risiko_pips} Pips`], ["Ziel", "var(--ziel)", o.tp, `+${o.ziel_pips} Pips`]];
  el.innerHTML = `<div class="t-kopf"><div class="t-art">${esc(su.name)} · ${esc(TREND[su.trend])}</div>
      <div class="t-richtung ${long ? "long" : "short"}">${long ? "LONG" : "SHORT"} ${long ? "über" : "unter"} <span class="num">${zahl(o.entry)}</span></div>
      ${statusPill(su)}</div>
    ${zeilen.map(([n, f, p, d]) => `<div class="lv"><span class="n"><i style="background:${f}"></i>${n}</span><span class="p">${zahl(p)}</span><span class="d">${d}</span></div>`).join("")}
    <div class="t-fuss"><div>Chance zu Risiko <b>1 : 2</b> · Entry gültig bis <b>${esc(su.ende)}</b> · spätestens <b>${esc(su.schluss)}</b> schließen</div>
      ${su.spanne ? `<div>Asien-Spanne ${zahl(su.spanne[0])} – ${zahl(su.spanne[1])} (${zahl(su.info?.breite_pips, 0)} Pips)</div>` : ""}
      ${histZeile}<div>Positionsgröße nach deinem Risiko. Keine Anlageberatung.</div></div>`;
}

/* ---------------- Chart mit allen Marken ---------------- */
function renderChart() {
  const el = $("chart"), d = S.dash, r = d?.report, su = d?.setup;
  if (!d) return;
  const [x0, x1] = d.fx_window;
  const ohlc = (S.live?.pairs?.gbpjpy?.ohlc || []).filter((b) => b[0] >= x0 && b[0] <= x1);
  const punkte = ohlc.length ? ohlc.map((b) => [b[0], b[4]]) : (S.live?.pairs?.gbpjpy?.bars || []).filter((b) => b[0] >= x0);
  const W = el.clientWidth || 640, H = W < 520 ? 300 : 340, pad = { l: 6, r: 74, t: 12, b: 24 };
  const schienen = [];
  const o = su?.status === "aktiv" ? su.orders : [];
  for (const x of o) {
    schienen.push({ y: x.entry, f: "var(--entry)", t: "Entry", strich: "6 4" });
    schienen.push({ y: x.sl, f: "var(--stop)", t: "Stop" });
    schienen.push({ y: x.tp, f: "var(--ziel)", t: "Ziel" });
  }
  if (r?.exp?.[0]) { schienen.push({ y: r.exp[0], f: "var(--leise)", t: "erw. Hoch", strich: "2 4", leise: true }); schienen.push({ y: r.exp[1], f: "var(--leise)", t: "erw. Tief", strich: "2 4", leise: true }); }
  if (r?.levels?.[0]) { schienen.push({ y: r.levels[0], f: "var(--text-2)", t: "Vortageshoch", strich: "1 3", leise: true }); schienen.push({ y: r.levels[1], f: "var(--text-2)", t: "Vortagestief", strich: "1 3", leise: true }); }
  const ys = punkte.map((p) => p[1]).concat(schienen.map((s) => s.y)).concat(r?.range?.[0] ? r.range : []).concat(su?.spanne || []);
  if (!ys.length) { el.innerHTML = `<p class="leer">Noch keine Kurse für den heutigen Handelstag.</p>`; return; }
  let y0 = Math.min(...ys), y1 = Math.max(...ys); const sp = (y1 - y0) || 0.5; y0 -= sp * 0.06; y1 += sp * 0.06;
  const X = (v) => pad.l + ((v - x0) / (x1 - x0)) * (W - pad.l - pad.r);
  const Y = (v) => pad.t + (1 - (v - y0) / (y1 - y0)) * (H - pad.t - pad.b);
  let s = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="GBP/JPY heute mit Setup-Marken">`;
  // Prognose-Spanne (68 %)
  if (r?.range?.[0]) s += `<rect x="${pad.l}" y="${Y(r.range[1])}" width="${W - pad.l - pad.r}" height="${Y(r.range[0]) - Y(r.range[1])}" fill="var(--band)"/>`;
  // Entry-Fenster und Asien-Spanne
  if (su) {
    const e0 = fxZeit(su.start), e1 = fxZeit(su.ende);
    s += `<rect x="${X(e0)}" y="${pad.t}" width="${X(e1) - X(e0)}" height="${H - pad.t - pad.b}" fill="rgba(242,184,75,0.05)"/>`;
    if (su.spanne) {
      const a0 = fxZeit("01:00");
      s += `<rect x="${X(a0)}" y="${Y(su.spanne[1])}" width="${X(e0) - X(a0)}" height="${Y(su.spanne[0]) - Y(su.spanne[1])}" fill="var(--asien)" stroke="rgba(242,184,75,0.5)" stroke-width="1"/>`;
    }
  }
  // Zeitachse
  const schritt = W < 520 ? 6 * 3600 : 3 * 3600;
  for (let t = Math.ceil(x0 / schritt) * schritt; t <= x1; t += schritt) {
    s += `<line x1="${X(t)}" x2="${X(t)}" y1="${pad.t}" y2="${H - pad.b}" stroke="var(--raster)"/>`;
    if (X(t) > 18 && X(t) < W - pad.r - 14) s += `<text x="${X(t)}" y="${H - 7}" text-anchor="middle">${uhr(new Date(t * 1000))}</text>`;
  }
  // Schienen mit Preisschildern rechts (Überlappung vermeiden)
  const belegt = [], innen = [];
  for (const sch of schienen.sort((a, b) => (a.leise ? 1 : 0) - (b.leise ? 1 : 0))) {
    const y = Y(sch.y);
    s += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}" stroke="${sch.f}" stroke-width="${sch.leise ? 1 : 1.6}" stroke-dasharray="${sch.strich || ""}"/>`;
    if (sch.leise) {
      // Beschriftung mit Zahl innen am rechten Rand, Überlappungen nach oben/unten ausweichen
      let ty = y - 4;
      for (let i = 0; i < 4 && innen.some((b) => Math.abs(b - ty) < 13); i++) ty += sch.t.includes("Tief") || sch.t.includes("tief") ? 13 : -13;
      innen.push(ty);
      s += `<text x="${W - pad.r - 4}" y="${ty}" text-anchor="end" style="fill:${sch.f === "var(--leise)" ? "#a3b0c4" : "var(--text-2)"};paint-order:stroke;stroke:var(--flaeche);stroke-width:4px;stroke-linejoin:round">${esc(sch.t)} ${zahl(sch.y)}</text>`;
      continue;
    }
    if (belegt.some((b) => Math.abs(b - y) < 15)) continue;
    belegt.push(y);
    s += `<rect x="${W - pad.r + 2}" y="${y - 9}" width="${pad.r - 4}" height="18" rx="3" fill="${sch.f}"/><text x="${W - pad.r + 6}" y="${y + 4}" style="fill:#0e1420;font-weight:600">${zahl(sch.y)}</text>`;
  }
  // Kursverlauf
  if (punkte.length > 1) {
    s += `<path d="${punkte.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("")}" fill="none" stroke="var(--kurs)" stroke-width="2" stroke-linejoin="round"/>`;
    const l = punkte[punkte.length - 1];
    s += `<circle cx="${X(l[0])}" cy="${Y(l[1])}" r="4" fill="var(--kurs)" stroke="var(--flaeche)" stroke-width="2"/>`;
  }
  // Entry/Exit-Punkte des Trades
  const tr = su?.trade;
  if (tr?.t_entry) s += `<circle cx="${X(fxZeit(tr.t_entry))}" cy="${Y(tr.entry)}" r="5" fill="var(--entry)" stroke="var(--flaeche)" stroke-width="2"/>`;
  if (tr?.t_exit && tr.exit) s += `<circle cx="${X(fxZeit(tr.t_exit))}" cy="${Y(tr.exit)}" r="5" fill="${tr.grund === "tp" ? "var(--ziel)" : tr.grund === "sl" ? "var(--stop)" : "var(--text-2)"}" stroke="var(--flaeche)" stroke-width="2"/>`;
  s += `<line class="cross" y1="${pad.t}" y2="${H - pad.b}" stroke="var(--text-2)" visibility="hidden"/><rect class="hit" x="${pad.l}" y="0" width="${W - pad.l - pad.r}" height="${H}" fill="transparent"/></svg>`;
  el.innerHTML = s;
  const cross = el.querySelector(".cross");
  const bewegen = (ev) => {
    if (!punkte.length) return;
    const pt = ev.touches ? ev.touches[0] : ev, rc = el.querySelector("svg").getBoundingClientRect();
    const xv = x0 + (((pt.clientX - rc.left) * (W / rc.width) - pad.l) / (W - pad.l - pad.r)) * (x1 - x0);
    let b = punkte[0]; for (const p of punkte) if (Math.abs(p[0] - xv) < Math.abs(b[0] - xv)) b = p;
    cross.setAttribute("x1", X(b[0])); cross.setAttribute("x2", X(b[0])); cross.setAttribute("visibility", "visible");
    zeigeTip(pt.clientX, pt.clientY, `${uhr(new Date(b[0] * 1000))} · <b>${zahl(b[1])}</b>`);
  };
  const weg = () => { cross.setAttribute("visibility", "hidden"); versteckeTip(); };
  const hit = el.querySelector(".hit");
  hit.addEventListener("mousemove", bewegen); hit.addEventListener("mouseleave", weg);
  hit.addEventListener("touchstart", bewegen, { passive: true }); hit.addEventListener("touchmove", bewegen, { passive: true }); hit.addEventListener("touchend", weg);
  $("legende").innerHTML = `<span><i style="background:var(--kurs)"></i>Kurs</span>` +
    (o.length ? `<span><i style="background:var(--entry)"></i>Entry</span><span><i style="background:var(--stop)"></i>Stop</span><span><i style="background:var(--ziel)"></i>Ziel</span>` : "") +
    (su?.spanne ? `<span><i class="kasten" style="background:var(--asien);border:1px solid rgba(242,184,75,.5)"></i>Asien-Spanne</span>` : "") +
    (r?.range?.[0] ? `<span><i class="kasten" style="background:rgba(127,178,255,.25)"></i>Schluss-Spanne 68 %</span>` : "");
}

/* ---------------- Treiber und Empfehlung ---------------- */
const RISIKO = { normal: "Risiko normal", halbiert: "Risiko halbieren", "kein neues Risiko": "Kein neues Risiko" };
const standText = (s) => (!s ? "–" : s.length > 10 ? uhr(new Date(s)) : datumLang(s).slice(0, 6));
const minus = (s) => esc(s).replace(/(^|[\s(])-(?=\d)/g, "$1−");   /* typografisches Minus in Server-Texten */
const tage = (n) => `${n} ${n === 1 ? "Tag" : "Tage"}`;
function treiberPlan(code, r) {
  if (!r?.exp?.[0] || !r?.levels?.[0]) return "";
  const [eh, et] = r.exp, [vh, vt] = r.levels, b = (v) => `<b>${zahl(v)}</b>`;
  if (code === "short" || code === "leicht_short")
    return `Shorts eher bei Rücklauf an ${b(eh)} (erw. Hoch) oder unter ${b(vt)} (Vortagestief). Ziel-Zone ${b(et)} (erw. Tief). Über ${b(vh)} (Vortageshoch) kippt das Bild.`;
  if (code === "long" || code === "leicht_long")
    return `Longs eher bei Rücklauf an ${b(et)} (erw. Tief) oder über ${b(vh)} (Vortageshoch). Ziel-Zone ${b(eh)} (erw. Hoch). Unter ${b(vt)} (Vortagestief) kippt das Bild.`;
  if (code === "long_meiden") return `Keine Longs. Shorts erst unter ${b(vt)} (Vortagestief), Ziel-Zone ${b(et)} (erw. Tief).`;
  return `Marken heute: erw. Hoch ${b(eh)} · erw. Tief ${b(et)} · Vortag ${b(vh)} / ${b(vt)}`;
}
/* Richtung aus der Treiber-Empfehlung (identisch mit WhatsApp), Trend und News als Gegencheck */
function richtungVon(t) {
  const st = t?.stufe ?? 0;
  if (t?.score == null) return { kl: "neutral", wort: "Keine Daten", stufe: "" };
  if (st >= 2) return { kl: "bull", wort: "Bullish", stufe: "klar" };
  if (st === 1) return { kl: "bull", wort: "Bullish", stufe: "leicht" };
  if (st <= -2) return { kl: "bear", wort: "Bearish", stufe: "klar" };
  if (st === -1) return { kl: "bear", wort: "Bearish", stufe: "leicht" };
  return { kl: "neutral", wort: "Neutral", stufe: "" };
}
function signalChip(name, wert) {
  const kl = wert > 0 ? "bull" : wert < 0 ? "bear" : "neutral";
  const txt = wert > 0 ? "bullish" : wert < 0 ? "bearish" : "neutral";
  return `<span class="chip-s ${kl}"><b>${wert > 0 ? "▲" : wert < 0 ? "▼" : "–"}</b>${name} ${txt}</span>`;
}
function renderUrteil() {
  const t = S.dash?.treiber, r = S.dash?.report, el = $("urteil");
  if (!t) { el.className = "urteil neutral"; el.innerHTML = `<div class="u-wort">Lädt …</div>`; $("k-urteil").textContent = ""; return; }
  const e = t.empfehlung || {}, ri = richtungVon(t);
  const items = (t.items || []).filter((i) => i.beitrag != null);
  const druecken = items.filter((i) => i.beitrag <= -1), stuetzen = items.filter((i) => i.beitrag >= 1);
  const summeAb = druecken.reduce((a, i) => a - i.beitrag, 0), summeAuf = stuetzen.reduce((a, i) => a + i.beitrag, 0);
  const gesamt = summeAb + summeAuf || 1;
  const warn = e.code === "long_meiden" || e.code === "abwarten";
  el.className = `urteil ${ri.kl}${warn ? " warnung" : ""}`;
  const k = $("k-urteil");
  k.className = `k-urteil ${ri.kl}`;
  k.textContent = t.score == null ? "" : `${ri.kl === "bull" ? "▲" : ri.kl === "bear" ? "▼" : "–"} ${ri.wort}`;
  const trend = r?.trend ?? 0, news = r?.news_score ?? 0;
  const pfeil = ri.kl === "bull" ? "▲" : ri.kl === "bear" ? "▼" : "◆";
  el.innerHTML = `
    <div class="u-oben">
      <div class="u-wort"><span class="u-pfeil" aria-hidden="true">${pfeil}</span>${esc(ri.wort)}${ri.stufe ? `<span class="u-stufe">${ri.stufe}</span>` : ""}</div>
      <div class="u-score"><span class="num">${t.score == null ? "–" : vz(t.score)}</span><span>Score von −100 bis +100</span></div>
    </div>
    ${warn ? `<div class="u-warn">${esc(e.titel)}: ${esc(e.text)}</div>` : `<p class="u-text">${esc(e.text || "")} <span class="u-risiko">${esc(RISIKO[e.risiko] || "")}</span></p>`}
    <div class="u-zug" role="img" aria-label="${druecken.length} Treiber drücken, ${stuetzen.length} stützen">
      <span class="ab" style="flex:${summeAb / gesamt}"></span><span class="auf" style="flex:${summeAuf / gesamt}"></span>
    </div>
    <div class="u-zug-l"><span>▼ ${druecken.length} drücken${druecken[0] ? `: ${esc(druecken.sort((a, b) => a.beitrag - b.beitrag).slice(0, 2).map((i) => i.kurz).join(", "))}` : ""}</span>
      <span>${stuetzen[0] ? `${esc(stuetzen.sort((a, b) => b.beitrag - a.beitrag).slice(0, 2).map((i) => i.kurz).join(", "))}: ` : ""}${stuetzen.length} stützen ▲</span></div>
    <div class="u-chips">${signalChip("Treiber", Math.sign(t.stufe || 0))}${signalChip("Trend", trend)}${signalChip("News", news)}
      ${t.termin_hinweis ? `<span class="chip-s zeit">⏰ ${esc(t.termin_hinweis)}</span>` : ""}</div>`;
}
function renderTreiber() {
  const t = S.dash?.treiber, r = S.dash?.report;
  if (!t) { $("tr-liste").innerHTML = ""; $("tr-plan").innerHTML = ""; $("tr-stand").textContent = ""; return; }
  const e = t.empfehlung || {};
  $("tr-stand").textContent = `${t.abdeckung.n} von ${t.abdeckung.von} verfügbar, Stand ${uhr(new Date(t.stand))}`;
  const hinweise = (e.hinweise || []).map((h) => esc(h)).join(" ");
  const plan = treiberPlan(e.code, r);
  $("tr-plan").innerHTML = [plan, hinweise].filter(Boolean).join("<br>");
  $("tr-liste").innerHTML = t.items.map((i) => {
    const b = i.beitrag, auf = S.trOffen === i.id;
    const kl = i.signal == null ? "fehlt" : i.wirkung === "stützt" ? "auf" : i.wirkung === "drückt" ? "ab" : "flach";
    const pfeil = { fehlt: "·", auf: "▲", ab: "▼", flach: "–" }[kl];
    const breite = b == null ? 0 : Math.min(50, (Math.abs(b) / 20) * 50);
    return `<div class="tr-zeile${auf ? " offen" : ""}${i.signal == null ? " leise" : ""}">
      <button class="tr-kopf" data-id="${esc(i.id)}" aria-expanded="${auf}">
        <span class="tr-pfeil ${kl}" aria-label="${esc(i.wirkung || "keine Daten")}">${pfeil}</span>
        <span class="tr-name">${esc(i.name)}<small>${esc(i.text)}</small></span>
        <span class="tr-wert num">${minus(i.wert_text)}<small>${minus(i.aenderung_text || "")}</small></span>
        <span class="tr-balken" title="Beitrag zum Score: ${b == null ? "–" : vz(b, 1)} von max. ±${i.gewicht}"><i class="${b > 0 ? "pos" : "neg"}" style="width:${breite}%;left:${b > 0 ? 50 : 50 - breite}%"></i></span>
      </button>
      ${auf ? `<div class="tr-mehr"><p>${esc(i.warum)}</p>
        <div class="tr-meta"><span>Gewicht ${i.gewicht}</span><span>Beitrag ${b == null ? "–" : vz(b, 1)}</span><span>${esc(i.quelle)}</span><span>${esc(i.takt)}</span><span>Stand ${esc(standText(i.stand))}</span></div>
        ${i.id === "intervention" && i.warnungen?.length ? `<ul class="tr-warn">${i.warnungen.map((w) => `<li>${esc(w.title)} <span>${esc(w.source)}</span></li>`).join("")}</ul>` : ""}</div>` : ""}
    </div>`;
  }).join("");
  $("tr-liste").querySelectorAll(".tr-kopf").forEach((k) => k.addEventListener("click", () => { S.trOffen = S.trOffen === k.dataset.id ? null : k.dataset.id; renderTreiber(); }));
}

/* ---------------- Studie: was GBP/JPY bewegt hat ---------------- */
function renderStudie() {
  const s = S.dash?.treiber_studie;
  if (!s) { $("studie").innerHTML = `<p class="leer">Studie nicht verfügbar.</p>`; return; }
  const pc = (v, d = 2) => `${zahl(v, d)} %`;
  $("sum-studie").textContent = `${s.zerlegung.yen_pct} % kommen über den Yen · BoJ-Tage ${zahl(s.termine[0]?.faktor, 1)}× Bewegung`;
  let h = `<div class="kpis">${kpi(pc(s.zerlegung.yen_pct, 0), "der Schwankung über den Yen", `Pfund: ${s.zerlegung.pfund_pct} %`)}
    ${kpi(pc(s.basis.avg_abs), "Ø Tagesbewegung", `Median ${pc(s.basis.median_abs)}`)}
    ${kpi(pc(s.basis.anteil_ueber_1pct, 1), "der Tage über 1 %", `${s.basis.tage_ueber_1pct} von ${s.tage} Tagen`)}
    ${kpi(s.tage, "Handelstage", `${datumLang(s.zeitraum[0])} – ${datumLang(s.zeitraum[1])}`)}</div>`;
  h += `<h3>Termine: wie stark bewegen sie GBP/JPY?</h3><div class="tabbox"><table><thead><tr><th>Termin</th><th class="r">Ø Bewegung</th><th class="r">× normal</th><th class="r">über 1 %</th><th class="r">Fälle</th></tr></thead><tbody>` +
    s.termine.map((t) => `<tr><td class="wrap">${esc(t.name)}${t.notiz ? `<div class="hinweis">${esc(t.notiz)}</div>` : ""}</td><td class="r">${pc(t.avg_abs)}</td><td class="r${t.faktor >= 1.5 ? " stark" : ""}">${zahl(t.faktor, 2)}×</td><td class="r">${t.anteil_ueber_1pct} %</td><td class="r">${t.n}</td></tr>`).join("") +
    `</tbody></table></div><p class="hinweis">Ein Tag ohne große Termine bewegt GBP/JPY im Schnitt ${pc(s.basis.ruhig_avg_abs)}. Gemessen von Mittag zu Mittag (New York).</p>`;
  const maxK = Math.max(...s.kategorien.map((k) => k.anzahl));
  h += `<h3>Ursachen der 40 größten Tagesbewegungen</h3><div class="st-balken">` +
    s.kategorien.map((k) => `<div><span>${esc(k.name)}</span><i style="width:${(k.anzahl / maxK) * 100}%"></i><b>${k.anzahl}</b></div>`).join("") + `</div>`;
  const tabelle = (zeilen, kopf, datum) => `<div class="tabbox"><table><thead><tr><th>${kopf}</th><th class="r">GBP/JPY</th><th>Ursache</th></tr></thead><tbody>` +
    zeilen.map((g) => `<tr><td>${datumLang(g[datum])}</td><td class="r ${g.r > 0 ? "ok" : "nein"}">${vz(g.r, 2, " %")}</td><td class="wrap">${esc(g.ursache)}</td></tr>`).join("") + `</tbody></table></div>`;
  h += `<h3>Die 20 größten Tage</h3>${tabelle(s.grosse_tage, "Tag", "datum")}`;
  h += `<h3>Die stärksten 5-Tage-Schübe</h3>${tabelle(s.schuebe_5t, "bis", "bis")}`;
  h += `<h3>Was die Daten sagen</h3><div class="st-befunde">` + s.befunde.map((b) => `<div><b>${esc(b.titel)}</b><p>${esc(b.text)}</p></div>`).join("") + `</div>`;
  h += `<p class="hinweis">${esc(s.daten)}.</p>`;
  $("studie").innerHTML = h;
}

/* ---------------- Tag auf einen Blick ---------------- */
function renderBlick() {
  const r = S.dash?.report, v = S.dash?.prev_close?.gbpjpy;
  const zelle = (l, w, h) => `<div><div class="l">${l}</div><div class="w">${w}</div><div class="h">${h}</div></div>`;
  if (!r) { $("blick").innerHTML = zelle("Prognose", "kommt um 06:45", "") + zelle("Erwartetes Hoch / Tief", "–", "") + zelle("Vortag Hoch / Tief", v ? `${zahl(v.high)} / ${zahl(v.low)}` : "–", ""); return; }
  $("blick").innerHTML =
    zelle("Schluss mit 68 %", `${zahl(r.range[0])} – ${zahl(r.range[1])}`, `Tagesschwankung ATR ${zahl(r.atr_pips, 0)} Pips`) +
    zelle("Erwartetes Hoch / Tief", `${zahl(r.exp[0])} / ${zahl(r.exp[1])}`, "wird an jedem zweiten Tag erreicht") +
    zelle("Vortag Hoch / Tief", `${zahl(r.levels[0])} / ${zahl(r.levels[1])}`, "Ausbruch darüber/darunter = Richtungswechsel");
}

/* ---------------- Termine ---------------- */
function renderTermine() {
  const heute = berlinTag(new Date()), jetzt = new Date();
  const liste = (S.dash?.calendar || []).filter((t) => berlinTag(new Date(t.ts)) === heute);
  $("termine").innerHTML = liste.length ? liste.map((t) => {
    const d = new Date(t.ts), auf = S.offen === t.id;
    return `<div class="termin${d < jetzt ? " vorbei" : ""}" data-id="${t.id}" role="button" tabindex="0" aria-expanded="${auf}">
      <span class="z">${uhr(d)}</span><span class="imp ${t.impact}" title="${t.impact === "high" ? "hoher" : "mittlerer"} Einfluss"></span>
      <span>${FLAGGE[t.currency] || ""} ${esc(t.title)}${t.faktor ? `<span class="fak${t.faktor >= 1.5 ? " hoch" : ""}" title="So viel Bewegung wie ein normaler Tag (Studie 2023–2026)">${zahl(t.faktor, 1)}×</span>` : ""}</span>
      ${auf ? `<div class="det"><span>Forecast ${esc(t.forecast || "–")}</span><span>Previous ${esc(t.previous || "–")}</span><span>Actual ${esc(t.actual || "n/v")}</span><div>${esc(t.effect || "Wirkung kommt mit dem Morgenreport.")}</div>${t.faktor ? `<div>Seit 2023 an solchen Tagen: GBP/JPY Ø ${zahl(t.avg_abs, 2)} % Bewegung (${zahl(t.faktor, 1)}× normal), ${t.anteil_1pct} % der Tage über 1 %.</div>` : ""}</div>` : ""}</div>`;
  }).join("") : `<p class="leer" style="padding:4px 0">Heute keine wichtigen Termine für GBP, JPY oder USD.</p>`;
  $("termine").querySelectorAll(".termin").forEach((z) => {
    const f = () => { S.offen = S.offen === z.dataset.id ? null : z.dataset.id; renderTermine(); };
    z.addEventListener("click", f); z.addEventListener("keydown", (e) => { if (e.key === "Enter") f(); });
  });
  renderCountdown();
}
function renderCountdown() {
  const jetzt = new Date();
  const n = (S.dash?.calendar || []).filter((t) => t.impact === "high" && new Date(t.ts) > jetzt).sort((a, b) => a.ts.localeCompare(b.ts))[0];
  if (!n) { $("countdown").textContent = "Kein roter Termin mehr heute"; return; }
  let s = Math.round((new Date(n.ts) - jetzt) / 1000); const h = Math.floor(s / 3600); s -= h * 3600; const m = Math.floor(s / 60); s -= m * 60;
  $("countdown").innerHTML = `Nächster roter Termin in <b>${h ? h + ":" : ""}${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}</b>`;
}

/* ---------------- Lage + News ---------------- */
function renderLage() {
  const r = S.dash?.report;
  $("sum-lage").textContent = r?.news_reason || "";
  $("lage").innerHTML = r ? `<p><b>🇬🇧 GBP</b>${esc(r.lage_gbp || "–")}</p><p><b>🇯🇵 JPY</b>${esc(r.lage_jpy || "–")}</p>
    <p><b>Nachrichtenlage</b>${esc(r.reason_long || "–")}</p><p><b>Risiken</b>${esc(r.risks || "–")}</p>` +
    (r.missing?.length ? `<p class="warn">Heute fehlende Quellen: ${esc(r.missing.join(", "))}</p>` : "") : `<p>Kommt mit dem Morgenreport um 06:45.</p>`;
  const f = S.newsFilter;
  const liste = (S.dash?.news || []).filter((n) => f === "ALL" || (n.currencies || []).includes(f));
  $("news").innerHTML = liste.length ? liste.map((n) => {
    const e = n.effect || "", kl = e.endsWith("+") ? "plus" : e.endsWith("-") ? "minus" : "neutral";
    const lab = e && e !== "neutral" ? `${e.slice(0, 3)} ${e.endsWith("+") ? "positiv" : "negativ"}` : "neutral";
    return `<li><div class="k"><span>${uhr(new Date(n.ts))}</span><span>${esc(n.source)}</span>${n.effect ? `<span class="tag ${kl}">${esc(lab)}</span>` : ""}</div>
      <a href="${esc(n.url)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>${n.note ? `<div class="e">${esc(n.note)}</div>` : ""}</li>`;
  }).join("") : `<li class="leer">Keine passenden Meldungen in den letzten 24 Stunden.</li>`;
}
$("news-filter").addEventListener("click", (e) => {
  const b = e.target.closest(".chip"); if (!b) return;
  S.newsFilter = b.dataset.f; $("news-filter").querySelectorAll(".chip").forEach((c) => c.classList.toggle("an", c === b)); renderLage();
});

/* ---------------- Bilanz: Setup + Prognose ---------------- */
function kpi(w, l, v = "") { return `<div class="kpi"><div class="w">${w}</div><div class="l">${l}</div>${v ? `<div class="v">${v}</div>` : ""}</div>`; }
const pz = (x) => (x == null ? "–" : `${x.toLocaleString("de-DE")} %`);
function renderBilanz() {
  const d = S.dash; if (!d) return;
  const b = d.setup_bilanz || {}, hist = d.setup_historie, q = d.stats?.q30 || {}, bt = S.bt;
  $("sum-bilanz").textContent = b.trades ? `Setup live: ${b.trades} Trades, ${vz(b.summe_pips, 0, " Pips")}` : "Backtest und Live-Bilanz";
  let h = `<h3>Setup live (seit Start)</h3><div class="kpis">${kpi(b.trades ?? 0, "Trades", `${b.aktive_tage ?? 0} von ${b.tage ?? 0} Tagen mit Setup`)}
    ${kpi(pz(b.treffer_pct), "Gewinner")}${kpi(vz(b.summe_pips, 0, " P"), "Summe nach 3 Pips Kosten")}${kpi(vz(b.avg_pips, 1, " P"), "Ø je Trade")}</div>`;
  const liste = d.setup_liste || [];
  if (liste.length) {
    h += `<div class="tabbox"><table><thead><tr><th>Tag</th><th>Setup</th><th>Ergebnis</th><th class="r">Pips</th></tr></thead><tbody>` +
      liste.map((s) => {
        const res = s.result || {}, p = res.pips;
        const erg = s.status !== "aktiv" ? `<span class="hinweis">kein Setup</span>` : res.grund === "tp" ? `<span class="ok">Ziel</span>` : res.grund === "sl" ? `<span class="nein">Stop</span>` : res.grund === "zeit" ? "Tagesende" : res.grund === "kein_entry" ? "kein Ausbruch" : "offen";
        return `<tr><td>${wochentag(s.date)} ${datumLang(s.date).slice(0, 6)}</td><td class="wrap">${s.status === "aktiv" ? (s.trend > 0 ? "Long" : "Short") : esc(s.grund || "")}</td><td>${erg}</td><td class="r">${p == null ? "" : vz(p, 0)}</td></tr>`;
      }).join("") + "</tbody></table></div>";
  }
  if (bt?.setups?.london_trend) {
    const k = bt.setups.london_trend.kennzahlen, kb = bt.setups.london_beide?.kennzahlen;
    h += `<h3>Backtest mit aktuellen Daten (${datumLang(bt.von.slice(0, 10))} – ${datumLang(bt.bis.slice(0, 10))})</h3>
      <div class="kpis">${kpi(k.n ?? 0, "Trades")}${kpi(pz(k.trefferquote), "Gewinner")}${kpi(vz(k.avg_pips, 1, " P"), "Ø je Trade nach Kosten", kb ? `ohne Trendfilter: ${vz(kb.avg_pips, 1, " P")}` : "")}${kpi(k.profit_faktor == null ? "–" : k.profit_faktor.toLocaleString("de-DE"), "Profit-Faktor", `max. Rückgang ${k.max_drawdown_pips ?? "–"} P`)}</div>
      <p class="hinweis">Diesen Zeitraum kannte ich beim Entwickeln der Regel nicht. Er ist deshalb der ehrlichste Test. Prognose-Spanne traf hier ${pz(bt.prognose?.spanne_abdeckung_pct)} der Tagesschlüsse (Ziel 68 %).</p>`;
  }
  if (hist) {
    h += `<h3>Alle getesteten Setups, ${esc(hist.daten.split(" (")[0])}</h3><div class="tabbox"><table><thead><tr><th>Setup</th><th class="r">Trades</th><th class="r">Gewinner</th><th class="r">Ø 2012–18</th><th class="r">Ø 2019–22</th><th class="r">Profit-Faktor</th></tr></thead><tbody>` +
      Object.values(hist.setups).map((s) => `<tr${s.live ? ' style="color:var(--entry)"' : ""}><td>${esc(s.name)}${s.live ? " (aktiv)" : ""}</td><td class="r">${s.gesamt.n}</td><td class="r">${s.gesamt.trefferquote.toLocaleString("de-DE")} %</td>
        <td class="r ${s.in.avg_pips > 0 ? "ok" : "nein"}">${vz(s.in.avg_pips, 1)}</td><td class="r ${s.out.avg_pips > 0 ? "ok" : "nein"}">${vz(s.out.avg_pips, 1)}</td><td class="r">${(s.gesamt.profit_faktor ?? 0).toLocaleString("de-DE")}</td></tr>`).join("") +
      `</tbody></table></div><p class="hinweis">Ø Pips je Trade nach ${hist.kosten_pips} Pips Kosten. Optimiert auf 2012–18, geprüft auf 2019–22. Nur das London-Setup in Trendrichtung lag in beiden Zeiträumen im Plus, der Vorteil ist klein. Längste Verlustserie im Backtest: ${hist.setups.london_trend.gesamt.max_verlustserie} Trades.</p>`;
  }
  h += `<h3>Tagesprognose (letzte 30 Tage)</h3><div class="kpis">${kpi(pz(q.range_pct), "Schluss in der Spanne", "Ziel 68 %")}${kpi(pz(q.direction_pct), "Trendregel richtig", `Vortagesrichtung: ${pz(q.naive_direction_pct)}`)}
    ${kpi(pz(q.news_pct), "News-Tendenz richtig", "Einschätzung von Claude")}${kpi(q.n ?? 0, "ausgewertete Tage")}</div>
    <p class="hinweis">Historisch trifft keine einfache Regel die Tagesrichtung deutlich öfter als 50 %. Trend und News sind Orientierung, keine Vorhersage.</p>`;
  const tv = d.treiber_verlauf || [];
  const ok = (x) => (x == null ? `<span class="hinweis">–</span>` : x ? `<span class="ok">✓</span>` : `<span class="nein">✗</span>`);
  h += `<h3>Treiber-Empfehlung (letzte 30 Tage)</h3><div class="kpis">${kpi(pz(q.treiber_pct), "Richtung am selben Tag", `${tage(q.treiber_n ?? 0)} mit klarer Richtung (Score ab ±15)`)}
    ${kpi(pz(q.treiber5_pct), "Richtung über 5 Tage", `${q.treiber5_n ?? 0} ausgewertet`)}</div>` +
    (tv.length ? `<div class="tabbox"><table><thead><tr><th>Tag</th><th class="r">Score</th><th>Empfehlung 06:45</th><th class="r">Tag</th><th class="r">5 Tage</th></tr></thead><tbody>` +
      tv.slice(0, 10).map((x) => `<tr><td>${wochentag(x.date)} ${datumLang(x.date).slice(0, 6)}</td><td class="r">${vz(x.score)}</td><td>${esc(x.ampel || "–")}</td><td class="r">${ok(x.hit)}</td><td class="r">${ok(x.hit5)}</td></tr>`).join("") +
      `</tbody></table></div>` : "") +
    `<p class="hinweis">Die Treiber zeigen Rückenwind und Risiko, nicht die sichere Richtung. Über 5 Tage sollten sie besser treffen als am einzelnen Tag – das wird hier ehrlich nachgemessen.</p>`;
  $("bilanz").innerHTML = h;
}

/* ---------------- Renditen ---------------- */
function renderRenditen() {
  const y = S.dash?.yields || {};
  const reihen = ["uk2", "uk10", "uk30", "jp2", "jp10", "us2", "us10"].filter((k) => y[k]);
  $("sum-renditen").textContent = y.uk10?.value != null && y.jp10?.value != null ? `Gilt 10y ${zahl(y.uk10.value, 2)} % · JGB 10y ${zahl(y.jp10.value, 2)} %` : "";
  $("renditen").innerHTML = `<thead><tr><th>Anleihe</th><th class="r">Rendite</th><th class="r">zum Vortag</th><th class="r">Stand</th></tr></thead><tbody>` +
    reihen.map((k) => { const s = y[k]; return `<tr><td>${esc(s.name)}</td><td class="r">${s.value == null ? "n/v" : zahl(s.value, 3) + " %"}</td><td class="r">${vz(s.delta_bp, 1, " bp")}</td><td class="r">${s.date ? datumLang(s.date).slice(0, 6) : "–"}</td></tr>`; }).join("") + "</tbody>";
  const pts = (S.dash?.spread || []).map((p) => [new Date(p[0] + "T12:00:00Z").getTime() / 1000, p[1]]);
  const el = $("spread");
  if (pts.length < 2) { el.innerHTML = `<p class="leer">Noch keine Renditedaten.</p>`; return; }
  const W = el.clientWidth || 500, H = 160, pad = { l: 4, r: 52, t: 8, b: 20 };
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs); let y0 = Math.min(...ys), y1 = Math.max(...ys); const sp = (y1 - y0) || 0.1; y0 -= sp * 0.1; y1 += sp * 0.1;
  const X = (v) => pad.l + ((v - x0) / (x1 - x0)) * (W - pad.l - pad.r), Y = (v) => pad.t + (1 - (v - y0) / (y1 - y0)) * (H - pad.t - pad.b);
  let s = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Zinsabstand Gilt minus JGB">`;
  for (let i = 0; i <= 2; i++) { const v = y0 + ((y1 - y0) * i) / 2; s += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--raster)"/><text x="${W - pad.r + 6}" y="${Y(v) + 4}">${zahl(v, 2)}</text>`; }
  s += `<text x="${pad.l}" y="${H - 5}">${new Date(x0 * 1000).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })}</text><text x="${W - pad.r}" y="${H - 5}" text-anchor="end">${new Date(x1 * 1000).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })}</text>`;
  s += `<path d="${pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("")}" fill="none" stroke="var(--kurs)" stroke-width="2"/></svg>`;
  el.innerHTML = s;
}

/* ---------------- Archiv ---------------- */
function quoten(ev) {
  const pct = (f) => { const w = ev.map((e) => e[f]).filter((x) => x != null); return w.length ? Math.round((100 * w.reduce((a, b) => a + b, 0)) / w.length) : null; };
  const avg = (f) => { const w = ev.map((e) => e[f]).filter((x) => x != null); return w.length ? Math.round((10 * w.reduce((a, b) => a + b, 0)) / w.length) / 10 : null; };
  return { n: ev.length, range: pct("in_range"), richtung: pct("direction_hit"), abw: avg("deviation_pips") };
}
function renderArchiv() {
  const alle = S.archiv?.reports || [], von = $("von").value, bis = $("bis").value;
  const aus = alle.filter((r) => (!von || r.date >= von) && (!bis || r.date <= bis));
  const q = quoten(aus.filter((r) => r.eval).map((r) => r.eval));
  $("archiv-kpis").innerHTML = kpi(pz(q.range), "Schluss in der Spanne") + kpi(pz(q.richtung), "Trend richtig") + kpi(vz(q.abw, 1, " P").replace("+", ""), "Ø Abweichung") + kpi(q.n, "ausgewertete Tage");
  $("archiv").innerHTML = `<div class="archiv">` + (aus.length ? aus.map((r) => {
    const e = r.eval;
    const erg = e ? `<span class="${e.in_range ? "ok" : "nein"}">${e.in_range ? "in Spanne" : "außerhalb"}</span>` : `<span class="hinweis">offen</span>`;
    const nach = e ? `\n\nErgebnis: Schluss ${zahl(e.close)}, Hoch ${zahl(e.high)}, Tief ${zahl(e.low)}, Veränderung ${vz(e.change_pips, 1)} Pips` : "";
    return `<details><summary><span>${wochentag(r.date)} ${datumLang(r.date)}</span><span class="b">${esc(r.bias || "–")} · ${zahl(r.range[0])} – ${zahl(r.range[1])}</span><span>${erg}</span></summary><pre>${esc(r.message || "")}${esc(nach)}</pre></details>`;
  }).join("") : `<p class="leer">Keine Reports im gewählten Zeitraum.</p>`) + `</div>`;
}
function zeitraum(t) { $("bis").value = t ? berlinTag(new Date()) : ""; $("von").value = t ? berlinTag(new Date(Date.now() - t * 86400000)) : ""; renderArchiv(); }
$("von").addEventListener("change", renderArchiv); $("bis").addEventListener("change", renderArchiv);
$("z30").addEventListener("click", () => zeitraum(30)); $("z90").addEventListener("click", () => zeitraum(90)); $("zalle").addEventListener("click", () => zeitraum(0));
$("d-archiv").addEventListener("toggle", () => { if ($("d-archiv").open && !S.archiv) ladeArchiv(); });

/* ---------------- Laden ---------------- */
function allesZeichnen() { renderKopf(); renderUrteil(); renderTicket(); renderChart(); renderTreiber(); renderBlick(); renderTermine(); renderLage(); renderBilanz(); renderStudie(); renderRenditen(); }
async function ladeLive() { try { S.live = await lade("live.json"); } catch (e) { S.live = null; } if (S.dash) { renderKopf(); renderTicket(); renderChart(); } }
async function ladeDash() {
  try { S.dash = await lade("dashboard.json"); } catch (e) { $("stand").textContent = "Noch keine Daten – der Server sammelt gerade."; return; }
  try { S.bt = await lade("backtest.json"); } catch (e) { S.bt = null; }
  allesZeichnen();
}
async function ladeArchiv() { try { S.archiv = await lade("archive.json"); } catch (e) { S.archiv = { reports: [] }; } renderArchiv(); }
(async () => {
  await ladeDash(); await ladeLive();
  setInterval(ladeLive, 30000);
  setInterval(ladeDash, 60000);
  setInterval(renderCountdown, 1000);
  let w = window.innerWidth;
  window.addEventListener("resize", () => { if (Math.abs(window.innerWidth - w) > 40) { w = window.innerWidth; renderChart(); renderRenditen(); } });
})();
