const STATE_LABELS = {
  PRESENT: "Present",
  TARDY: "Tardy",
  TEMP_OUT: "Stepped Out",
  LEFT_EARLY: "Left Early",
  ABSENT: "Absent",
  NOT_YET_ARRIVED: "Not Yet Arrived",
};

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
  const timerEl = document.getElementById("period-timer");
  const rosterEl = document.getElementById("roster");

  if (!data.period) {
    periodEl.textContent = "No active period";
    timerEl.textContent = "";
    rosterEl.innerHTML = "";
    return;
  }

  periodEl.textContent = data.period.name;
  timerEl.textContent = `${data.period.seconds_remaining}s remaining`;

  rosterEl.innerHTML = "";
  data.students.forEach((student) => {
    const card = document.createElement("div");
    card.className = `card state-${student.state}`;
    card.innerHTML = `<div class="name">${student.name}</div><div class="state">${
      STATE_LABELS[student.state] || student.state
    }</div>`;
    rosterEl.appendChild(card);

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

function showAlert(name, state) {
  const alertEl = document.getElementById("alert");
  alertEl.textContent = `${name} is now ${state}`;
  alertEl.classList.remove("hidden");
  setTimeout(() => alertEl.classList.add("hidden"), 4000);
}

poll();
