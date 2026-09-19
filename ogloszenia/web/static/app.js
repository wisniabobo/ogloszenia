// Odkrywanie numeru telefonu — świadoma akcja, jedno kliknięcie = jeden numer.
async function revealPhone(el, listingId) {
  if (el.dataset.revealed === "1") return;
  el.textContent = "…";
  try {
    const resp = await fetch(`/api/listings/${listingId}/phone`);
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    const numbers = (data.phones || []).map(p => p.national || p.e164).filter(Boolean);
    el.textContent = numbers.length ? numbers.join(" · ") : "brak numeru";
    el.classList.add("revealed");
    el.dataset.revealed = "1";
  } catch (err) {
    el.textContent = "niedostępny";
    el.title = String(err);
  }
}

async function toggleFavorite(btn, listingId) {
  const on = btn.dataset.on === "1";
  const method = on ? "DELETE" : "POST";
  const resp = await fetch(`/api/favorites/${listingId}`, { method });
  if (resp.ok) {
    btn.dataset.on = on ? "0" : "1";
    btn.textContent = on ? "☆ Do schowka" : "★ W schowku";
  }
}

function toggleSidebar() {
  document.querySelector(".sidebar").classList.toggle("open");
}

// Zapis bieżących filtrów jako poszukiwanie z alertem
async function saveSearch() {
  const name = prompt("Nazwa poszukiwania:");
  if (!name) return;
  const params = new URLSearchParams(window.location.search);
  const query = {};
  for (const [k, v] of params.entries()) {
    if (v !== "" && k !== "page") query[k] = v;
  }
  const resp = await fetch("/api/searches", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, query, channels: ["telegram"], only_original: true }),
  });
  alert(resp.ok ? "Zapisano. Nowe trafienia przyjdą powiadomieniem." : "Nie udało się zapisać.");
}

document.addEventListener("click", (e) => {
  const sidebar = document.querySelector(".sidebar");
  if (window.innerWidth <= 860 && sidebar?.classList.contains("open")
      && !sidebar.contains(e.target) && !e.target.closest(".burger")) {
    sidebar.classList.remove("open");
  }
});
