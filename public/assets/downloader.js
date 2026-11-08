const API_ORIGIN = "https://web-dlp-api-17wm.onrender.com";
const $ = (selector) => document.querySelector(selector);
const result = $("#result");
const input = $("#url");
const detectBtn = $("#detect");

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function api(path, options = {}) {
  const response = await fetch(API_ORIGIN + path, {
    ...options,
    headers: {
      ...(options.body ? { "content-type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });

  let data = {};
  try {
    data = await response.json();
  } catch {
    // Some hosting errors return plain text instead of JSON.
  }

  if (!response.ok) {
    const message = typeof data.detail === "string"
      ? data.detail
      : response.status === 429
        ? "Too many requests. Please wait a little and try again."
        : "The service could not complete this request. Please try again.";
    throw new Error(message);
  }

  return data;
}

function showError(message) {
  result.hidden = false;
  result.innerHTML = `<div class="error" role="alert">${esc(message)}</div>`;
}

function showProgress(message, percent = null) {
  result.hidden = false;
  const suffix = Number.isFinite(percent) ? ` ${Math.max(0, Math.min(100, percent))}%` : "";
  result.innerHTML = `<div class="progress" role="status" aria-live="polite">${esc(message)}${suffix}</div>`;
}

async function startDownload(kind, quality) {
  const url = input.value.trim();
  if (!url) {
    showError("Paste a public media URL first.");
    input.focus();
    return;
  }

  const buttons = result.querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  showProgress("Adding your download to the queue…");

  try {
    const job = await api("/api/download", {
      method: "POST",
      body: JSON.stringify({ url, kind, quality }),
    });

    // Poll for up to 15 minutes. A stuck request should not leave the page
    // showing an endless spinner.
    const deadline = Date.now() + 15 * 60 * 1000;
    while (Date.now() < deadline) {
      await sleep(1200);
      const status = await api("/api/status/" + encodeURIComponent(job.id));

      if (status.status === "finished") {
        const fileUrl = API_ORIGIN + "/api/file/" + encodeURIComponent(job.id);
        result.innerHTML = `
          <div class="title">Your file is ready</div>
          <p class="small">Your download is ready. If it does not start, tap the button again.</p>
          <a href="${fileUrl}" download><button class="primary" type="button">Download file</button></a>
          <button class="option" id="new-download" type="button">Prepare another link</button>
        `;
        $("#new-download")?.addEventListener("click", () => {
          result.hidden = true;
          input.focus();
        });
        return;
      }

      if (status.status === "error") {
        throw new Error(status.error || "The download failed. Try another supported public link.");
      }

      const percent = Number(status.progress);
      showProgress(
        status.status === "queued" ? "Waiting for a free download slot…" : "Preparing your file…",
        Number.isFinite(percent) ? percent : null
      );
    }

    throw new Error("This download is taking too long. Please try again with the same public link later.");
  } catch (error) {
    showError(error.message || "The download failed. Please try again.");
  }
}

function renderOptions(data) {
  const qualities = Array.isArray(data.video) ? data.video : [];
  const extractor = data.extractor ? `Source: ${esc(data.extractor)}` : "Supported public media";
  const uploader = data.uploader ? ` · ${esc(data.uploader)}` : "";
  let html = `<div class="title">${esc(data.title || "Media found")}</div>
    <p class="small">${extractor}${uploader}</p>
    <p class="small">Choose the best available quality or a size that suits your device.</p>
    <div class="grid" aria-label="Download options">`;

  if (qualities.length) {
    html += '<button class="option" type="button" data-q="best">Best available MP4</button>';
    for (const quality of qualities) {
      if (/^\d+p$/.test(String(quality))) {
        html += `<button class="option" type="button" data-q="${esc(quality)}">${esc(quality)} MP4</button>`;
      }
    }
  } else {
    html += '<p class="small">No video quality list was found for this link. You can still try the MP3 option if the source supports audio extraction.</p>';
  }

  html += '<button class="option" type="button" data-audio="1">MP3 audio</button></div>';
  result.hidden = false;
  result.innerHTML = html;

  result.querySelectorAll("[data-q]").forEach((button) => {
    button.addEventListener("click", () => startDownload("video", button.dataset.q));
  });
  result.querySelector("[data-audio]")?.addEventListener("click", () => startDownload("audio", "best"));
}

detectBtn.addEventListener("click", async () => {
  const url = input.value.trim();
  if (!url) {
    showError("Paste a public media URL first.");
    input.focus();
    return;
  }

  detectBtn.disabled = true;
  detectBtn.textContent = "Checking link…";
  showProgress("Checking which download options are available…");

  try {
    const data = await api("/api/detect", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    renderOptions(data);
  } catch (error) {
    showError(error.message || "This link could not be checked. Make sure it is public and try again.");
  } finally {
    detectBtn.disabled = false;
    detectBtn.textContent = "Get download options";
  }
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter") detectBtn.click();
});

const presetUrl = new URLSearchParams(window.location.search).get("url");
if (presetUrl) {
  input.value = presetUrl;
  setTimeout(() => detectBtn.click(), 0);
}
