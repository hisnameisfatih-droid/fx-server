/* GBP/JPY Desk – liest data/dashboard.json, data/live.json, data/archive.json */
"use strict";

const PAARE = [["gbpjpy", "GBP/JPY", 3], ["usdjpy", "USD/JPY", 3], ["gbpusd", "GBP/USD", 5]];
const FLAGGE = { GBP: "🇬🇧", JPY: "🇯🇵", USD: "🇺🇸" };
const BIAS = [
  { s: -2, name: "stark bearisch", farbe: "#d03b3b" },
  { s: -1, name: "bearisch", farbe: "#ec835a" },
  { s: 0, name: "neutral", farbe: "#6f6e68" },
  { s: 1, name: "bullisch", farbe: "#5cb85c" },
  { s: 2, name: "stark bullisch", farbe: "#0ca30c" },
];
const S = { dash: null, live: null, archiv: null, newsFilter: "ALL", termin: null, archivVon: null, archivBis: null };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const zahl = (v, d = 3) => (v == null || isNaN(v) ? "–" : Number(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (v, d = 2) => (v == null || isNaN(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d }) + " %");
const uhr = (d) => d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Berlin" });
const datumKurz = (iso) => { const [y, m, d] = iso.split("-"); return `${d}.${m}.`; };
const datumLang = (iso) => { const [y, m, d] = iso.split("-"); return `${d}.${m}.${y}`; };
const wochentag = (iso) => ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"][new Date(iso + "T12:00:00").getDay()];
const berlinTag = (d) => d.toLocaleDateString("sv-SE", { timeZone: "Europe/Berlin" });

async function lade(name) {
  const r = await fetch(`data/${name}?t=${Date.now()}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${name}: ${r.status}`);
  return r.json();
}

/* ---------------- Tooltip ---------------- */
const tip = $("tip");
function zeigeTip(x, y, html) {
  tip.innerHTML = html; tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(window.innerWidth - w - 8, Math.max(8, x + 12)) + "px";
  tip.style.top = Math.max(8, y - h - 12) + "px";
}
function versteckeTip() { tip.hidden = true; }

/* ---------------- Liniendiagramm (SVG) ---------------- */
function linie(el, opt) {
  const W = el.clientWidth || 600, H = opt.hoehe || 180;
  const pad = { l: opt.padL ?? 8, r: opt.padR ?? 52, t: 10, b: opt.ohneX ? 6 : 22 };
  const pts = opt.serien.flatMap((s) => s.punkte);
  if (!pts.length) { el.innerHTML = `<p class="hint">${esc(opt.leer || "Noch keine Daten.")}</p>`; return; }
  let x0 = opt.x0 ?? Math.min(...pts.map((p) => p[0])), x1 = opt.x1 ?? Math.max(...pts.map((p) => p[0]));
  const ys = pts.map((p) => p[1]).concat(opt.extraY || []).filter((v) => v != null);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const spanne = (y1 - y0) || Math.abs(y1) * 0.001 || 1; y0 -= spanne * 0.08; y1 += spanne * 0.08;
  if (x1 === x0) x1 = x0 + 1;
  const X = (v) => pad.l + ((v - x0) / (x1 - x0)) * (W - pad.l - pad.r);
  const Y = (v) => pad.t + (1 - (v - y0) / (y1 - y0)) * (H - pad.t - pad.b);
  const d = opt.yStellen ?? 3;
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(opt.titel || "Diagramm")}">`;
  // Raster + Y-Achse rechts
  for (let i = 0; i <= 3; i++) {
    const v = y0 + ((y1 - y0) * i) / 3, y = Y(v);
    svg += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}" stroke="var(--grid)" stroke-width="1"/>`;
    if (!opt.ohneY) svg += `<text x="${W - pad.r + 6}" y="${y + 3}">${zahl(v, d)}</text>`;
  }
  if (opt.band) {
    const [b0, b1] = opt.band;
    svg += `<rect x="${pad.l}" width="${W - pad.l - pad.r}" y="${Y(b1)}" height="${Math.max(1, Y(b0) - Y(b1))}" fill="var(--band)" stroke="rgba(57,135,229,0.45)" stroke-width="1"/>`;
  }
  for (const h of opt.hlinien || []) {
    if (h.y == null) continue;
    svg += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${Y(h.y)}" y2="${Y(h.y)}" stroke="${h.farbe}" stroke-width="${h.breite || 1.5}" stroke-dasharray="${h.strich || ""}"/>`;
    if (h.label) svg += h.rechts
      ? `<text x="${W - pad.r - 4}" y="${Y(h.y) - 4}" text-anchor="end" style="fill:${h.textfarbe || "var(--ink-2)"}">${esc(h.label)}</text>`
      : `<text x="${pad.l + 4}" y="${Y(h.y) - 4}" style="fill:${h.textfarbe || "var(--ink-2)"}">${esc(h.label)}</text>`;
  }
  // X-Achse
  if (!opt.ohneX && opt.xTicks) {
    for (const [v, label] of opt.xTicks(x0, x1, W)) {
      svg += `<text x="${X(v)}" y="${H - 6}" text-anchor="middle">${esc(label)}</text>`;
    }
  }
  for (const s of opt.serien) {
    if (!s.punkte.length) continue;
    const pfad = s.punkte.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    svg += `<path d="${pfad}" fill="none" stroke="${s.farbe}" stroke-width="${s.breite || 2}" stroke-linejoin="round" stroke-linecap="round"/>`;
    const l = s.punkte[s.punkte.length - 1];
    if (s.endpunkt) svg += `<circle cx="${X(l[0])}" cy="${Y(l[1])}" r="4" fill="${s.farbe}" stroke="var(--surface)" stroke-width="2"/>`;
  }
  svg += `<line class="cross" x1="0" x2="0" y1="${pad.t}" y2="${H - pad.b}" stroke="var(--ink-2)" stroke-width="1" visibility="hidden"/>`;
  svg += `<circle class="crossdot" r="4" fill="${opt.serien[0].farbe}" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>`;
  svg += `<rect class="hit" x="${pad.l}" y="0" width="${W - pad.l - pad.r}" height="${H}" fill="transparent"/></svg>`;
  el.innerHTML = svg;
  const haupt = opt.serien[0].punkte, cross = el.querySelector(".cross"), dot = el.querySelector(".crossdot");
  const bewegen = (ev) => {
    const r = el.querySelector("svg").getBoundingClientRect();
    const px = ((ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left) * (W / r.width);
    const xv = x0 + ((px - pad.l) / (W - pad.l - pad.r)) * (x1 - x0);
    let best = haupt[0];
    for (const p of haupt) if (Math.abs(p[0] - xv) < Math.abs(best[0] - xv)) best = p;
    cross.setAttribute("x1", X(best[0])); cross.setAttribute("x2", X(best[0])); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(best[0])); dot.setAttribute("cy", Y(best[1])); dot.setAttribute("visibility", "visible");
    zeigeTip(ev.touches ? ev.touches[0].clientX : ev.clientX, ev.touches ? ev.touches[0].clientY : ev.clientY, opt.tip(best));
  };
  const weg = () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); versteckeTip(); };
  const hit = el.querySelector(".hit");
  hit.addEventListener("mousemove", bewegen); hit.addEventListener("touchstart", bewegen, { passive: true });
  hit.addEventListener("touchmove", bewegen, { passive: true }); hit.addEventListener("mouseleave", weg); hit.addEventListener("touchend", weg);
}

