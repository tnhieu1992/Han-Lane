// HanLane live map. Reads only from the gateway: /api for config and controls, /ws for 1 Hz snapshots.
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";

const DRAGON_BRIDGE = [108.2275, 16.0613];
const TYPE_COLOR = { m: "#f5c518", c: "#4f9cf9", b: "#2fbf71" };
const ADVISED = "#ff7a00";
const INCIDENT = "#e5243b";
const WARN_RADIUS_M = 150;
const FALLBACK_STYLE = {
  version: 8,
  sources: {},
  layers: [{ id: "bg", type: "background", paint: { "background-color": "#1b2233" } }],
};

const $ = (id) => document.getElementById(id);
let baseScale = null;
let lastStats = {};

// ------------------------------------------------------------------ map
const cfg = await fetch("/api/config").then((r) => r.json()).catch(() => ({}));
const map = new maplibregl.Map({
  container: "map",
  style: cfg.map_style_url || FALLBACK_STYLE,
  center: cfg.center || DRAGON_BRIDGE,
  zoom: cfg.zoom || 15,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
map.on("error", (e) => console.warn("map error", e.error?.message || e));

const empty = { type: "FeatureCollection", features: [] };

map.on("load", () => {
  map.addSource("warn", { type: "geojson", data: empty });
  map.addLayer({
    id: "warn-fill", type: "fill", source: "warn",
    paint: { "fill-color": INCIDENT, "fill-opacity": 0.08 },
  });
  map.addLayer({
    id: "warn-line", type: "line", source: "warn",
    paint: { "line-color": INCIDENT, "line-width": 1.5, "line-dasharray": [2, 2], "line-opacity": 0.7 },
  });

  map.addSource("vehicles", { type: "geojson", data: empty });
  map.addLayer({
    id: "vehicles", type: "circle", source: "vehicles",
    paint: {
      "circle-radius": [
        "interpolate", ["linear"], ["zoom"],
        13, ["match", ["get", "flag"], 2, 5, 1, 3.5, 2],
        17, ["match", ["get", "flag"], 2, 12, 1, 9, 6],
      ],
      "circle-color": [
        "match", ["get", "flag"], 2, INCIDENT, 1, ADVISED,
        ["match", ["get", "type"], "m", TYPE_COLOR.m, "c", TYPE_COLOR.c, "b", TYPE_COLOR.b, "#cccccc"],
      ],
      "circle-stroke-color": ["match", ["get", "flag"], 0, "rgba(0,0,0,0.35)", "#ffffff"],
      "circle-stroke-width": ["match", ["get", "flag"], 0, 0.5, 2],
    },
  });

  map.addSource("events", { type: "geojson", data: empty });
  map.addLayer({
    id: "event-halo", type: "circle", source: "events",
    paint: { "circle-radius": 22, "circle-color": INCIDENT, "circle-opacity": 0.18,
             "circle-stroke-color": INCIDENT, "circle-stroke-width": 2 },
  });
  map.addLayer({
    id: "event-label", type: "symbol", source: "events",
    layout: { "text-field": ["get", "type"], "text-size": 12, "text-offset": [0, 2.2],
              "text-font": ["Open Sans Bold", "Arial Unicode MS Bold"], "text-allow-overlap": true },
    paint: { "text-color": INCIDENT, "text-halo-color": "#fff", "text-halo-width": 1.5 },
  });

  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
  map.on("mouseenter", "vehicles", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const p = e.features[0].properties;
    const label = { 0: "", 1: " · đã nhận cảnh báo", 2: " · sự cố" }[p.flag];
    popup.setLngLat(e.lngLat).setHTML(`<b>${p.vid}</b>${label}<br>${p.speed} m/s`).addTo(map);
  });
  map.on("mouseleave", "vehicles", () => { map.getCanvas().style.cursor = ""; popup.remove(); });
  requestAnimationFrame(animate);
});

// ------------------------------------------------------------------ smooth movement between 1 Hz snapshots
const tracks = new Map(); // vid -> {from:[lon,lat], to:[lon,lat], t, props}
let tick = 0;

function ingestVehicles(rows) {
  const now = performance.now();
  const seen = new Set();
  for (const [vid, type, lon, lat, speed, heading, flag] of rows) {
    seen.add(vid);
    const tr = tracks.get(vid);
    const cur = tr ? position(tr, now) : [lon, lat];
    tracks.set(vid, { from: cur, to: [lon, lat], t: now, props: { vid, type, speed, heading, flag } });
  }
  for (const vid of tracks.keys()) if (!seen.has(vid)) tracks.delete(vid);
}

function position(tr, now) {
  const k = Math.min(1, (now - tr.t) / 1000);
  return [tr.from[0] + (tr.to[0] - tr.from[0]) * k, tr.from[1] + (tr.to[1] - tr.from[1]) * k];
}

function animate(now) {
  if (tick++ % 2 === 0 && map.getSource("vehicles")) {
    const features = [];
    for (const tr of tracks.values()) {
      features.push({ type: "Feature", geometry: { type: "Point", coordinates: position(tr, now) }, properties: tr.props });
    }
    map.getSource("vehicles").setData({ type: "FeatureCollection", features });
  }
  requestAnimationFrame(animate);
}

function circlePolygon([lon, lat], meters, steps = 48) {
  const dLat = meters / 111320;
  const dLon = meters / (111320 * Math.cos((lat * Math.PI) / 180));
  const ring = [];
  for (let i = 0; i <= steps; i++) {
    const a = (i / steps) * 2 * Math.PI;
    ring.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
  }
  return { type: "Feature", geometry: { type: "Polygon", coordinates: [ring] }, properties: {} };
}

function renderEvents(events) {
  if (!map.getSource("events")) return;
  const located = events.filter((e) => e.lon != null);
  map.getSource("events").setData({
    type: "FeatureCollection",
    features: located.map((e) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [e.lon, e.lat] },
      properties: { type: e.type.replace("_", " "), id: e.event_id },
    })),
  });
  map.getSource("warn").setData({
    type: "FeatureCollection",
    features: located.map((e) => circlePolygon([e.lon, e.lat], WARN_RADIUS_M)),
  });
}

