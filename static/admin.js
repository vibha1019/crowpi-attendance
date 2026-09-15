async function poll() {
  try {
    const res = await fetch("/api/admin/overview");
    const data = await res.json();
    render(data);
  } catch (err) {
    console.error("Failed to fetch admin overview", err);
  }
  setTimeout(poll, 2000);
}

function render(data) {
  renderPeriod(data.period);
  renderPending(data.pending);
  renderRoster(data.roster);
}

function renderPeriod(period) {
  const el = document.getElementById("period-status");
  if (!period) {
    el.textContent = "No active period.";
    return;
  }
  el.textContent = `Active: ${period.name} (started ${new Date(period.start_time).toLocaleTimeString()})`;
}

function renderPending(pending) {
  const el = document.getElementById("pending-list");
  if (!pending.length) {
    el.innerHTML = '<p class="hint">No new tags waiting.</p>';
    return;
  }
  el.innerHTML = "";
  pending.forEach((tag) => {
    const row = document.createElement("div");
    row.className = "pending-row";
    row.innerHTML = `
      <span class="tag-uid">${tag.tag_uid}</span>
      <input type="text" placeholder="Student name" data-id="${tag.id}">
      <button data-id="${tag.id}">Save</button>
    `;
    const input = row.querySelector("input");
    const button = row.querySelector("button");
    const save = () => nameTag(tag.id, input.value.trim());
    button.addEventListener("click", save);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") save();
    });
    el.appendChild(row);
  });
}

function renderRoster(roster) {
  const tbody = document.querySelector("#roster-table tbody");
  tbody.innerHTML = "";
  roster.forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${s.name}</td><td>${s.tag_uid}</td>`;
    tbody.appendChild(tr);
  });
}

async function nameTag(id, name) {
  if (!name) {
    showToast("Enter a name first");
    return;
  }
  const res = await fetch("/api/admin/name_tag", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, name }),
  });
  if (res.ok) {
    showToast(`Registered ${name}`);
  } else {
    showToast("Could not register tag");
  }
}

document.getElementById("period-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = document.getElementById("period-name").value.trim();
  const minutes = Number(document.getElementById("period-duration").value);
  const graceMinutes = Number(document.getElementById("period-grace").value);

  const res = await fetch("/api/admin/period/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      duration_seconds: minutes * 60,
      grace_seconds: graceMinutes * 60,
    }),
  });
  showToast(res.ok ? `Started ${name}` : "Could not start period");
});

function showToast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 3000);
}

poll();