function sparkline(punkte, farbe, w = 100, h = 28) {
  if (!punkte || punkte.length < 2) return "";
  const xs = punkte.map((p) => p[0]), ys = punkte.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const X = (v) => 2 + ((v - x0) / ((x1 - x0) || 1)) * (w - 6), Y = (v) => 3 + (1 - (v - y0) / ((y1 - y0) || 1)) * (h - 6);
  const d = punkte.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
  const l = punkte[punkte.length - 1];
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none" aria-hidden="true"><path d="${d}" fill="none" stroke="${farbe}" stroke-width="1.6" vector-effect="non-scaling-stroke"/><circle cx="${X(l[0])}" cy="${Y(l[1])}" r="2.5" fill="${farbe}"/></svg>`;
}

/* ---------------- Kopf ---------------- */
function renderStand() {
  const live = S.live, jetzt = Date.now() / 1000;
  let txt = "";
  if (live) {
    const alt = jetzt - live.updated;
    const stale = alt > 180 && !live.market_closed;
    txt = `<span class="dot${stale ? " stale" : ""}"></span>` +
      (live.market_closed ? "Markt geschlossen" : stale ? `Live-Daten verzögert (${Math.round(alt / 60)} Min)` : `Live · ${uhr(new Date(live.updated * 1000))}`);
  } else txt = "Live-Daten fehlen";
  if (S.dash?.report) txt += ` · Report ${uhr(new Date(S.dash.report.created))}`;
  $("stand").innerHTML = txt;
}

