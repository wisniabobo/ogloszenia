/* Monitor rynku — warstwa interakcji.
   Celowo bez frameworka: strona ma się otwierać natychmiast, także na słabym
   łączu, a tyle kodu spokojnie wystarcza. */

function toggleNav() {
  document.getElementById("mainnav")?.classList.toggle("is-open");
}

/* Numer telefonu odsłaniamy na kliknięcie, nie hurtem przy ładowaniu listy.
   Dzięki temu jedno wejście na stronę nie pobiera setek cudzych numerów. */
async function revealContact(button, listingId) {
  if (button.dataset.open === "1") return;
  const before = button.textContent;
  button.textContent = "…";
  try {
    const resp = await fetch(`/api/listings/${listingId}/phone`);
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    const numbers = (data.phones || []).map((p) => p.national || p.e164).filter(Boolean);
    if (numbers.length) {
      button.textContent = numbers.join(" · ");
    } else {
      // Numeru nie ma w ogłoszeniu — pokazujemy centralę biura, jeśli ją znamy.
      const alt = await fetch(`/api/listings/${listingId}/kontakt`);
      const info = alt.ok ? await alt.json() : { kontakty: [] };
      const office = (info.kontakty || []).find((k) => !k.z_ogloszenia);
      button.textContent = office ? office.numer : before;
    }
    button.classList.add("is-open");
    button.dataset.open = "1";
  } catch {
    button.textContent = "niedostępny";
  }
}

/* Zapis (schowek, alerty) należy do właściciela instancji. Publiczna kopia bez
   hasła jest tylko do czytania — wtedy kierujemy na formularz hasła zamiast
   udawać, że klik się udał. */
function handleWriteError(resp) {
  if (resp.status === 401 || resp.status === 403) {
    if (confirm("Zapis wymaga hasła właściciela tej instancji. Przejść do logowania?")) {
      window.location.href = "/wejscie";
    }
    return true;
  }
  return false;
}

async function toggleFavorite(button, listingId) {
  const on = button.dataset.on === "1";
  const resp = await fetch(`/api/favorites/${listingId}`, { method: on ? "DELETE" : "POST" });
  if (!resp.ok) {
    handleWriteError(resp);
    return;
  }
  button.dataset.on = on ? "0" : "1";
  button.textContent = on ? "☆ Zapisz" : "★ Zapisane";
  button.classList.toggle("btn--ghost", !on);
}

/* Bieżące filtry zapisujemy jako alert — dokładnie to, co widać na ekranie. */
async function saveSearch() {
  const name = prompt("Jak nazwać to poszukiwanie?");
  if (!name) return;
  const query = {};
  for (const [k, v] of new URLSearchParams(window.location.search)) {
    if (v !== "" && k !== "page") query[k] = v;
  }
  const resp = await fetch("/api/searches", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, query, channels: ["telegram"], only_original: true }),
  });
  if (!resp.ok && handleWriteError(resp)) return;
  alert(resp.ok
    ? "Zapisane. Nowe pasujące oferty przyjdą powiadomieniem."
    : "Nie udało się zapisać poszukiwania.");
}

/* Działka ewidencyjna z rejestru GUGiK — na żądanie, bo to zapytanie do
   cudzego serwera i nie ma powodu robić go przy każdym otwarciu strony. */
async function loadParcel(button, listingId) {
  const box = document.getElementById("parcel-result");
  button.disabled = true;
  button.textContent = "sprawdzam…";
  try {
    const resp = await fetch(`/api/listings/${listingId}/dzialka`);
    const data = await resp.json();
    if (!data.dzialka) {
      box.innerHTML = `<p class="muted">${data.info || "Nie udało się ustalić działki."}</p>`;
    } else {
      const p = data.dzialka;
      box.innerHTML = `<table class="table"><tbody>
        <tr><th>Identyfikator działki</th><td><code>${p.identyfikator}</code></td></tr>
        ${p.obreb ? `<tr><th>Obręb</th><td>${p.obreb}</td></tr>` : ""}
      </tbody></table>`;
    }
  } catch {
    box.innerHTML = `<p class="muted">Rejestr nie odpowiedział. Spróbuj ponownie później.</p>`;
  }
  button.disabled = false;
  button.textContent = "Sprawdź działkę pod tym adresem";
}

/* Przełączniki filtrów wysyłają formularz od razu — bez szukania przycisku. */
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".toggle input[type=checkbox]").forEach((box) => {
    box.addEventListener("change", () => box.form?.submit());
  });
});

document.addEventListener("click", (event) => {
  const nav = document.getElementById("mainnav");
  if (window.innerWidth <= 720 && nav?.classList.contains("is-open")
      && !nav.contains(event.target) && !event.target.closest(".burger")) {
    nav.classList.remove("is-open");
  }
});

/* Galeria na stronie oferty: miniatura podmienia duże zdjęcie. */
function showPhoto(thumb) {
  const main = document.getElementById("gallery-main");
  if (!main) return;
  main.src = thumb.dataset.src;
  main.closest(".gallery").classList.remove("gallery--broken");
  document.querySelectorAll(".gallery__thumb.is-on").forEach((t) => t.classList.remove("is-on"));
  thumb.classList.add("is-on");
}

/* Na telefonie panel filtrów jest zwinięty pod przyciskiem „Filtry". */
function toggleFilters(button) {
  const form = document.getElementById("filters-form");
  const open = form.classList.toggle("is-open");
  button.setAttribute("aria-expanded", open ? "true" : "false");
}

/* Pole „Ulica" podpowiada ulice, przy których są oferty — w wybranej
   miejscowości, jeśli jest wpisana. Pobieramy po chwili bezczynności. */
document.addEventListener("DOMContentLoaded", () => {
  const input = document.querySelector("input[data-streets]");
  const list = document.getElementById("streets");
  if (!input || !list) return;
  const city = input.form?.querySelector("[name=city]");
  let timer = null;
  let last = "";
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      const key = `${city?.value || ""}|${q}`;
      if (q.length < 2 || key === last) return;
      last = key;
      const params = new URLSearchParams({ q, limit: "20" });
      if (city?.value) params.set("city", city.value);
      try {
        const res = await fetch(`/api/ulice?${params}`);
        if (!res.ok) return;
        const data = await res.json();
        list.replaceChildren(...data.items.map((item) => {
          const option = document.createElement("option");
          option.value = item.name;
          option.label = `${item.count} ofert`;
          return option;
        }));
      } catch (_) { /* brak podpowiedzi nie blokuje wyszukiwania */ }
    }, 250);
  });
});

/* Usunięcie alertu — z obsługą braku hasła zapisu. */
async function deleteSearch(searchId) {
  const resp = await fetch(`/api/searches/${searchId}`, { method: "DELETE" });
  if (!resp.ok) {
    if (!handleWriteError(resp)) alert("Nie udało się usunąć poszukiwania.");
    return;
  }
  location.reload();
}
