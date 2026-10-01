/* Futures Trader arayüzü: tek WebSocket, saniyede bir güncelleme. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const TZ = new Date().getTimezoneOffset() * 60; // grafik yerel saatle gösterilsin
  const C = { up: "#16c784", down: "#ea3943", signal: "#ff9f1c", be: "#f0b90b", liq: "#b36bff", em: "#ff7b00", order: "#4ea1ff" };

  const store = {
    get(k, d) { try { const v = localStorage.getItem("ft." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("ft." + k, JSON.stringify(v)); } catch {} },
  };

  const S = {
    ws: null, req: 0, pending: new Map(), intervals: [], tickers: new Map(), top: [],
    positions: [], orders: [], activePane: "A", orderPane: "A", side: "LONG", preview: null,
  };

  // ---------------------------------------------------------------- yardımcılar
  const decimalsOf = (step) => { const s = String(step); if (s.includes("e-")) return +s.split("e-")[1]; const i = s.indexOf("."); return i < 0 ? 0 : s.length - i - 1; };
  const autoDec = (p) => (p >= 1000 ? 2 : p >= 10 ? 3 : p >= 1 ? 4 : p >= 0.01 ? 5 : 7);
  const fp = (p, dec) => (p == null || !isFinite(p) ? "—" : Number(p).toFixed(dec ?? autoDec(Math.abs(p))));
  const fu = (v) => (v == null ? "—" : Number(v).toFixed(2));
  const fvol = (v) => (v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(1) + "K" : v.toFixed(0));
  const pct = (v) => (v == null || !isFinite(v) ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%");
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  const tsec = (ms) => Math.floor(ms / 1000) - TZ;
  const dtime = (ms) => new Date(ms).toLocaleString("tr-TR");
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function toast(text, level = "info") {
    const el = document.createElement("div");
    el.className = "toast " + level;
    el.textContent = text;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 6000);
  }

  // ---------------------------------------------------------------- WebSocket
  function connect() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    S.ws = ws;
    ws.onopen = () => {
      $("#conn").textContent = "bağlı"; $("#conn").className = "conn ok";
      for (const p of Object.values(panes)) if (p.symbol) p.load(p.symbol, p.interval);
    };
    ws.onclose = () => {
      $("#conn").textContent = "bağlantı koptu, yeniden deneniyor…"; $("#conn").className = "conn";
      setTimeout(connect, 2000);
    };
    ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  }

  function send(msg) { if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(msg)); }

  function request(msg) {
    return new Promise((resolve, reject) => {
      const req = ++S.req;
      S.pending.set(req, { resolve, reject });
      send({ ...msg, req });
      setTimeout(() => { if (S.pending.delete(req)) reject(new Error("Zaman aşımı")); }, 15000);
    });
  }

  function handle(m) {
    if (m.type === "hello") {
      S.intervals = m.intervals;
      const badge = $("#mode-badge");
      badge.textContent = { paper: "PAPER", testnet: "TESTNET", live: "CANLI" }[m.mode] || m.mode;
      badge.className = "badge" + (m.mode === "live" ? " live" : "");
      $("#sl-pct").textContent = Math.round(m.sl_pct * 100);
      $("#tp-pct").textContent = Math.round(m.tp_pct * 100);
      for (const p of Object.values(panes)) p.renderIntervals();
      if (!panes.A.symbol) setTimeout(initPanes, 300);
    } else if (m.type === "history") {
      panes[m.pane]?.setHistory(m);
    } else if (m.type === "tick") {
      onTick(m);
    } else if (m.type === "result") {
      const p = S.pending.get(m.req);
      if (p) { S.pending.delete(m.req); m.ok ? p.resolve(m.data) : p.reject(new Error(m.error)); }
      else if (!m.ok) toast(m.error, "error");
    }
  }

  // ---------------------------------------------------------------- grafik penceresi
  class Pane {
    constructor(id) {
      this.id = id;
      this.root = $(`.pane[data-pane="${id}"]`);
      this.symbol = null; this.interval = store.get(`pane.${id}.interval`, id === "A" ? "5m" : "15m");
      this.candles = []; this.info = { tick: 0.0001 }; this.signals = []; this.levelLines = []; this.posLines = [];
      this.zones = []; this.linesSig = ""; this.hover = null;
      const el = $(".chart", this.root);
      this.chart = LightweightCharts.createChart(el, {
        autoSize: true,
        layout: { background: { type: "solid", color: "#0f1218" }, textColor: "#9aa3b2", fontSize: 11 },
        grid: { vertLines: { color: "#181d26" }, horzLines: { color: "#181d26" } },
        timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#262c38", rightOffset: 8 },
        rightPriceScale: { borderColor: "#262c38" },
        crosshair: { mode: 0 },
      });
      this.series = this.chart.addCandlestickSeries({
        upColor: C.up, downColor: C.down, wickUpColor: C.up, wickDownColor: C.down, borderVisible: false,
      });
      this.vol = this.chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
      this.chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      this.chart.subscribeCrosshairMove((p) => { this.hover = p && p.time ? p.time : null; this.renderLegend(); });
      this.root.addEventListener("mousedown", () => setActivePane(id));
    }

    renderIntervals() {
      const box = $(".intervals", this.root);
      box.innerHTML = S.intervals.map((i) => `<button data-iv="${i}" class="${i === this.interval ? "on" : ""}">${i}</button>`).join("");
      $$("button", box).forEach((b) => (b.onclick = () => this.load(this.symbol, b.dataset.iv)));
    }

    load(symbol, interval) {
      if (!symbol) return;
      this.symbol = symbol; this.interval = interval;
      store.set(`pane.${this.id}.symbol`, symbol); store.set(`pane.${this.id}.interval`, interval);
      $(".pane-symbol", this.root).textContent = symbol;
      $$(".intervals button", this.root).forEach((b) => b.classList.toggle("on", b.dataset.iv === interval));
      send({ type: "chart", pane: this.id, symbol, interval });
      if (S.orderPane === this.id) refreshOrderSymbol();
    }

    setHistory(m) {
      this.symbol = m.symbol; this.interval = m.interval; this.info = m.info;
      const dec = decimalsOf(m.info.tick);
      this.series.applyOptions({ priceFormat: { type: "price", precision: dec, minMove: m.info.tick } });
      this.candles = m.candles;
      this.series.setData(m.candles.map((c) => ({ time: tsec(c[0]), open: c[1], high: c[2], low: c[3], close: c[4] })));
      this.vol.setData(m.candles.map((c) => this.volBar(c)));
      this.setSignals(m.signals);
      this.setLevels(m.levels);
      this.chart.timeScale().scrollToRealTime();
      this.linesSig = ""; this.updatePositionLines();
      this.renderLegend();
      if (S.orderPane === this.id) refreshOrderSymbol();
    }

    volBar(c) { return { time: tsec(c[0]), value: c[6], color: c[4] >= c[1] ? "rgba(22,199,132,.45)" : "rgba(234,57,67,.45)" }; }

    update(ch) {
      if (ch.symbol !== this.symbol || ch.interval !== this.interval || !this.candles.length) return;
      for (const c of ch.candles) {
        const last = this.candles[this.candles.length - 1];
        if (c[0] === last[0]) this.candles[this.candles.length - 1] = c;
        else if (c[0] > last[0]) this.candles.push(c);
        else { const i = this.candles.findIndex((x) => x[0] === c[0]); if (i >= 0) this.candles[i] = c; continue; }
        this.series.update({ time: tsec(c[0]), open: c[1], high: c[2], low: c[3], close: c[4] });
        this.vol.update(this.volBar(c));
      }
      if (ch.signals) this.setSignals(ch.signals);
      if (ch.levels) this.setLevels(ch.levels);
      if (!this.hover) this.renderLegend();
    }

    setSignals(sigs) {
      this.signals = sigs;
      this.sigByTime = new Map(sigs.map((s) => [tsec(s.t), s]));
      this.series.setMarkers(sigs.map((s) => ({
        time: tsec(s.t),
        position: s.dir === "up" ? "aboveBar" : "belowBar",
        color: C.signal,
        shape: "circle",
        size: 0.6,
      })));
    }

    setLevels(levels) {
      this.levelLines.forEach((l) => this.series.removePriceLine(l));
      this.levelLines = levels.map((l) => this.series.createPriceLine({
        price: l.price,
        color: l.kind === "resistance" ? "rgba(234,57,67,.75)" : "rgba(22,199,132,.75)",
        lineWidth: l.strength > 0.8 ? 3 : l.strength > 0.5 ? 2 : 1,
        lineStyle: 0,
        axisLabelVisible: true,
        title: `D${l.touches}`,
      }));
    }

    // Pozisyon / emir / önizleme çizgileri ve SL-TP taralı alanları
    updatePositionLines() {
      const pos = S.positions.find((p) => p.symbol === this.symbol);
      const orders = S.orders.filter((o) => o.symbol === this.symbol);
      const pv = S.preview && S.orderPane === this.id && S.preview.symbol === this.symbol ? S.preview : null;
      const sig = JSON.stringify([pos && [pos.breakeven.toFixed(10), pos.liq, pos.emergency, pos.legs.map((l) => l.id)], orders.map((o) => o.id), pv && [pv.price, pv.sl, pv.tp]]);
      if (sig === this.linesSig) return;
      this.linesSig = sig;
      this.posLines.forEach((l) => this.series.removePriceLine(l));
      this.posLines = []; this.zones = [];
      const line = (price, color, title, style = 0, width = 1) => price && this.posLines.push(this.series.createPriceLine({ price, color, title, lineStyle: style, lineWidth: width, axisLabelVisible: true }));
      if (pos) {
        line(pos.breakeven, C.be, "Başabaş", 0, 2);
        line(pos.liq, C.liq, "Likidasyon", 2);
        if (pos.emergency) line(pos.emergency, C.em, "Acil SL", 2);
        pos.legs.forEach((l, i) => {
          const n = pos.legs.length > 1 ? ` #${i + 1}` : "";
          line(l.sl, C.down, "SL" + n, 0);
          line(l.tp, C.up, "TP" + n, 0);
          this.zones.push({ from: pos.breakeven, to: l.sl, kind: "sl" }, { from: pos.breakeven, to: l.tp, kind: "tp" });
        });
      }
      for (const o of orders) {
        line(o.price, C.order, `Limit ${o.side === "LONG" ? "L" : "S"}`, 1);
        line(o.sl, "rgba(234,57,67,.6)", "SL (emir)", 1);
        line(o.tp, "rgba(22,199,132,.6)", "TP (emir)", 1);
      }
      if (pv) {
        line(pv.price, "rgba(78,161,255,.6)", "Önizleme", 3);
        this.zones.push({ from: pv.price, to: pv.sl, kind: "sl", ghost: true }, { from: pv.price, to: pv.tp, kind: "tp", ghost: true });
      }
    }

    drawZones() {
      const ov = $(".overlay", this.root);
      const w = ov.clientWidth - this.chart.priceScale("right").width();
      const h = ov.clientHeight - this.chart.timeScale().height();
      while (ov.children.length < this.zones.length) ov.appendChild(document.createElement("div"));
      [...ov.children].forEach((el, i) => {
        const z = this.zones[i];
        if (!z) { el.style.display = "none"; return; }
        const y1 = this.series.priceToCoordinate(z.from), y2 = this.series.priceToCoordinate(z.to);
        if (y1 == null || y2 == null) { el.style.display = "none"; return; }
        const top = Math.max(0, Math.min(y1, y2)), bottom = Math.min(h, Math.max(y1, y2));
        if (bottom <= top) { el.style.display = "none"; return; }
        el.className = "zone " + z.kind;
        el.style.cssText = `display:block;top:${top}px;height:${bottom - top}px;width:${w}px;opacity:${z.ghost ? 0.5 : 1}`;
      });
    }

    renderLegend() {
      const el = $(".legend", this.root);
      if (!this.candles.length) { el.innerHTML = ""; return; }
      let i = this.candles.length - 1;
      if (this.hover != null) { const j = this.candles.findIndex((c) => tsec(c[0]) === this.hover); if (j >= 0) i = j; }
      const c = this.candles[i], p = this.candles[i - 1];
      const dec = decimalsOf(this.info.tick);
      const pchg = p ? ((c[4] - p[4]) / p[4]) * 100 : ((c[4] - c[1]) / c[1]) * 100;
      const vchg = p && p[6] ? ((c[6] - p[6]) / p[6]) * 100 : null;
      const s = this.sigByTime && this.sigByTime.get(tsec(c[0]));
      el.innerHTML = `A <b>${fp(c[1], dec)}</b> Y <b>${fp(c[2], dec)}</b> D <b>${fp(c[3], dec)}</b> K <b class="${cls(c[4] - c[1])}">${fp(c[4], dec)}</b>
        &nbsp; Fiyat <b class="${cls(pchg)}">${pct(pchg)}</b> &nbsp; Hacim <b>${fvol(c[6])}</b> USDT <b class="${cls(vchg)}">${pct(vchg)}</b>
        ${c[7] ? "" : '<span class="muted"> (açık mum)</span>'}${s ? ` &nbsp;<b style="color:${C.signal}">⚑ Sinyal: fiyat hareketi büyüdü, hacim azaldı</b>` : ""}`;
    }
  }

  const panes = { A: new Pane("A"), B: new Pane("B") };

  function setActivePane(id) {
    S.activePane = id;
    $$(".pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === id));
    // Tıklanan pencere işlem penceresi de olur (emir panelinden ayrıca değiştirilebilir)
    if (S.orderPane !== id) selectOrderPane(id);
  }

  function selectOrderPane(id) {
    S.orderPane = id;
    $$("#o-pane button").forEach((x) => x.classList.toggle("on", x.dataset.v === id));
    $$(".pane").forEach((p) => p.classList.toggle("trade", p.dataset.pane === id));
    $("#o-price").value = "";
    refreshOrderSymbol();
  }

  function initPanes() {
    const a = store.get("pane.A.symbol", S.top[0]?.[0] || "BTCUSDT");
    const b = store.get("pane.B.symbol", S.top[1]?.[0] || "ETHUSDT");
    panes.A.load(a, panes.A.interval);
    panes.B.load(b, panes.B.interval);
    setActivePane("A");
    selectOrderPane("A");
  }

  (function zoneLoop() {
    for (const p of Object.values(panes)) p.drawZones();
    requestAnimationFrame(zoneLoop);
  })();

  // ---------------------------------------------------------------- tick
  function onTick(m) {
    S.top = m.tickers;
    for (const t of m.tickers) S.tickers.set(t[0], t);
    renderCoins();
    for (const [id, ch] of Object.entries(m.charts || {})) panes[id]?.update(ch);
    for (const p of Object.values(panes)) {
      const t = S.tickers.get(p.symbol);
      const el = $(".pane-price", p.root);
      if (t) { el.textContent = fp(t[1], decimalsOf(p.info.tick)); el.className = "pane-price " + (t[4] > 0 ? "up" : t[4] < 0 ? "down" : ""); }
    }
    S.positions = m.positions; S.orders = m.orders;
    renderAccount(m.account);
    renderFeed(m.feed_age);
    renderPositions(); renderOrders();
    for (const p of Object.values(panes)) p.updatePositionLines();
    if (m.trades) renderHistory(m.trades);
    if (m.stats) renderStats(m.stats);
    for (const e of m.events || []) { addEvent(e); if (e.level !== "info") toast(e.text, e.level); }
    if (!$("#o-price").value) fillLastPrice();
  }

  function renderFeed(age) {
    const el = $("#conn");
    if (age == null) { el.textContent = "Binance verisi bekleniyor…"; el.className = "conn bad"; }
    else if (age > 5) { el.textContent = `Binance verisi gelmiyor (${Math.round(age)} sn)`; el.className = "conn bad"; }
    else { el.textContent = "canlı veri"; el.className = "conn ok"; }
  }

  function renderAccount(a) {
    $("#account").innerHTML = [
      ["Cüzdan", fu(a.wallet)], ["Varlık", fu(a.equity)], ["Kullanılabilir", fu(a.available)],
      ["Kullanılan teminat", fu(a.used)], ["Gerç. olmayan K/Z", `<b class="${cls(a.upnl)}">${fu(a.upnl)}</b>`],
      ["Bugün K/Z", `<b class="${cls(a.daily_pnl)}">${fu(a.daily_pnl)}</b> / -${a.limits.max_daily_loss}`],
    ].map(([k, v]) => `<div><span>${k}</span>${v}</div>`).join("");
    $("#o-lev").max = a.limits.max_leverage;
  }

  // ---------------------------------------------------------------- coin listesi
  let coinFilter = "";
  // Sıralama: başlığa tıkla → büyükten küçüğe, tekrar tıkla → küçükten büyüğe, 3. tık → varsayılan (hacim)
  const SORT_COL = { sym: 0, price: 1, chg: 2, vol: 3 };
  let coinSort = store.get("coinSort", { key: "vol", dir: -1 });
  function renderSortHead() {
    $$(".coins-head span").forEach((el) => {
      const on = el.dataset.sort === coinSort.key;
      el.classList.toggle("on", on);
      el.dataset.arrow = on ? (coinSort.dir < 0 ? "▼" : "▲") : "";
    });
  }
  $$(".coins-head span").forEach((el) => (el.onclick = () => {
    const key = el.dataset.sort;
    if (coinSort.key !== key) coinSort = { key, dir: key === "sym" ? 1 : -1 };
    else if (coinSort.dir === (key === "sym" ? 1 : -1)) coinSort = { key, dir: -coinSort.dir };
    else coinSort = { key: "vol", dir: -1 };
    store.set("coinSort", coinSort);
    renderSortHead(); renderCoins();
  }));
  renderSortHead();
  $("#coin-search").addEventListener("input", (e) => { coinFilter = e.target.value.trim().toUpperCase(); renderCoins(); });
  function renderCoins() {
    const list = $("#coin-list");
    const sel = panes[S.activePane]?.symbol;
    const col = SORT_COL[coinSort.key];
    const rows = S.top.filter((t) => !coinFilter || t[0].includes(coinFilter))
      .sort((a, b) => (col === 0 ? a[0].localeCompare(b[0]) : a[col] - b[col]) * coinSort.dir);
    list.innerHTML = rows.map((t) => `<div class="coin-row${t[0] === sel ? " sel" : ""}" data-s="${t[0]}">
      <span class="sym">${t[0].replace(/USDT$/, "")}</span>
      <span class="${t[4] > 0 ? "up" : t[4] < 0 ? "down" : ""}">${fp(t[1])}</span>
      <span class="${cls(t[2])}">${pct(t[2])}</span><span class="muted">${fvol(t[3])}</span></div>`).join("");
  }
  $("#coin-list").addEventListener("click", (e) => {
    const row = e.target.closest(".coin-row");
    if (row) { const p = panes[S.activePane]; p.load(row.dataset.s, p.interval); }
  });

  // ---------------------------------------------------------------- emir paneli
  const seg = (id, cb) => $$(`#${id} button`).forEach((b) => (b.onclick = () => { $$(`#${id} button`).forEach((x) => x.classList.toggle("on", x === b)); cb(b.dataset.v); }));
  seg("o-pane", (v) => selectOrderPane(v));
  seg("o-side", (v) => { S.side = v; updateSubmit(); schedulePreview(); });
  function updateSubmit() {
    const btn = $("#o-submit");
    btn.className = "submit " + (S.side === "LONG" ? "long" : "short");
    btn.textContent = `${S.side} ${orderSymbol() || ""} @ ${$("#o-price").value || "—"}`;
  }
  $("#o-lev").addEventListener("input", (e) => { $("#o-lev-v").textContent = e.target.value; store.set("lev", e.target.value); schedulePreview(); });
  $("#o-margin").addEventListener("input", (e) => { store.set("margin", e.target.value); schedulePreview(); });
  $("#o-price").addEventListener("input", () => { updateSubmit(); schedulePreview(); });
  $("#o-last").onclick = () => { fillLastPrice(true); };
  $("#o-lev").value = store.get("lev", 5); $("#o-lev-v").textContent = $("#o-lev").value;
  $("#o-margin").value = store.get("margin", 10);

  function orderSymbol() { return panes[S.orderPane].symbol; }
  function refreshOrderSymbol() { $("#o-symbol").textContent = `${orderSymbol() || "—"}  (Pencere ${S.orderPane})`; fillLastPrice(true); updateSubmit(); }
  function fillLastPrice(force) {
    const t = S.tickers.get(orderSymbol());
    if (t && (force || !$("#o-price").value)) { $("#o-price").value = t[1]; updateSubmit(); schedulePreview(); }
  }

  let pvTimer = null;
  function schedulePreview() { clearTimeout(pvTimer); pvTimer = setTimeout(doPreview, 200); }
  async function doPreview() {
    const sym = orderSymbol(), price = +$("#o-price").value, margin = +$("#o-margin").value, lev = +$("#o-lev").value;
    const box = $("#o-preview");
    if (!sym || !(price > 0) || !(margin > 0)) { box.innerHTML = ""; S.preview = null; return; }
    try {
      const p = await request({ type: "preview", symbol: sym, side: S.side, price, margin, leverage: lev });
      S.preview = p;
      box.innerHTML = [
        ["Pozisyon büyüklüğü", fu(p.notional) + " USDT"], ["Miktar", p.qty], ["Teminat", fu(p.margin) + " USDT"],
        ["Stop loss", `<span class="down">${fp(p.sl)} (-${fu(p.sl_loss)})</span>`],
        ["Take profit", `<span class="up">${fp(p.tp)} (+${fu(p.tp_gain)})</span>`],
        ["Tahmini likidasyon", fp(p.liq)], ["Acil durum SL", fp(p.emergency)], ["Tahmini komisyon", fu(p.fee_est)],
      ].map(([k, v]) => `<div><span>${k}</span><span>${v}</span></div>`).join("");
    } catch (e) { box.innerHTML = `<div class="down">${esc(e.message)}</div>`; S.preview = null; }
    for (const p of Object.values(panes)) p.updatePositionLines();
  }

  $("#o-submit").onclick = async () => {
    const btn = $("#o-submit");
    const symbol = orderSymbol(), price = +$("#o-price").value;
    const last = S.tickers.get(symbol)?.[1];
    if (last && Math.abs(price / last - 1) > 0.03 &&
        !confirm(`${symbol} şu an ${fp(last)}. Limit fiyatı ${fp(price)} bundan %${(Math.abs(price / last - 1) * 100).toFixed(1)} uzakta.\nYine de ${S.side} emri verilsin mi?`)) return;
    btn.disabled = true;
    try {
      await request({ type: "order", pane: S.orderPane, symbol, side: S.side, price, margin: +$("#o-margin").value, leverage: +$("#o-lev").value });
      toast("Emir gönderildi", "success");
    } catch (e) { toast(e.message, "error"); }
    btn.disabled = false;
  };

  // ---------------------------------------------------------------- tablolar
  $$(".tabs button").forEach((b) => (b.onclick = () => {
    $$(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
    $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + b.dataset.tab));
  }));

  // Pozisyon altındaki işlemler varsayılan olarak kapalı; açık olanlar hatırlanır
  const openLegs = new Set(store.get("openLegs", []));
  function toggleLegs(symbol) {
    openLegs.has(symbol) ? openLegs.delete(symbol) : openLegs.add(symbol);
    store.set("openLegs", [...openLegs]);
    renderPositions();
  }

  function renderPositions() {
    const el = $("#tab-positions");
    $(".tabs button[data-tab=positions]").textContent = `Pozisyonlar (${S.positions.length})`;
    if (!S.positions.length) { el.innerHTML = '<div class="empty">Açık pozisyon yok</div>'; return; }
    el.innerHTML = `<table><tr><th>Coin</th><th>Yön</th><th>Miktar</th><th>Ort. giriş</th><th>Başabaş</th><th>Mark</th>
      <th>Likidasyon</th><th>Acil SL</th><th>Teminat</th><th>Kaldıraç</th><th>K/Z (ROE)</th><th>Funding</th><th></th></tr>` +
      S.positions.map((p) => { const open = openLegs.has(p.symbol); return `<tr class="pos-row" data-toggle="${p.symbol}">
        <td><span class="tog">${open ? "▾" : "▸"}</span> <b>${p.symbol}</b> <span class="muted">${p.legs.length} işlem</span></td><td class="${p.side === "LONG" ? "up" : "down"}">${p.side}</td><td>${p.qty}</td>
        <td>${fp(p.entry)}</td><td>${fp(p.breakeven)}</td><td>${fp(p.mark)}</td><td style="color:${C.liq}">${fp(p.liq)}</td>
        <td style="color:${C.em}">${fp(p.emergency)}</td><td>${fu(p.margin)}${p.added_margin ? ` <span class="muted">(+${fu(p.added_margin)})</span>` : ""}</td>
        <td>${p.leverage}x</td><td class="${cls(p.upnl)}">${fu(p.upnl)} (${pct(p.roe)})</td><td>${fu(-p.funding)}</td>
        <td><button data-act="margin" data-s="${p.symbol}">Teminat ekle</button> <button data-act="close" data-s="${p.symbol}">Kapat</button></td></tr>` +
        (open ? p.legs : []).map((l, i) => `<tr class="leg"><td></td><td>işlem ${i + 1}</td><td>${l.qty}</td><td>${fp(l.entry)}</td><td></td><td></td><td></td><td></td>
          <td>${fu(l.margin)}</td><td>${l.leverage}x</td><td colspan="3"><span class="down">SL ${fp(l.sl)}</span> &nbsp; <span class="up">TP ${fp(l.tp)}</span> &nbsp; ${dtime(l.opened_at)}</td></tr>`).join(""); }
      ).join("") + "</table>";
  }

  function renderOrders() {
    const el = $("#tab-orders");
    $(".tabs button[data-tab=orders]").textContent = `Bekleyen emirler (${S.orders.length})`;
    if (!S.orders.length) { el.innerHTML = '<div class="empty">Bekleyen emir yok</div>'; return; }
    el.innerHTML = `<table><tr><th>Zaman</th><th>Coin</th><th>Yön</th><th>Limit</th><th>Miktar</th><th>Dolan</th><th>Kaldıraç</th><th>Teminat</th><th>SL</th><th>TP</th><th>Pencere</th><th></th></tr>` +
      S.orders.map((o) => `<tr><td>${dtime(o.created_at)}</td><td><b>${o.symbol}</b></td><td class="${o.side === "LONG" ? "up" : "down"}">${o.side}</td>
        <td>${fp(o.price)}</td><td>${o.qty}</td><td>${o.filled}</td><td>${o.leverage}x</td><td>${fu(o.margin)}</td>
        <td class="down">${fp(o.sl)}</td><td class="up">${fp(o.tp)}</td><td>${o.pane}</td>
        <td><button data-act="cancel" data-id="${o.id}">Emri iptal et</button></td></tr>`).join("") + "</table>";
  }

  const REASONS = { SL: "Stop loss", TP: "Take profit", EMERGENCY: "Acil durum SL", LIQUIDATION: "Likidasyon", MANUAL: "Elle", EXTERNAL: "Borsada kapandı" };
  function renderHistory(trades) {
    const el = $("#tab-history");
    if (!trades.length) { el.innerHTML = '<div class="empty">Henüz kapanmış işlem yok</div>'; return; }
    el.innerHTML = `<table><tr><th>Kapanış</th><th>Coin</th><th>Yön</th><th>Miktar</th><th>Giriş</th><th>Çıkış</th><th>Kaldıraç</th><th>Teminat</th><th>Komisyon</th><th>Funding</th><th>Net K/Z</th><th>Neden</th></tr>` +
      trades.map((t) => `<tr><td>${dtime(t.closed_at)}</td><td>${t.symbol}</td><td class="${t.side === "LONG" ? "up" : "down"}">${t.side}</td><td>${t.qty}</td>
        <td>${fp(t.entry)}</td><td>${fp(t.exit)}</td><td>${t.leverage}x</td><td>${fu(t.margin)}</td><td>${fu(t.fee)}</td><td>${fu(-t.funding)}</td>
        <td class="${cls(t.pnl)}"><b>${fu(t.pnl)}</b></td><td>${REASONS[t.reason] || t.reason}</td></tr>`).join("") + "</table>";
  }

  function renderStats(s) {
    $("#tab-stats").innerHTML = `<div class="stats">${[
      ["İşlem sayısı", s.count], ["Kazanan / kaybeden", `${s.wins} / ${s.losses}`], ["Başarı oranı", s.win_rate.toFixed(1) + "%"],
      ["Gereken başarı (SL %30 / TP %15)", "≈ %66.7+"], ["Net K/Z", `<span class="${cls(s.net_pnl)}">${fu(s.net_pnl)} USDT</span>`],
      ["Ort. kazanç", fu(s.avg_win)], ["Ort. kayıp", fu(s.avg_loss)], ["Toplam komisyon", fu(s.fees)], ["Toplam funding", fu(-s.funding)],
    ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("")}</div>`;
  }

  function addEvent(e) {
    const el = $("#tab-events");
    if (!el.firstChild || el.firstChild.className === "empty") el.innerHTML = "";
    const row = document.createElement("div");
    row.className = "ev-" + e.level;
    row.textContent = `${new Date(e.ts).toLocaleTimeString("tr-TR")}  ${e.text}`;
    el.prepend(row);
    while (el.children.length > 300) el.lastChild.remove();
  }

  document.addEventListener("click", async (e) => {
    const row = e.target.closest("tr[data-toggle]");
    if (row && !e.target.closest("button")) { toggleLegs(row.dataset.toggle); return; }
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    try {
      if (b.dataset.act === "cancel") {
        if (!confirm("Bekleyen limit emir iptal edilsin mi? (Dolmuş kısım ve SL/TP'si etkilenmez)")) return;
        await request({ type: "cancel", id: b.dataset.id });
      } else if (b.dataset.act === "margin") {
        const v = prompt(`${b.dataset.s} pozisyonuna eklenecek teminat (USDT). Likidasyon fiyatı uzaklaşır, SL/TP değişmez.`);
        if (!v) return;
        await request({ type: "add_margin", symbol: b.dataset.s, amount: +v });
      } else if (b.dataset.act === "close") {
        if (!confirm(`${b.dataset.s} pozisyonunun tamamı piyasa fiyatından kapatılsın mı?`)) return;
        await request({ type: "close", symbol: b.dataset.s });
      }
    } catch (err) { toast(err.message, "error"); }
  });

  renderPositions(); renderOrders();
  $("#tab-history").innerHTML = '<div class="empty">Henüz kapanmış işlem yok</div>';
  $("#tab-events").innerHTML = '<div class="empty">Olay yok</div>';
  connect();
})();