/* ---------------- Kacheln ---------------- */
function renderKacheln() {
  const html = PAARE.map(([p, name, d]) => {
    const l = S.live?.pairs?.[p];
    const preis = l?.price ?? (p === "gbpjpy" ? S.dash?.report?.price : null);
    const vor = S.dash?.prev_close?.[p]?.close;
    const ch = preis && vor ? (preis / vor - 1) * 100 : null;
    const kl = ch == null ? "flat" : ch > 0 ? "up" : ch < 0 ? "down" : "flat";
    const pfeil = ch == null ? "" : ch > 0 ? "▲ " : ch < 0 ? "▼ " : "";
    const farbe = kl === "up" ? "var(--good)" : kl === "down" ? "var(--bad)" : "var(--ink-2)";
    return `<div class="tile"><div class="name">${name}</div><div class="chg ${kl}">${pfeil}${pct(ch)}</div>
      <div class="price">${zahl(preis, d)}</div><div></div>
      <div class="spark">${sparkline(l?.bars, farbe, 300, 42)}</div></div>`;
  }).join("");
  $("tiles").innerHTML = html;
}

/* ---------------- Bias ---------------- */
function renderBias() {
  const r = S.dash?.report;
  if (!r) { $("bias").innerHTML = `<p class="hint">Heute noch kein Report (kommt Mo–Fr um 06:45 Uhr).</p>`; }
  else {
    const s = r.score ?? 0, b = BIAS.find((x) => x.s === s);
    $("bias").innerHTML = `<div class="biasword" style="color:${b.farbe}">${esc(b.name)}</div>
      <div class="gauge"><div class="bar">${BIAS.map((x) => `<span class="${x.s === s ? "on" : ""}" style="background:${x.farbe}"></span>`).join("")}</div>
      <div class="needle" style="left:${((s + 2) * 20 + 10)}%"></div>
      <div class="labels">${BIAS.map((x) => `<span>${x.name.replace("stark ", "stark<br>")}</span>`).join("")}</div></div>
      <p class="reason">${esc(r.reason_long || r.reason || "")}</p>`;
  }
  const hist = S.dash?.bias_history || [];
  $("bias-hist").innerHTML = hist.length ? hist.map((h) => {
    const b = BIAS.find((x) => x.s === (h.score ?? 0));
    const treffer = h.hit == null ? "·" : h.hit ? "✓" : "✗";
    return `<div class="d" title="${esc(datumLang(h.date))}: ${esc(b.name)}${h.hit == null ? "" : h.hit ? " – Richtung getroffen" : " – Richtung verfehlt"}\n${esc(h.reason || "")}">
      <div class="sq" style="background:${b.farbe}">${treffer}</div><span>${+h.date.slice(8)}.</span></div>`;
  }).join("") : `<p class="hint" style="grid-column:1/-1;margin:0">Noch keine Historie.</p>`;
}

