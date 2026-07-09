import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const frontendPort = Number(process.env.CODEXHUB_FRONTEND_PORT ?? 1420);
const validatedFrontendPort = Number.isInteger(frontendPort) ? frontendPort : 1420;

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  build: {
    assetsInlineLimit: 0,
  },
  server: {
    host: "127.0.0.1",
    port: validatedFrontendPort,
    strictPort: true,
  },
  preview: {
    host: "127.0.0.1",
    port: validatedFrontendPort,
    strictPort: true,
  },
});
