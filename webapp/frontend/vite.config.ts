import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: { rollupOptions: { output: { manualChunks: id => /node_modules[\\/](echarts|zrender)/.test(id) ? 'charts' : undefined } } },
  server: {
    host: '127.0.0.1', port: Number(process.env.QUANT_UI_PORT || 5175), strictPort: true,
    fs: { strict: true, allow: [process.cwd()] },
    proxy: { '/api': { target: `http://127.0.0.1:${process.env.QUANT_API_PORT || 8765}` } },
  },
});