/* ---------------- Prognose-Panel ---------------- */
function renderPrognose() {
  const r = S.dash?.report, fw = S.dash?.fx_window;
  const bars = (S.live?.pairs?.gbpjpy?.bars || []).filter((b) => !fw || (b[0] >= fw[0] && b[0] <= fw[1]));
  const preis = S.live?.pairs?.gbpjpy?.price;
  const status = $("prog-status");
  if (!r || r.range[0] == null) {
    status.innerHTML = `<span class="pill neutral">Noch keine Prognose für heute</span>`;
  } else if (preis) {
    const [lo, hi] = r.range;
    if (preis < lo) status.innerHTML = `<span class="pill bad">▼ Kurs unter der Spanne (${zahl(lo)} – ${zahl(hi)})</span>`;
    else if (preis > hi) status.innerHTML = `<span class="pill bad">▲ Kurs über der Spanne (${zahl(lo)} – ${zahl(hi)})</span>`;
    else status.innerHTML = `<span class="pill good">● Spanne hält – ${zahl(preis)} liegt in ${zahl(lo)} – ${zahl(hi)}</span>`;
  } else status.innerHTML = "";
  const hl = [];
  if (r?.target != null) hl.push({ y: r.target, farbe: "var(--series-2)", strich: "6 4", label: `Ziel ${zahl(r.target)}`, textfarbe: "#f0a07e", rechts: true });
  if (r?.levels?.[0] != null) hl.push({ y: r.levels[0], farbe: "var(--muted)", strich: "2 3", breite: 1, label: `Vortageshoch ${zahl(r.levels[0])}` });
  if (r?.levels?.[1] != null) hl.push({ y: r.levels[1], farbe: "var(--muted)", strich: "2 3", breite: 1, label: `Vortagestief ${zahl(r.levels[1])}` });
  linie($("prog-chart"), {
    titel: "GBP/JPY heute mit Prognose-Spanne", hoehe: 220,
    serien: [{ punkte: bars, farbe: "var(--series)", endpunkt: true }],
    band: r && r.range[0] != null ? r.range : null, hlinien: hl,
    extraY: [r?.range?.[0], r?.range?.[1], r?.target, r?.levels?.[0], r?.levels?.[1]].filter((v) => v != null),
    x0: fw?.[0], x1: fw?.[1], leer: "Noch keine Kurse für den heutigen Handelstag.",
    xTicks: (a, b, w) => { const s = w < 520 ? 21600 : 10800, t = []; for (let h = Math.ceil(a / s) * s; h <= b; h += s) t.push([h, uhr(new Date(h * 1000))]); return t; },
    tip: (p) => `${uhr(new Date(p[0] * 1000))} · <b>${zahl(p[1])}</b>`,
  });
  $("prog-legend").innerHTML = `<span><i style="background:var(--series)"></i>Kurs (5 Min)</span>` +
    (r ? `<span><i class="band"></i>Spanne für den Schluss</span><span><i style="background:var(--series-2)"></i>Zielkurs</span><span><i style="background:var(--muted)"></i>Vortageshoch/-tief</span>` : "");
}

/* ---------------- Lage ---------------- */
function renderLage() {
  const r = S.dash?.report;
  if (!r) { $("lage").innerHTML = `<p class="hint">Kommt mit dem Morgenreport.</p>`; return; }
  $("lage").innerHTML = `<p><b>🇬🇧 GBP</b>${esc(r.lage_gbp || "–")}</p><p><b>🇯🇵 JPY</b>${esc(r.lage_jpy || "–")}</p>
    <p><b>Risiken</b>${esc(r.risks || "–")}</p>` +
    (r.missing?.length ? `<p class="missing">⚠️ Heute fehlende Quellen: ${esc(r.missing.join(", "))}</p>` : "");
}