// ------------------------------------------------------------------ panel
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString("vi-VN", { hour12: false });
const fmtSim = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const ms = (v) => (v == null ? "–" : `${Math.round(v)}`);

function renderStats(s) {
  lastStats = s;
  if (baseScale == null && s.scale != null) baseScale = s.scale;
  $("k-vehicles").textContent = s.vehicles ?? "–";
  $("k-open").textContent = s.open_events ?? "–";
  $("k-open").className = s.open_events ? "bad" : "";
  const p95 = s.loop_p95;
  $("k-loop").textContent = p95 == null ? "–" : `${ms(p95)} / ${ms(s.loop_p50)} ms`;
  $("k-loop").className = p95 == null ? "" : p95 < 800 ? "ok" : p95 < 1000 ? "warn" : "bad";
  $("k-loop-n").textContent = s.loop_n ? `${s.loop_n} vòng gần nhất · mục tiêu p95 < 1000 ms` : "";
  $("k-write").textContent = `${s.write_mode} · ${s.last_write_ms == null ? "–" : Math.round(s.last_write_ms) + " ms"}`;
  $("k-write").className = s.pending_batches > 2 ? "bad" : s.write_mode === "single" ? "warn" : "ok";
  $("k-write-detail").textContent = `batch chờ ghi: ${s.pending_batches} · bỏ: ${s.dropped_batches}`;
  $("k-sim").textContent = fmtSim(s.sim_time || 0);
  $("k-adv").textContent = s.advisories_enabled == null ? "–" : s.advisories_enabled ? "BẬT" : "TẮT";
  $("k-adv").className = s.advisories_enabled ? "ok" : "warn";
  $("b-adv").textContent = `Cảnh báo: ${s.advisories_enabled ? "BẬT" : "TẮT"}`;
  $("b-adv").classList.toggle("active", !s.advisories_enabled);
  $("b-write").textContent = `WRITE_MODE: ${s.write_mode}`;
  $("b-write").classList.toggle("active", s.write_mode === "single");
  const x3 = baseScale && s.scale && s.scale > baseScale * 1.5;
  $("b-scale3").classList.toggle("active", !!x3);
  $("b-scale1").classList.toggle("active", !x3);
}

