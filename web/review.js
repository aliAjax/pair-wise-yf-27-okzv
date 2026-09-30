// 审查工作台脚本：建包、补材料、封存、查看失效原因、复审、流转作业与重试
let state = {object: null};

function uid() { return document.getElementById("user").value; }
function objectId() { return parseInt(document.getElementById("objectId").value, 10); }

function msg(text, cls = "") {
  const el = document.getElementById("msg");
  el.className = cls;
  el.textContent = text;
}

async function api(path, options = {}) {
  const headers = Object.assign({"X-User-Id": uid(), "Content-Type": "application/json"}, options.headers || {});
  const res = await fetch(path, Object.assign({}, options, {headers}));
  const data = await res.json();
  if (!res.ok) throw Object.assign(new Error(data.error ? data.error.message : "请求失败"),
                                   {code: data.error && data.error.code, status: res.status});
  return data;
}

async function loadAll() {
  try {
    state.object = await api(`/api/objects/${objectId()}`);
    msg("", "");
    renderObject();
  } catch (e) {
    msg(`载入失败：${e.message}`, "err");
  }
}

function renderObject() {
  const o = state.object;
  document.getElementById("objectPanel").hidden = false;
  document.getElementById("objectTitle").textContent = `#${o.id} ${o.inventory_no} · ${o.title}`;
  document.getElementById("objectMeta").textContent =
    `当前藏品版本 v${o.version} ｜ 持有人：${o.current_holder || "-"}`;

  const publicEvents = (o.events || []).filter(e => e.visibility === "public");
  document.getElementById("eventList").innerHTML = publicEvents.length
    ? publicEvents.map(e => `<label><input type="checkbox" class="pick-event" value="${e.id}">
        #${e.id} ${e.event_type} ${e.date_start} ${e.place} — ${e.description}</label>`).join("<br>")
    : "<p class='muted'>暂无公开来源事件</p>";

  const internalEvidence = (o.unlinked_evidence || []).filter(e => e.visibility === "internal")
    .concat(...(o.events || []).map(e => (e.evidence || []).filter(x => x.visibility === "internal")));
  document.getElementById("evidenceList").innerHTML = internalEvidence.length
    ? internalEvidence.map(e => `<label><input type="checkbox" class="pick-evidence" value="${e.id}">
        #${e.id} ${e.filename}（SHA-256 ${(e.sha256 || "").slice(0, 12)}…，${e.size}B）</label>`).join("<br>")
    : "<p class='muted'>暂无内部证据</p>";

  document.getElementById("claimList").innerHTML = (o.claims || []).map(c =>
    `<div>#${c.id} ${c.claimed_by} → 当前阶段 <b>${c.status}</b><div class="muted">${c.desired_outcome}</div></div>`
  ).join("") || "<p class='muted'>暂无主张</p>";

  renderPackages(o.packages || []);
  renderJobs(o.jobs || []);
}

function selectedItems() {
  const events = [...document.querySelectorAll(".pick-event:checked")].map(c => ({item_type: "event", ref_id: parseInt(c.value, 10)}));
  const evidence = [...document.querySelectorAll(".pick-evidence:checked")].map(c => ({item_type: "evidence", ref_id: parseInt(c.value, 10)}));
  return events.concat(evidence);
}

async function createPackage() {
  try {
    const items = selectedItems();
    if (!items.length) return msg("请先勾选公开来源事件和/或内部证据", "err");
    await api(`/api/objects/${objectId()}/packages`, {method: "POST", body: JSON.stringify({items})});
    msg("依据包已创建（草稿）", "ok");
    await loadAll();
  } catch (e) { msg(`建包失败：${e.message}`, "err"); }
}

