"use strict";

const $ = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const labels = {request_id:"Request ID",request_date:"Request date",kind:"Request type",requester:"Requester",team:"Team",system:"System",environment:"Environment",permission:"Permission",business_purpose:"Business purpose",start_date:"Start date",end_date:"End date",manager:"Manager contact"};
const fieldOrder = ["request_id","request_date","kind","requester","team","system","environment","permission","start_date","end_date","manager","business_purpose"];
const actions = {"clarify":"Clarify request details","inspect-message":"Inspect the message","confirm-withdrawal":"Confirm the withdrawal","review-risk-then-owner":"Review risk, then owner","review-request":"Review the request","retry-extraction":"Retry extraction"};
let files = [], source = null, lastResult = null, busy = false, model = "OpenAI";

function notice(message) {
  $("notice").textContent = message || "";
  $("notice").classList.toggle("hidden", !message);
}

async function api(path, options = {}) {
  const response = await fetch(path, {cache:"no-store", ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "The local server could not complete the request.");
  return body;
}

function controls(processing, showProgress = false) {
  busy = processing;
  $("file-select").disabled = processing || !files.length;
  $("provider-select").disabled = processing;
  $("run-button").disabled = processing || !source;
  document.querySelectorAll(".scenario").forEach(button => {
    button.disabled = processing || !files.some(file => file.document_id === button.dataset.file);
  });
  $("progress").classList.toggle("hidden", !showProgress);
  $("run-label").textContent = showProgress ? "Processing…" : lastResult?.error ? "Retry analysis" : $("provider-select").value === "openai" ? "Run live extraction" : "Run analysis";
}

function inputTab(kind) {
  for (const name of ["message", "json"]) {
    const active = name === kind;
    $("input-" + name).classList.toggle("hidden", !active);
    $("input-" + name + "-tab").classList.toggle("active", active);
    $("input-" + name + "-tab").setAttribute("aria-selected", String(active));
  }
}

function outputTab(kind) {
  const facts = kind === "facts";
  $("facts-view").classList.toggle("hidden", !facts);
  $("output-json").classList.toggle("hidden", facts);
  for (const name of ["facts", "json"]) {
    const active = name === kind;
    $("output-" + name + "-tab").classList.toggle("active", active);
    $("output-" + name + "-tab").setAttribute("aria-selected", String(active));
  }
}

function emptyOutput() {
  lastResult = null;
  $("output-empty").classList.remove("hidden");
  $("output-content").classList.add("hidden");
  $("output-origin").classList.add("hidden");
  $("evidence-section").open = false;
}

async function loadSource(id) {
  if (busy) return;
  source = null;
  notice("");emptyOutput();controls(true);
  $("input-message").textContent = "Loading the original message…";
  try {
    source = await api("/api/source?document_id=" + encodeURIComponent(id));
    $("file-select").value = source.document_id;
    $("source-subject").textContent = source.subject;
    $("source-filename").textContent = source.document_id;
    $("channel").textContent = source.channel.replaceAll("_", " ").toUpperCase();
    $("input-message").textContent = source.message || "This export has no readable message string. Inspect Raw JSON for details.";
    $("input-json").textContent = source.raw_text;
    $("source-hash").textContent = "SHA-256 · " + source.source_sha256.slice(0,18) + "…";
    $("source-hash").title = source.source_sha256;
    document.querySelectorAll(".scenario").forEach(button => button.classList.toggle("selected", button.dataset.file === id));
    inputTab("message");
  } catch (error) {
    $("input-message").textContent = "Select a request to inspect its original message.";
    notice(error.message);
  } finally { controls(false); }
}

function highlight(quote) {
  if (!source) return;
  const index = source.message.indexOf(quote);
  if (index < 0) return;
  inputTab("message");
  const mark = document.createElement("mark");mark.textContent = quote;
  $("input-message").replaceChildren(document.createTextNode(source.message.slice(0,index)),mark,document.createTextNode(source.message.slice(index+quote.length)));
  mark.scrollIntoView({behavior:matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block:"center"});
}

function readableIssue(issue) {
  let text = issue.replace(/^(missing|conflict):([^:]+):\s*/, (_, type, field) => (labels[field] || field) + ": ")
    .replace(/^instruction-override:\s*/, "Instruction override: ")
    .replace(/^identifier-mismatch:\s*/, "Identifier: ")
    .replace(/^invalid-range:\s*/, "Access window: ");
  for (const field of fieldOrder) text = text.replaceAll(field, field.replaceAll("_", " "));
  return text;
}

function render(job) {
  const result = job.result;
  if (result.document_id !== source.document_id || result.source_sha256 !== source.source_sha256) throw new Error("The result belongs to a different source revision. Reload and run this request again.");
  lastResult = result;
  $("output-empty").classList.add("hidden");$("output-content").classList.remove("hidden");
  const origin = $("output-origin");origin.classList.remove("hidden");
  origin.textContent = (result.response_source === "mock" ? "MOCK" : "LIVE") + (result.error ? " ATTEMPT" : " RESPONSE");
  const status = $("result-status");
  status.textContent = result.status === "error" ? "Extraction needs attention" : result.status === "needs-information" ? "Needs information" : "Review required";
  status.className = "result-status" + (result.error ? " error" : result.status === "needs-information" ? " warning" : "");
  $("cache-label").textContent = job.reused ? "Reused unchanged result" : "Just processed";
  $("result-owner").textContent = result.owner;
  $("result-action").textContent = actions[result.next_action] || result.next_action;
  $("result-error").classList.toggle("hidden", !result.error);
  $("result-error").textContent = result.error?.message || "";
  $("output-json").textContent = JSON.stringify(result, null, 2);
  $("facts-grid").replaceChildren();$("quotes-list").replaceChildren();$("issues-list").replaceChildren();
  const extraction = result.extraction;
  const issues = extraction?.issues || [];
  $("issues-section").classList.toggle("hidden", !issues.length);
  issues.forEach(issue => { const item = document.createElement("li");item.textContent = readableIssue(issue);$("issues-list").append(item); });
  $("evidence-section").classList.toggle("hidden", !extraction);
  if (extraction) {
    for (const field of fieldOrder) {
      const cell = document.createElement("div");cell.className = "fact" + (field === "business_purpose" ? " wide" : "");
      const label = document.createElement("div");label.className = "fact-label";label.textContent = labels[field];
      const value = document.createElement("div");value.className = "fact-value" + (extraction[field] === null ? " missing" : "");
      value.textContent = extraction[field] === null ? "Not provided / unresolved" : String(extraction[field]);
      cell.append(label,value);$("facts-grid").append(cell);
    }
    $("quote-count").textContent = String(extraction.supporting_quotes.length);
    extraction.supporting_quotes.forEach(quote => {
      const button = document.createElement("button");button.className = "quote-button";button.textContent = quote;
      button.title = "Highlight this quote in the original message";button.addEventListener("click", () => highlight(quote));
      $("quotes-list").append(button);
    });
  }
  outputTab("facts");
}

async function run() {
  if (!source || busy) return;
  const retry = Boolean(lastResult?.error);
  notice("");emptyOutput();controls(true,true);
  try {
    const started = await api("/api/process", {method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({document_id:source.document_id,provider:$("provider-select").value,source_sha256:source.source_sha256,retry})});
    while (true) {
      const job = await api("/api/jobs/" + encodeURIComponent(started.job_id));
      if (job.state === "error") throw new Error(job.error);
      if (job.state === "complete") {render(job);break;}
      await new Promise(resolve => setTimeout(resolve,250));
    }
  } catch (error) { notice(error.message); }
  finally { controls(false); }
}

$("run-button").addEventListener("click",run);
$("file-select").addEventListener("change",event => loadSource(event.target.value));
$("provider-select").addEventListener("change",() => {
  emptyOutput();notice("");
  $("provider-note").textContent = $("provider-select").value === "mock" ? "Saved responses. No API call." : "Server-side provider · " + model;
  controls(false);
});
for (const kind of ["message","json"]) $("input-"+kind+"-tab").addEventListener("click",()=>inputTab(kind));
for (const kind of ["facts","json"]) $("output-"+kind+"-tab").addEventListener("click",()=>outputTab(kind));
document.querySelectorAll(".scenario").forEach(button => button.addEventListener("click",()=>loadSource(button.dataset.file)));

async function initialize() {
  try {
    const [available,config] = await Promise.all([api("/api/files"),api("/api/config")]);
    files = available;model = config.model;
    $("file-count").textContent = "("+files.length+" files)";
    $("file-select").replaceChildren();
    files.forEach(file => { const option=document.createElement("option");option.value=file.document_id;option.textContent=file.document_id+" — "+file.subject;$("file-select").append(option); });
    if (!files.length) {notice("No JSON exports were found in this input directory.");controls(false);return;}
    await loadSource(files[0].document_id);
  } catch (error) {notice("Unable to load the local workspace. "+error.message);controls(false);}
}
initialize();
