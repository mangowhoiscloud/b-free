import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// Dev server only: the same /svc/<name>/ read-only routes the container's nginx serves
// (docker/40-bravo-config.sh). Targets come from BRAVO_* variables, with local pilot ports as defaults.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'BRAVO_');
  const upstreams: Record<string, string> = {
    collector: env.BRAVO_COLLECTOR_UPSTREAM ?? 'http://localhost:8083',
    gate: env.BRAVO_GATE_UPSTREAM ?? 'http://localhost:8084',
    prometheus: env.BRAVO_PROMETHEUS_UPSTREAM ?? 'http://localhost:9090',
  };
  const proxy = Object.fromEntries(
    Object.entries(upstreams).map(([name, target]) => [
      `/svc/${name}/`,
      { target, changeOrigin: true, rewrite: (p: string) => p.replace(`/svc/${name}`, '') },
    ]),
  );
  return {
    plugins: [react()],
    server: { port: 5173, strictPort: true, proxy },
    preview: { port: 5173, strictPort: true },
    build: { target: 'es2022', sourcemap: false },
  };
});
