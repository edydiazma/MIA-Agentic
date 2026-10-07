import "./globals.css";
import type { Metadata, Viewport } from "next";
import PwaRegister from "@/components/pwa/PwaRegister";

export const metadata: Metadata = {
  title: "Bandeja WhatsApp",
  description: "Agente de IA + asesores en WhatsApp",
  // App instalable del asesor (PWA): manifiesto, íconos y service worker (public/sw.js)
  manifest: "/manifest.webmanifest",
  icons: { icon: "/icons/icon.svg", apple: "/icons/apple-touch-icon.png" },
  appleWebApp: { capable: true, title: "WA Agent", statusBarStyle: "default" },
};

export const viewport: Viewport = { themeColor: "#128c7e" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body>
        {children}
        <PwaRegister />
      </body>
    </html>
  );
}
