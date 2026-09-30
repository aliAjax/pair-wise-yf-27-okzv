// 公开入口页脚本：与审查工作台分开维护
async function api(path, options = {}) {
  const headers = Object.assign({"X-User-Id": document.getElementById("user").value}, options.headers || {});
  const res = await fetch(path, Object.assign({}, options, {headers}));
  const data = await res.json();
  if (!res.ok) throw new Error(`${res.status} ${data.error ? data.error.code : ""} ${data.error ? data.error.message : ""}`);
  return data;
}

async function loadObjects() {
  const list = document.getElementById("list");
  const out = document.getElementById("out");
  out.hidden = true;
  try {
    const data = await api("/api/objects");
    list.hidden = false;
    list.innerHTML = "<h3>藏品清单</h3>" + data.items.map(o =>
      `<div class="card"><b>#${o.id} ${o.inventory_no} · ${o.title}</b>
       <div class="muted">${o.object_type || ""} ｜ 版本 v${o.version}</div>
       <div>${o.public_summary || ""}</div>
       <button onclick="showObject(${o.id})">查看详情</button></div>`).join("") || "<p>暂无藏品</p>";
  } catch (e) {
    out.hidden = false;
    out.textContent = String(e);
  }
}

async function showObject(id) {
  const out = document.getElementById("out");
  try {
    const data = await api(`/api/objects/${id}`);
    out.hidden = false;
    out.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    out.hidden = false;
    out.textContent = String(e);
  }
}
