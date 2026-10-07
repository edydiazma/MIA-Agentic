/** @type {import('next').NextConfig} */
const nextConfig = {
  // Imagen de producción mínima (node server.js) para Docker en EC2
  output: "standalone",
  poweredByHeader: false,
};

export default nextConfig;
