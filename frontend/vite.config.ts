import { fileURLToPath, URL } from 'node:url'
import { isAbsolute, resolve } from 'node:path'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

const runtimeRoot = process.env.KANTOKU_RUNTIME_ROOT
if (runtimeRoot && !isAbsolute(runtimeRoot)) throw new Error('KANTOKU_RUNTIME_ROOT 必须为绝对路径')

export default defineConfig({
  plugins: [vue()],
  cacheDir: runtimeRoot ? resolve(runtimeRoot, 'cache/vite') : fileURLToPath(new URL('../cache/vite', import.meta.url)),
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
  },
  build: {
    outDir: '../src/kantoku/shells/web',
    emptyOutDir: true,
    sourcemap: false,
  },
})
