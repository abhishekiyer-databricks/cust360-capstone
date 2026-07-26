import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// This project lives in app/frontend/ (package.json is here — NOT at app/ root, which would
// make the Databricks Apps runtime try to `npm build` and fail; see master_plan D6). npm
// scripts run from here, so cwd is app/frontend.
//
// FastAPI serves the built bundle as static files (see app/backend/main.py). We build into
// ../backend/static so `bundle deploy` (source_code_path) uploads it with the app.
// Dev server proxies /api → uvicorn so `npm run dev` + a local uvicorn work side-by-side.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: path.resolve(__dirname, "../backend/static"),
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