function renderPackages(packages) {
  document.getElementById("packageList").innerHTML = packages.map(p => {
    const items = p.items.map(i => {
      const d = i.detail || {};
      const name = i.item_type === "event"
        ? `事件 #${i.ref_id} ${d.event_type || ""} ${d.date_start || ""} ${d.place || ""}`
        : `证据 #${i.ref_id} ${d.filename || ""}`;
      return `<li>${i.item_type === "event" ? "📜" : "🔒"} ${name}${i.available ? "" : " <span class='err'>（材料已不存在）</span>"}</li>`;
    }).join("");
    let body = `<ul>${items}</ul>`;
    if (p.sealed_at) {
      body += `<div class="muted">封存人 ${p.sealed_by} ｜ 封存时间 ${p.sealed_at}<br>
               封存藏品版本 v${p.sealed_object_version} ｜ 现行版本 v${p.current_object_version || "-"}</div>`;
      if (p.evidence_summary && p.evidence_summary.length) {
        body += `<details><summary class="muted">证据摘要（${p.evidence_summary.length} 条 SHA-256）</summary><pre>${
          p.evidence_summary.map(e => `${e.evidence_id} ${e.filename}\n${e.sha256}`).join("\n")}</pre></details>`;
      }
    }
    if (p.status === "draft") {
      body += `<div class="row">
        <button onclick="addItems(${p.id}, ${p.revision})">补入勾选材料</button>
        <button class="primary" onclick="seal(${p.id}, ${p.revision})">封存（revision ${p.revision}）</button></div>`;
    }
    if (p.status === "sealed" && p.basis_valid === false) {
      body += `<div class="err">依据已失效：${p.invalid_reason}</div>`;
    }
    if (p.status === "sealed" && p.basis_valid === true) {
      body += `<div class="ok">✓ 依据现行有效，可推动主张流转</div>` + transitionForm(p);
    }
    if (p.status === "invalid") {
      body += `<div class="err"><b>失效原因：${p.invalidation_reason || "未知"}</b>（${p.invalidation_at || ""}）</div>`;
      if (p.affected_items && p.affected_items.length) {
        body += `<details open><summary>受影响、未执行的主张流转（${p.affected_items.length}）</summary><ul>${
          p.affected_items.map(a => `<li>作业 #${a.job_id} 项 #${a.item_id}：主张 ${a.claim_id} → ${a.to_status}
            <span class="tag tag-${a.status}">${a.status}</span>
            <span class="muted">${a.last_error || ""}</span></li>`).join("")}</ul></details>`;
      }
      body += `<div class="row"><button class="primary" onclick="review(${p.id})">复审：按原条目另起草稿包</button></div>`;
    }
    return `<div class="card pkg-${p.status}">
      <span class="tag tag-${p.status}">${p.status}</span> 依据包 #${p.id} <span class="muted">revision ${p.revision}</span>
      ${p.reopens_package_id ? `<span class="muted">复审自 #${p.reopens_package_id}</span>` : ""}
      ${body}</div>`;
  }).join("") || "<p class='muted'>还没有依据包</p>";
}

function transitionForm(p) {
  const claims = (state.object.claims || []);
  const opts = claims.map(c => `<option value="${c.id}">#${c.id} ${c.claimed_by}（${c.status}）</option>`).join("");
  return `<div class="row">
    <select id="ts-claim-${p.id}">${opts}</select>
    <select id="ts-target-${p.id}">
      <option>under_review</option><option>negotiating</option>
      <option>resolved_return</option><option>rejected</option>
    </select>
    <input id="ts-note-${p.id}" placeholder="审查说明（至少5字）" style="width:220px">
    <button onclick="runTransitions(${p.id})">依据本包流转</button>
  </div>`;
}

async function addItems(packageId, revision) {
  try {
    const items = selectedItems();
    if (!items.length) return msg("请勾选要补入的材料", "err");
    await api(`/api/packages/${packageId}/items`,
      {method: "POST", body: JSON.stringify({items, expected_revision: revision})});
    msg("材料已补入，revision 已推进", "ok");
    await loadAll();
  } catch (e) { msg(`补材料失败：${e.message}${e.code === "package_revision_conflict" ? "（他人先写入，请刷新）" : ""}`, "err"); }
}

async function seal(packageId, revision) {
  try {
    await api(`/api/packages/${packageId}/seal`,
      {method: "POST", body: JSON.stringify({expected_revision: revision})});
    msg("封存成功：已记录藏品版本与证据摘要", "ok");
    await loadAll();
  } catch (e) { msg(`封存失败：${e.message}`, "err"); }
}

async function review(packageId) {
  try {
    const pkg = await api(`/api/packages/${packageId}/review`, {method: "POST"});
    msg(`复审草稿包 #${pkg.id} 已建立，请补材料或直接重新封存`, "ok");
    await loadAll();
  } catch (e) { msg(`复审失败：${e.message}`, "err"); }
}

async function runTransitions(packageId) {
  try {
    const transitions = [{
      claim_id: parseInt(document.getElementById(`ts-claim-${packageId}`).value, 10),
      to_status: document.getElementById(`ts-target-${packageId}`).value,
      note: document.getElementById(`ts-note-${packageId}`).value,
    }];
    const job = await api(`/api/packages/${packageId}/jobs`,
      {method: "POST", body: JSON.stringify({transitions})});
    msg(summarizeJob(job), job.counts.failed || job.counts.blocked ? "err" : "ok");
    await loadAll();
  } catch (e) { msg(`流转被挡住：${e.message}`, "err"); }
}

function renderJobs(jobs) {
  document.getElementById("jobList").innerHTML = jobs.map(j => {
    const c = j.counts;
    return `<div class="card">作业 #${j.id}（依据包 #${j.package_id}）
      <span class="tag tag-done">完成 ${c.done}</span>
      <span class="tag tag-failed">失败 ${c.failed}</span>
      <span class="tag tag-blocked">挡住 ${c.blocked}</span>
      <span class="tag tag-pending">待办 ${c.pending}</span>
      ${j.finished ? "<span class='ok'>已全部完成</span>" : "<button onclick='retryJob(" + j.id + ")'>重试：只续做未完成项</button>"}
      <div class="muted">创建 ${j.created_at || ""}</div>
      <button onclick='showJob(${j.id})'>查看明细</button>
      <pre id="job-${j.id}" hidden></pre>
    </div>`;
  }).join("") || "<p class='muted'>暂无流转作业</p>";
}

function summarizeJob(job) {
  return `作业 #${job.id}：完成 ${job.counts.done}、失败 ${job.counts.failed}、挡住 ${job.counts.blocked}、待办 ${job.counts.pending}`;
}

async function retryJob(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}/retry`, {method: "POST"});
    msg("重试结果：" + summarizeJob(job), job.counts.failed || job.counts.blocked ? "err" : "ok");
    await loadAll();
  } catch (e) { msg(`重试失败：${e.message}`, "err"); }
}

async function showJob(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    const pre = document.getElementById(`job-${jobId}`);
    pre.hidden = false;
    pre.textContent = JSON.stringify(job, null, 2);
  } catch (e) { msg(`查看失败：${e.message}`, "err"); }
}