/* ---------------- Termine ---------------- */
function termineHeute() {
  const heute = berlinTag(new Date());
  return (S.dash?.calendar || []).filter((t) => berlinTag(new Date(t.ts)) === heute);
}
function renderTermine() {
  const tl = $("timeline"), jetzt = new Date(), liste = termineHeute();
  const anteil = (d) => { const p = new Intl.DateTimeFormat("de-DE", { hour: "numeric", minute: "numeric", hourCycle: "h23", timeZone: "Europe/Berlin" }).formatToParts(d);
    const h = +p.find((x) => x.type === "hour").value, m = +p.find((x) => x.type === "minute").value; return (h + m / 60) / 24; };
  let html = `<div class="axis"></div>`;
  for (let h = 0; h <= 24; h += 3) html += `<div class="tick" style="left:${(h / 24) * 100}%">${String(h).padStart(2, "0")}</div>`;
  liste.forEach((t, i) => {
    const d = new Date(t.ts), vorbei = d < jetzt;
    html += `<button class="ev ${t.impact}${vorbei ? " past" : ""}" style="left:${anteil(d) * 100}%" data-i="${i}" aria-label="${esc(uhr(d) + " " + t.currency + " " + t.title)}"></button>`;
    html += `<div class="tick" style="left:${anteil(d) * 100}%;top:0">${FLAGGE[t.currency] || ""}</div>`;
  });
  html += `<div class="now" style="left:${anteil(jetzt) * 100}%"></div>`;
  tl.innerHTML = html;
  tl.querySelectorAll(".ev").forEach((b) => b.addEventListener("click", () => zeigeTermin(liste[+b.dataset.i])));
  $("termin-liste").innerHTML = liste.length ? liste.map((t, i) => {
    const d = new Date(t.ts);
    return `<div class="row${d < jetzt ? " past" : ""}" data-i="${i}" role="button" tabindex="0"><span class="time">${uhr(d)}</span><span class="imp ${t.impact}" title="${t.impact === "high" ? "hoher" : "mittlerer"} Impact"></span>
      <span>${FLAGGE[t.currency] || ""} <span class="fx">${t.currency}</span></span><span>${esc(t.title)}</span></div>`;
  }).join("") : `<div class="empty">Heute keine wichtigen Termine für GBP, JPY oder USD.</div>`;
  $("termin-liste").querySelectorAll(".row").forEach((r) => {
    const f = () => zeigeTermin(liste[+r.dataset.i]);
    r.addEventListener("click", f); r.addEventListener("keydown", (e) => { if (e.key === "Enter") f(); });
  });
  renderCountdown();
}
function zeigeTermin(t) {
  const el = $("termin-detail");
  if (S.termin === t?.id) { el.hidden = true; S.termin = null; return; }
  S.termin = t.id;
  el.innerHTML = `<div class="t">${uhr(new Date(t.ts))} ${FLAGGE[t.currency] || ""} ${esc(t.title)} <span class="tag ${t.impact === "high" ? "minus" : "neutral"}">${t.impact === "high" ? "hoch" : "mittel"}</span></div>
    <div class="vals"><span>Forecast: <b>${esc(t.forecast || "–")}</b></span><span>Previous: <b>${esc(t.previous || "–")}</b></span><span>Actual: <b>${esc(t.actual || "n/v")}</b></span></div>
    <div>${esc(t.effect || "Erwartete Wirkung: kommt mit dem Morgenreport.")}</div>`;
  el.hidden = false;
}
function renderCountdown() {
  const jetzt = new Date();
  const naechster = (S.dash?.calendar || []).filter((t) => t.impact === "high" && new Date(t.ts) > jetzt).sort((a, b) => a.ts.localeCompare(b.ts))[0];
  if (!naechster) { $("countdown").textContent = "Kein roter Termin mehr bis morgen"; return; }
  let s = Math.round((new Date(naechster.ts) - jetzt) / 1000);
  const h = Math.floor(s / 3600); s -= h * 3600; const m = Math.floor(s / 60); s -= m * 60;
  $("countdown").innerHTML = `Nächster 🔴 ${FLAGGE[naechster.currency] || ""} ${esc(naechster.title)} in <b>${h ? h + " h " : ""}${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}</b>`;
}

/* ---------------- Renditen ---------------- */
function renderRenditen() {
  const y = S.dash?.yields || {};
  const reihen = ["uk10", "uk30", "jp10", "jp2", "us10"];
  $("ytable").innerHTML = `<thead><tr><th>Anleihe</th><th class="num">Rendite</th><th class="num">Δ Vortag</th><th>30 Tage</th><th class="num">Stand</th></tr></thead><tbody>` +
    reihen.map((k) => {
      const s = y[k]; if (!s) return "";
      const delta = s.delta_bp == null ? "–" : (s.delta_bp > 0 ? "+" : s.delta_bp < 0 ? "−" : "±") + Math.abs(s.delta_bp).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " bp";
      return `<tr><td>${esc(s.name)}</td><td class="num">${s.value == null ? "n/v" : zahl(s.value, 3) + " %"}</td><td class="num">${delta}</td>
        <td class="spark">${sparkline(s.points.map((p, i) => [i, p[1]]), "var(--series)", 110, 24)}</td><td class="num">${s.date ? datumKurz(s.date) : "–"}</td></tr>`;
    }).join("") + "</tbody>";
  const sp = (S.dash?.spread || []).map((p) => [new Date(p[0] + "T12:00:00Z").getTime() / 1000, p[1]]);
  linie($("spread-chart"), {
    titel: "Zinsdifferenz Gilt 10y minus JGB 10y", hoehe: 190, yStellen: 2, serien: [{ punkte: sp, farbe: "var(--series)", endpunkt: true }],
    leer: "Noch keine Renditedaten.",
    xTicks: (a, b) => { const t = []; const schritt = Math.max(86400 * 7, Math.ceil((b - a) / 5 / 86400) * 86400); for (let v = a; v <= b; v += schritt) t.push([v, new Date(v * 1000).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })]); return t; },
    tip: (p) => `${new Date(p[0] * 1000).toLocaleDateString("de-DE")} · <b>${zahl(p[1], 2)} %-Pkt.</b>`,
  });
}

