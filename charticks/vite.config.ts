import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import electron from "vite-plugin-electron";
import { resolve } from "node:path";

// Some hosts (e.g. VS Code's integrated terminal / extension host) inject
// ELECTRON_RUN_AS_NODE=1, which makes the electron binary behave like plain
// Node and crash on `require("electron")`. Clear it so the child Electron the
// plugin spawns runs as a real Electron app.
delete process.env.ELECTRON_RUN_AS_NODE;

// Renderer (React) + Electron main/preload bundling.
export default defineConfig({
  resolve: { alias: { "@": resolve(__dirname, "src") } },
  plugins: [
    react(),
    electron([
      {
        // Main process
        entry: "electron/main.ts",
        vite: { build: { outDir: "dist-electron" } },
      },
      {
        // Preload
        entry: "electron/preload.ts",
        vite: { build: { outDir: "dist-electron" } },
        onstart({ reload }) {
          reload();
        },
      },
    ]),
  ],
  server: { port: 5173, strictPort: true },
  build: { outDir: "dist" },
});
