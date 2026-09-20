/* Mapa ofert — Leaflet + klastrowanie.
   Cały stan siedzi w adresie URL, więc widok da się wysłać linkiem. */

/* Środek Polski i zoom, przy którym widać cały kraj. Mapa startuje stąd,
   a po wczytaniu punktów dopasowuje się do nich — ale zanim to nastąpi,
   użytkownik ma widzieć Polskę, a nie jedno województwo. */
const POLAND = [52.05, 19.35];
const POLAND_ZOOM = 6;

// progi ceny za m² (zł) i barwy — od najtańszych do najdroższych
const SCALE = [
  { limit: 4000,  color: '#1a9850', label: '< 4 tys.' },
  { limit: 6000,  color: '#66bd63', label: '4–6 tys.' },
  { limit: 8000,  color: '#d9b511', label: '6–8 tys.' },
  { limit: 10000, color: '#f46d43', label: '8–10 tys.' },
  { limit: Infinity, color: '#d73027', label: '> 10 tys.' },
];
const NO_PRICE = '#7b8794';

const KIND_COLOR = { licytacja: '#6b4ec9', przetarg: '#c98218', wykaz: '#c98218' };

function colorFor(p) {
  if (KIND_COLOR[p.k]) return KIND_COLOR[p.k];
  if (!p.m2) return NO_PRICE;
  return (SCALE.find(s => p.m2 < s.limit) || SCALE[SCALE.length - 1]).color;
}

const fmt = (n) => n == null ? '—' : Math.round(n).toLocaleString('pl-PL');

function pinLabel(p) {
  if (p.p == null) return '?';
  if (p.p >= 1000000) return (p.p / 1000000).toFixed(p.p >= 10000000 ? 0 : 1) + ' mln';
  if (p.p >= 1000) return Math.round(p.p / 1000) + ' tys.';
  return fmt(p.p);
}

function popupHtml(p) {
  const where = [p.st, p.c].filter(Boolean).join(', ');
  const params = [
    p.a ? `${p.a} m²` : null,
    p.r ? `${p.r} pok.` : null,
    p.m2 ? `${fmt(p.m2)} zł/m²` : null,
  ].filter(Boolean).join(' · ');
  const badges = [
    p.k === 'licytacja' ? '<b style="color:#6b4ec9">LICYTACJA</b>' : null,
    p.k === 'przetarg' ? '<b style="color:#c98218">PRZETARG</b>' : null,
    p.s === 'prywatna' ? 'prywatna' : (p.s === 'posrednik' ? 'pośrednik' : p.s),
    p.cop ? `${p.cop} kopii` : null,
    p.prec === 'city' ? '<i>lokalizacja przybliżona</i>' : null,
  ].filter(Boolean).join(' · ');
  return `<h4>${fmt(p.p)} zł</h4>
    <div><b>${p.t}</b></div>
    <div class="pop-meta">${where || ''}${params ? '<br>' + params : ''}</div>
    <div class="pop-meta" style="font-size:12px">${p.src} · stoi ${p.d} dni${badges ? ' · ' + badges : ''}</div>
    <a href="/oferta/${p.id}">Szczegóły</a> ·
    <a href="${p.u}" target="_blank" rel="noopener noreferrer">Źródło ↗</a>`;
}

async function initMap(query) {
  const map = L.map('map', { preferCanvas: true }).setView(POLAND, POLAND_ZOOM);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);

  const legend = document.getElementById('legend');
  legend.innerHTML = SCALE.map(s => `<span style="background:${s.color}">${s.label}</span>`).join('');

  const cluster = L.markerClusterGroup({
    chunkedLoading: true,
    maxClusterRadius: 48,
    spiderfyOnMaxZoom: true,
    disableClusteringAtZoom: 16,
  });

  const params = new URLSearchParams(query);
  params.set('limit', '8000');
  let features = [];
  try {
    const resp = await fetch('/api/geojson?' + params.toString());
    features = (await resp.json()).features || [];
  } catch (e) {
    document.getElementById('map-count').textContent = 'nie udało się wczytać punktów';
    return;
  }

  const bounds = [];
  for (const f of features) {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    const color = colorFor(p);
    const marker = L.marker([lat, lon], {
      icon: L.divIcon({
        className: '',
        html: `<div class="pin" style="background:${color}">${pinLabel(p)}</div>`,
        iconSize: null,
      }),
    });
    marker.bindPopup(() => popupHtml(p), { maxWidth: 300 });
    marker.__props = p;
    cluster.addLayer(marker);
    bounds.push([lat, lon]);
  }
  map.addLayer(cluster);
  if (bounds.length) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 13 });

  const counter = document.getElementById('map-count');
  counter.textContent = `${features.length} ofert na mapie`;

  // --- szukanie w promieniu: klik w mapę wyznacza środek okręgu
  let radiusMode = false, circle = null;
  document.getElementById('btn-radius').addEventListener('click', (e) => {
    radiusMode = !radiusMode;
    e.target.classList.toggle('btn--ghost', radiusMode);
    e.target.textContent = radiusMode ? 'Kliknij środek na mapie…' : 'Szukaj w promieniu';
    if (!radiusMode && circle) { map.removeLayer(circle); circle = null; applyRadius(null); }
  });

  function applyRadius(center, radiusM) {
    let shown = 0;
    cluster.clearLayers();
    const markers = [];
    for (const f of features) {
      const [lon, lat] = f.geometry.coordinates;
      if (center && map.distance(center, [lat, lon]) > radiusM) continue;
      const p = f.properties;
      const m = L.marker([lat, lon], {
        icon: L.divIcon({ className: '',
          html: `<div class="pin" style="background:${colorFor(p)}">${pinLabel(p)}</div>`,
          iconSize: null }),
      });
      m.bindPopup(() => popupHtml(p), { maxWidth: 300 });
      markers.push(m); shown++;
    }
    cluster.addLayers(markers);
    counter.textContent = center
      ? `${shown} ofert w promieniu ${(radiusM / 1000).toFixed(1)} km`
      : `${shown} ofert na mapie`;
  }

  map.on('click', (ev) => {
    if (!radiusMode) return;
    const radiusM = 3000;
    if (circle) map.removeLayer(circle);
    circle = L.circle(ev.latlng, { radius: radiusM, color: '#0a84c4', weight: 2, fillOpacity: 0.06 }).addTo(map);
    applyRadius(ev.latlng, radiusM);
  });

  // --- podgląd rozkładu cen za m² w widocznym obszarze
  document.getElementById('btn-stats').addEventListener('click', () => {
    const visible = features.filter(f => {
      const [lon, lat] = f.geometry.coordinates;
      return map.getBounds().contains([lat, lon]) && f.properties.m2;
    }).map(f => f.properties.m2).sort((a, b) => a - b);
    if (!visible.length) { counter.textContent = 'brak ofert z ceną w widoku'; return; }
    const med = visible[Math.floor(visible.length / 2)];
    counter.textContent =
      `${visible.length} ofert w widoku · mediana ${fmt(med)} zł/m² · ` +
      `min ${fmt(visible[0])} · maks ${fmt(visible[visible.length - 1])}`;
  });
}
