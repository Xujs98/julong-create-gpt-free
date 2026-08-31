import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../webui/static/react',
    emptyOutDir: true,
    manifest: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:5000',
      '/logout': 'http://127.0.0.1:5000',
    },
  },
});
