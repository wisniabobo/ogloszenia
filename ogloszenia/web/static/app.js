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

async function toggleFavorite(button, listingId) {
  const on = button.dataset.on === "1";
  const resp = await fetch(`/api/favorites/${listingId}`, { method: on ? "DELETE" : "POST" });
  if (!resp.ok) return;
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
