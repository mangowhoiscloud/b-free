/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Dev server only: API origin. Built images read config.json at runtime instead. */
  readonly VITE_API_BASE?: string;
  /** Dev server only: observability links. */
  readonly VITE_GRAFANA_URL?: string;
  readonly VITE_PROMETHEUS_URL?: string;
  readonly VITE_TEMPO_URL?: string;
  readonly VITE_DOH_URL?: string;
  readonly VITE_APP_ENVS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
