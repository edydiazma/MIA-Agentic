import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Imagen de producción mínima (node server.js) para Docker en EC2
  output: "standalone",
  // La raíz del repo tiene otro package-lock (CLI de Supabase): fijar la raíz evita rutas anidadas en standalone
  outputFileTracingRoot: dirname(fileURLToPath(import.meta.url)),
  poweredByHeader: false,
};

export default nextConfig;
