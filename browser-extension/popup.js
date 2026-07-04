// YT Buzz Cookie Helper — Popup Script

const syncBtn = document.getElementById("syncBtn");
const lastSyncEl = document.getElementById("lastSync");
const cookieCountEl = document.getElementById("cookieCount");
const autoSyncToggle = document.getElementById("autoSyncToggle");
const messageEl = document.getElementById("message");

function showMessage(text, type) {
  messageEl.textContent = text;
  messageEl.className = `message show ${type}`;
  setTimeout(() => { messageEl.className = "message"; }, 4000);
}

function formatTime(isoString) {
  if (!isoString) return "Never";
  const d = new Date(isoString);
  return d.toLocaleString();
}

// Load status
chrome.runtime.sendMessage({ action: "getStatus" }, (data) => {
  if (data) {
    lastSyncEl.textContent = formatTime(data.lastSync);
    cookieCountEl.textContent = data.cookieCount || "0";
    if (data.autoSync) {
      autoSyncToggle.classList.add("active");
    }
  }
});

// Sync button
syncBtn.addEventListener("click", async () => {
  syncBtn.disabled = true;
  syncBtn.textContent = "Syncing...";

  chrome.runtime.sendMessage({ action: "syncCookies" }, (response) => {
    syncBtn.disabled = false;
    syncBtn.textContent = "Sync YouTube Cookies Now";

    if (response && response.success) {
      showMessage(response.message, "success");
      lastSyncEl.textContent = formatTime(response.lastSync);
      cookieCountEl.textContent = response.cookieCount || "0";
    } else {
      showMessage(response ? response.message : "Unknown error", "error");
    }
  });
});

// Auto-sync toggle
autoSyncToggle.addEventListener("click", () => {
  const isActive = autoSyncToggle.classList.toggle("active");
  chrome.runtime.sendMessage({ action: "setAutoSync", value: isActive });
});
