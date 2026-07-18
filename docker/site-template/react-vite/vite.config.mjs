// 平台约束：站点托管在 /site/<slug>/ 子路径下，base 必须保持 './'（相对路径），
// 改成 '/' 会导致发布后所有资源 404。outDir/cacheDir 指向 /workspace 临时区，
// 不落入项目文件夹（myspace 网盘），路径按项目目录名自动隔离。
import { basename } from "node:path";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// 工程标识 = 工作区下的相对路径打平（与 init-react-site.sh 的 KEY 算法一致），
// 避免 site-src/foo 与 myspace/<uid>/foo 同名时共享产物/缓存目录互相覆盖。
// 工作区根 Docker 内是 /workspace，无 Docker 本地档由 SCRIPT_RUNNER_WORKSPACE 指定。
const ws = (process.env.SCRIPT_RUNNER_WORKSPACE || "/workspace").replace(/\/$/, "");
const cwd = process.cwd();
const projectKey = cwd.startsWith(ws + "/")
  ? cwd.slice(ws.length + 1).replace(/\//g, "_")
  : basename(cwd);

export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  cacheDir: process.env.SITE_CACHE || `${ws}/.vite-cache/${projectKey}`,
  build: {
    outDir: process.env.SITE_DIST || `${ws}/.site-dist/${projectKey}`,
    emptyOutDir: true,
    chunkSizeWarningLimit: 2048,
  },
});
