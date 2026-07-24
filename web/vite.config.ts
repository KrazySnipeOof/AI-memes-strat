import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const base = process.env.BASE_PATH ?? "/";
const webDir = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  base,
  plugins: [react()],
  resolve: {
    alias: {
      "cursor/canvas": `${webDir}/node_modules/@thisismydesign/cursor-canvas-web`,
      react: `${webDir}/node_modules/react`,
      "react-dom": `${webDir}/node_modules/react-dom`,
      "react/jsx-runtime": `${webDir}/node_modules/react/jsx-runtime.js`,
      "react/jsx-dev-runtime": `${webDir}/node_modules/react/jsx-dev-runtime.js`,
    },
  },
  server: {
    fs: {
      allow: [fileURLToPath(new URL("..", import.meta.url))],
    },
  },
});
