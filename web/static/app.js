"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  fromLang: $("from-lang"),
  toLang: $("to-lang"),
  source: $("source"),
  output: $("output"),
  ir: $("ir"),
  warnings: $("warnings"),
  status: $("status"),
  translate: $("translate"),
  loadSample: $("load-sample"),
  copy: $("copy"),
  download: $("download"),
  themeToggle: $("theme-toggle"),
};

const THEME_KEY = "qbabel-theme";

let languages = [];
let samples = {};
let requestId = 0;
let timer = null;

const EXTENSIONS = {
  qiskit: "py", cirq: "py", pennylane: "py", braket: "py", pytket: "py",
  pyquil: "py", qulacs: "py", qsharp: "qs", cudaq: "cpp", openqasm: "qasm",
};

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  els.themeToggle.textContent = theme === "light" ? "dark mode" : "light mode";
  els.themeToggle.setAttribute("aria-pressed", String(theme === "light"));
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch (err) { /* private mode: the theme just will not persist */ }
}

function setStatus(text) {
  els.status.textContent = text;
}

function fillSelect(select, selected) {
  select.innerHTML = "";
  for (const lang of languages) {
    const option = document.createElement("option");
    option.value = lang;
    option.textContent = lang;
    if (lang === selected) option.selected = true;
    select.appendChild(option);
  }
}

function showWarnings(list) {
  if (!list || list.length === 0) {
    els.warnings.hidden = true;
    els.warnings.textContent = "";
    return;
  }
  els.warnings.hidden = false;
  els.warnings.innerHTML = "<strong>notes</strong><ul>" +
    list.map((w) => `<li>${escapeHtml(w)}</li>`).join("") + "</ul>";
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function translateNow() {
  const id = ++requestId;
  const source = els.source.value;
  const fromLang = els.fromLang.value;
  const toLang = els.toLang.value;

  if (!source.trim()) {
    els.output.textContent = "";
    els.ir.textContent = "";
    showWarnings([]);
    setStatus("ready");
    return;
  }

  setStatus("translating…");
  try {
    const response = await fetch("/api/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, from_lang: fromLang, to_lang: toLang }),
    });
    const data = await response.json();
    if (id !== requestId) return;
    if (!response.ok) {
      els.output.textContent = "";
      els.ir.textContent = "";
      showWarnings([data.error || "translation failed"]);
      setStatus("failed");
      return;
    }
    els.output.textContent = data.code;
    els.ir.textContent = data.ir ? JSON.stringify(data.ir, null, 2) : "";
    showWarnings(data.warnings);
    const count = data.ir ? data.ir.program.operations.length : 0;
    setStatus(`${fromLang} \u2192 ${toLang} · ${count} operation${count === 1 ? "" : "s"}`);
  } catch (err) {
    if (id !== requestId) return;
    showWarnings([String(err)]);
    setStatus("failed");
  }
}

function scheduleTranslate() {
  clearTimeout(timer);
  timer = setTimeout(translateNow, 250);
}

function loadSampleForCurrentLanguage() {
  const lang = els.fromLang.value;
  if (samples[lang] !== undefined) {
    els.source.value = samples[lang];
    translateNow();
  }
}

function downloadOutput() {
  const lang = els.toLang.value;
  const blob = new Blob([els.output.textContent], { type: "text/plain" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `circuit.${EXTENSIONS[lang] || "txt"}`;
  link.click();
  URL.revokeObjectURL(link.href);
}

async function init() {
  const [langsRes, samplesRes] = await Promise.all([
    fetch("/api/languages").then((r) => r.json()),
    fetch("/api/samples").then((r) => r.json()),
  ]);
  languages = langsRes.languages;
  samples = samplesRes.samples;

  fillSelect(els.fromLang, "qiskit");
  fillSelect(els.toLang, "qsharp");
  els.source.value = samples.qiskit || "";

  els.source.addEventListener("input", scheduleTranslate);
  els.fromLang.addEventListener("change", () => {
    if (!els.source.value.trim()) loadSampleForCurrentLanguage();
    else translateNow();
  });
  els.toLang.addEventListener("change", translateNow);
  els.translate.addEventListener("click", translateNow);
  els.loadSample.addEventListener("click", loadSampleForCurrentLanguage);
  els.download.addEventListener("click", downloadOutput);
  els.themeToggle.addEventListener("click", () => {
    applyTheme(currentTheme() === "light" ? "dark" : "light");
  });
  els.copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(els.output.textContent);
      setStatus("copied to clipboard");
    } catch (err) {
      setStatus("copy failed");
    }
  });

  applyTheme(currentTheme());
  translateNow();
}

init();
