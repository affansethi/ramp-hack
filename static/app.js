async function loadFiles() {
  const list = document.getElementById("files");
  try {
    const res = await fetch("/api/files");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const files = await res.json();
    list.innerHTML = "";
    if (files.length === 0) {
      list.innerHTML = '<li class="muted">No files yet.</li>';
      return;
    }
    for (const name of files) {
      const li = document.createElement("li");
      li.textContent = name;
      list.appendChild(li);
    }
  } catch (err) {
    list.innerHTML = "";
    const li = document.createElement("li");
    li.className = "error";
    li.textContent = `Failed to load files: ${err.message}`;
    list.appendChild(li);
  }
}

document.addEventListener("DOMContentLoaded", loadFiles);
