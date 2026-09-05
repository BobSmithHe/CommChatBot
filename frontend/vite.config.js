import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          vue: ["vue"],
          markdown: ["marked", "highlight.js", "katex"],
          icons: ["lucide-vue-next"],
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", ws: true },
      "/health": "http://127.0.0.1:8765",
    },
  },
});