/* ---------------- Treffer ---------------- */
function kpiHtml(q, mitVergleich = true) {
  const v = (x, suf = " %") => (x == null ? "–" : x.toLocaleString("de-DE") + suf);
  return `<div class="kpi"><div class="v">${v(q.range_pct)}</div><div class="l">Schluss in der Spanne</div><div class="c">Modell erwartet ca. 68 %</div></div>
    <div class="kpi"><div class="v">${v(q.direction_pct)}</div><div class="l">Bias-Richtung getroffen</div>${mitVergleich ? `<div class="c">einfaches Modell: ${v(q.naive_direction_pct)}</div>` : ""}</div>
    <div class="kpi"><div class="v">${v(q.avg_dev, " Pips")}</div><div class="l">Ø Abweichung vom Zielkurs</div>${mitVergleich ? `<div class="c">ohne Bias: ${v(q.naive_avg_dev, " Pips")}</div>` : ""}</div>
    <div class="kpi"><div class="v">${q.n}</div><div class="l">ausgewertete Tage</div></div>`;
}
function renderTreffer() {
  const st = S.dash?.stats;
  if (!st) return;
  $("kpis").innerHTML = kpiHtml(st.q30);
  const z = st.last10;
  $("last10").innerHTML = z.length ? `<thead><tr><th>Datum</th><th>Spanne</th><th>Bias</th><th class="num">Schluss</th><th>Treffer</th><th class="num">Abw.</th></tr></thead><tbody>` +
    z.map((e) => `<tr><td>${wochentag(e.date)} ${datumKurz(e.date)}</td><td>${zahl(e.range[0])} – ${zahl(e.range[1])}</td><td>${esc(e.bias)}</td>
      <td class="num">${zahl(e.close)}</td><td><span class="${e.in_range ? "ok" : "no"}">${e.in_range ? "✓" : "✗"} Spanne</span> · <span class="${e.direction_hit ? "ok" : "no"}">${e.direction_hit ? "✓" : "✗"} Richtung</span></td>
      <td class="num">${zahl(e.deviation_pips, 0)} P</td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="hint" style="white-space:normal">Die erste Auswertung kommt nach Handelsschluss (23:05 Uhr).</td></tr></tbody>`;
}

/* ---------------- News ---------------- */
function renderNews() {
  const f = S.newsFilter;
  const liste = (S.dash?.news || []).filter((n) => f === "ALL" || (n.currencies || []).includes(f));
  $("news").innerHTML = liste.length ? liste.map((n) => {
    const e = n.effect || "";
    const kl = e.endsWith("+") ? "plus" : e.endsWith("-") ? "minus" : "neutral";
    const label = e && e !== "neutral" ? `${e.slice(0, 3)}-${e.endsWith("+") ? "positiv" : "negativ"}` : "neutral";
    return `<li><div class="head"><span>${uhr(new Date(n.ts))}</span><span>${esc(n.source)}</span>${n.effect ? `<span class="tag ${kl}">${esc(label)}</span>` : ""}</div>
      <a class="title" href="${esc(n.url)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>${n.note ? `<div class="note">${esc(n.note)}</div>` : ""}</li>`;
  }).join("") : `<li class="hint">Keine passenden Meldungen in den letzten 24 Stunden.</li>`;
}
$("news-filter").addEventListener("click", (e) => {
  const b = e.target.closest(".chip"); if (!b) return;
  S.newsFilter = b.dataset.f;
  $("news-filter").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === b));
  renderNews();
});

