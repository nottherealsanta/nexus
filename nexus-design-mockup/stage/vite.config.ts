import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteSingleFile } from 'vite-plugin-singlefile'
// One self-contained index.html: fonts and images inline, so it opens from disk or as an artifact.
export default defineConfig({ plugins: [react(), viteSingleFile()], build: { target: 'es2022', assetsInlineLimit: 100000000 } })
