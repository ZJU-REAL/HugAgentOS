// 平台内置轻后端封装（发布后由站点托管方提供，本地 dev 环境不可用）。
// 全部走相对路径（不带前导 /）：hash 路由下 document base 是 /site/<slug>/，
// fetch("__api/...") 会自动落到当前站点的 __api 命名空间。
// 限额：KV ≤200 键、值 ≤4KB；表单 ≤8KB/条。

const KV_BASE = "__api/kv/";
const FORM_BASE = "__api/forms/";

export async function kvGet(key) {
  const res = await fetch(KV_BASE + encodeURIComponent(key));
  if (!res.ok) throw new Error(`kvGet ${key}: HTTP ${res.status}`);
  return res.json(); // { value, exists }
}

export async function kvSet(key, value) {
  const res = await fetch(KV_BASE + encodeURIComponent(key), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: String(value) }),
  });
  if (!res.ok) throw new Error(`kvSet ${key}: HTTP ${res.status}`);
  return res.json();
}

export async function kvDelete(key) {
  const res = await fetch(KV_BASE + encodeURIComponent(key), { method: "DELETE" });
  if (!res.ok) throw new Error(`kvDelete ${key}: HTTP ${res.status}`);
  return res.json();
}

export async function submitForm(formKey, data) {
  const res = await fetch(FORM_BASE + encodeURIComponent(formKey), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`submitForm ${formKey}: HTTP ${res.status}`);
  return res.json();
}
