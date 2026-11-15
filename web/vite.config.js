import { defineConfig } from "vite";

// The web only talks to the gateway (never to Atlas). Vite proxies /api and /ws to it.
const gateway = process.env.GATEWAY_URL || "http://localhost:8000";

export default defineConfig({
  build: { target: "es2022" },
  server: {
    proxy: {
      "/api": { target: gateway, changeOrigin: true },
      "/ws": { target: gateway.replace(/^http/, "ws"), ws: true },
    },
  },
});
