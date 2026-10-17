import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { build } from 'vite';
await build({
 configFile: false,
 build: {
  outDir: 'dist', emptyOutDir: true, sourcemap: false,
  lib: { entry: 'src/customization.tsx', formats: ['es'], fileName: 'customization', cssFileName: 'theme' },
  rollupOptions: { external: id => id === 'react' || id.startsWith('react/') ||
   id === 'react-dom' || id.startsWith('react-dom/') || id === '@hugagent/chat-sdk' || id.startsWith('@hugagent/chat-sdk/') },
 },
});

// Vite extracts library CSS. Keep the package entry self-contained for the host.
if (existsSync('dist/theme.css')) writeFileSync('dist/customization.js', "import './theme.css';\n" + readFileSync('dist/customization.js', 'utf8'));