function renderFeed(feed) {
  const ol = $("feed");
  ol.innerHTML = "";
  for (const f of [...feed].reverse()) {
    const li = document.createElement("li");
    li.className = f.kind;
    const what = f.kind === "opened"
      ? `${f.vid ?? f.zone ?? ""} · ${f.recipients} lệnh · phát hiện ${f.detect_lag_ms} ms`
      : `đóng · ${f.reason}`;
    li.innerHTML = `<span class="t">${fmtTime(f.t)}</span><span class="type">${f.type}</span> ${what}`;
    if (f.lon != null) li.onclick = () => map.flyTo({ center: [f.lon, f.lat], zoom: 17 });
    ol.appendChild(li);
  }
}

let toastTimer;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3500);
}

async function control(path, body, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(`/api/control/${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
    const data = await r.json();
    if (!r.ok || data.error || data.detail) throw new Error(data.error || data.detail || r.statusText);
    return data;
  } catch (e) {
    toast(`Lỗi: ${e.message}`);
    return null;
  } finally {
    if (btn) btn.disabled = false;
  }
}

$("b-incident").onclick = async (e) => {
  const d = await control("incident", {}, e.target);
  if (d?.incident) {
    toast(`Sự cố: ${d.incident.vid ?? "đang chờ xe"} trên cầu Rồng (${d.incident.phase})`);
    map.flyTo({ center: DRAGON_BRIDGE, zoom: 16.5 });
  }
};
$("b-clear").onclick = async (e) => {
  const d = await control("clear", {}, e.target);
  if (d) toast(d.cleared ? `Đã dọn sự cố (${d.vid})` : "Không có sự cố");
};
$("b-adv").onclick = async (e) => {
  const d = await control("advisories", { enabled: !lastStats.advisories_enabled }, e.target);
  if (d) toast(`Cảnh báo ${d.advisories_enabled ? "BẬT" : "TẮT"}`);
};
$("b-write").onclick = async (e) => {
  const mode = lastStats.write_mode === "single" ? "batch" : "single";
  const d = await control("write-mode", { mode }, e.target);
  if (d) toast(mode === "single" ? "Ghi từng bản ghi (insert_one): xem trần M0" : "Ghi theo batch (insert_many)");
};
$("b-scale1").onclick = async (e) => {
  if (baseScale == null) return;
  const d = await control("scale", { scale: baseScale }, e.target);
  if (d) toast(`Lưu lượng ×1 (scale ${d.scale})`);
};
$("b-scale3").onclick = async (e) => {
  if (baseScale == null) return;
  const d = await control("scale", { scale: +(baseScale * 3).toFixed(2) }, e.target);
  if (d) toast(`Lưu lượng ×3 (scale ${d.scale})`);
};
$("b-fly").onclick = () => map.flyTo({ center: DRAGON_BRIDGE, zoom: 16 });

// ------------------------------------------------------------------ websocket
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { $("conn").textContent = "live"; $("conn").className = "conn on"; };
  ws.onclose = () => {
    $("conn").textContent = "offline"; $("conn").className = "conn off";
    setTimeout(connect, 1500);
  };
  ws.onmessage = (msg) => {
    const snap = JSON.parse(msg.data);
    if (snap.type !== "snapshot") return;
    ingestVehicles(snap.vehicles);
    renderEvents(snap.open_events);
    renderStats(snap.stats);
    renderFeed(snap.feed);
  };
}
connect();