/* ---------------- Archiv ---------------- */
function quoten(evals) {
  const n = evals.length;
  const pctOf = (f) => { const w = evals.map((e) => e[f]).filter((x) => x != null); return w.length ? Math.round((100 * w.reduce((a, b) => a + b, 0)) / w.length) : null; };
  const avg = (f) => { const w = evals.map((e) => e[f]).filter((x) => x != null); return w.length ? Math.round((10 * w.reduce((a, b) => a + b, 0)) / w.length) / 10 : null; };
  return { n, range_pct: pctOf("in_range"), direction_pct: pctOf("direction_hit"), avg_dev: avg("deviation_pips"),
    naive_avg_dev: avg("naive_deviation_pips"), naive_direction_pct: pctOf("naive_direction_hit") };
}
function renderArchiv() {
  const alle = S.archiv?.reports || [];
  const von = $("von").value, bis = $("bis").value;
  const auswahl = alle.filter((r) => (!von || r.date >= von) && (!bis || r.date <= bis));
  $("archiv-kpis").innerHTML = kpiHtml(quoten(auswahl.filter((r) => r.eval).map((r) => r.eval)));
  $("archiv-liste").innerHTML = auswahl.length ? auswahl.map((r) => {
    const e = r.eval;
    const erg = e ? `<span class="${e.in_range ? "ok" : "no"}">${e.in_range ? "✓" : "✗"}</span> <span class="${e.direction_hit ? "ok" : "no"}">${e.direction_hit ? "✓" : "✗"}</span> ${zahl(e.deviation_pips, 0)} P` : `<span class="hint">offen</span>`;
    const vergleich = e ? `\n\n— Ergebnis —\nSchluss ${zahl(e.close)} (Spanne ${zahl(r.range[0])} – ${zahl(r.range[1])}, Ziel ${zahl(r.target)})\nTageshoch ${zahl(e.high)} · Tagestief ${zahl(e.low)} · Veränderung ${zahl(e.change_pips, 1)} Pips\nSpanne ${e.in_range ? "getroffen" : "verfehlt"} · Richtung ${e.direction_hit ? "getroffen" : "verfehlt"} · Abweichung ${zahl(e.deviation_pips, 1)} Pips` : "";
    return `<details><summary><span>${wochentag(r.date)} ${datumLang(r.date)}</span><span class="b">${esc(r.bias || "–")} · ${zahl(r.range[0])} – ${zahl(r.range[1])}</span><span>${erg}</span></summary>
      <pre>${esc(r.message || "")}${esc(vergleich)}</pre></details>`;
  }).join("") : `<p class="hint">Keine Reports im gewählten Zeitraum.</p>`;
}
function setzeZeitraum(tage) {
  const bis = berlinTag(new Date());
  $("bis").value = tage ? bis : "";
  $("von").value = tage ? berlinTag(new Date(Date.now() - tage * 86400000)) : "";
  renderArchiv();
}
$("von").addEventListener("change", renderArchiv); $("bis").addEventListener("change", renderArchiv);
$("zeitraum-30").addEventListener("click", () => setzeZeitraum(30));
$("zeitraum-90").addEventListener("click", () => setzeZeitraum(90));
$("zeitraum-all").addEventListener("click", () => setzeZeitraum(0));

/* ---------------- Laden ---------------- */
async function ladeLive() {
  try { S.live = await lade("live.json"); } catch (e) { S.live = null; }
  renderStand(); renderKacheln(); renderPrognose();
}
async function ladeDash() {
  try { S.dash = await lade("dashboard.json"); } catch (e) {
    $("stand").textContent = "Dashboard-Daten noch nicht vorhanden – der Server sammelt gerade."; return;
  }
  renderStand(); renderKacheln(); renderBias(); renderPrognose(); renderLage(); renderTermine(); renderRenditen(); renderTreffer(); renderNews();
}
async function ladeArchiv() {
  try { S.archiv = await lade("archive.json"); renderArchiv(); } catch (e) { $("archiv-liste").innerHTML = `<p class="hint">Archiv noch leer.</p>`; }
}
(async () => {
  await ladeDash(); await ladeLive(); ladeArchiv();
  setInterval(ladeLive, 60000);
  setInterval(() => { ladeDash(); ladeArchiv(); }, 5 * 60000);
  setInterval(() => { renderCountdown(); }, 1000);
  setInterval(() => { renderTermine(); }, 60000);
  let w = window.innerWidth;
  window.addEventListener("resize", () => { if (Math.abs(window.innerWidth - w) > 40) { w = window.innerWidth; renderPrognose(); renderRenditen(); } });
})();
