const STATE_LABELS = {
  PRESENT: "Present",
  TARDY: "Tardy",
  TEMP_OUT: "Stepped Out",
  LEFT_EARLY: "Left Early",
  ABSENT: "Absent",
  NOT_YET_ARRIVED: "Not Yet Arrived",
};

const SUMMARY_STATES = ["PRESENT", "TARDY", "TEMP_OUT", "ABSENT", "LEFT_EARLY"];

const previousStates = {};

async function poll() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    render(data);
  } catch (err) {
    console.error("Failed to fetch status", err);
  }
  setTimeout(poll, 2000);
}

function render(data) {
  const periodEl = document.getElementById("period-name");
  const metaEl = document.getElementById("period-meta");
  const timerEl = document.getElementById("period-timer");
  const rosterEl = document.getElementById("roster");
  const summaryEl = document.getElementById("summary");

  if (!data.period) {
    periodEl.textContent = "No class in session";
    metaEl.textContent = "";
    timerEl.textContent = "";
    rosterEl.innerHTML = "";
    summaryEl.innerHTML = "";
    return;
  }

  const start = new Date(data.period.start_time);
  const end = new Date(data.period.end_time);
  const fmt = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

  periodEl.textContent = data.period.name;
  metaEl.innerHTML = `${fmt(start)} &ndash; ${fmt(end)} <span class="source-pill source-${data.period.source}">${
    data.period.source === "bell" ? "Live from bell schedule" : "Manually started"
  }</span>`;
  timerEl.textContent = formatRemaining(data.period.seconds_remaining);

  renderSummary(summaryEl, data.students);

  rosterEl.innerHTML = "";
  data.students.forEach((student) => {
    const tr = document.createElement("tr");
    const since = student.since
      ? new Date(student.since).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
      : "—";
    tr.innerHTML = `
      <td>${student.name}</td>
      <td><span class="pill pill-${student.state}">${STATE_LABELS[student.state] || student.state}</span></td>
      <td class="since">${since}</td>
    `;
    rosterEl.appendChild(tr);

    const prev = previousStates[student.id];
    if (
      prev &&
      prev !== student.state &&
      (student.state === "ABSENT" || student.state === "LEFT_EARLY")
    ) {
      showAlert(student.name, STATE_LABELS[student.state]);
    }
    previousStates[student.id] = student.state;
  });
}

function renderSummary(el, students) {
  const counts = {};
  SUMMARY_STATES.forEach((s) => (counts[s] = 0));
  students.forEach((s) => {
    if (counts[s.state] !== undefined) counts[s.state]++;
  });

  el.innerHTML = SUMMARY_STATES.map(
    (state) => `
      <div class="stat stat-${state}">
        <div class="stat-count">${counts[state]}</div>
        <div class="stat-label">${STATE_LABELS[state]}</div>
      </div>
    `
  ).join("");
}

function formatRemaining(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")} remaining`;
}

function showAlert(name, state) {
  const alertEl = document.getElementById("alert");
  alertEl.textContent = `${name} is now ${state}`;
  alertEl.classList.remove("hidden");
  setTimeout(() => alertEl.classList.add("hidden"), 4000);
}

poll();
