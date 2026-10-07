import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = { title: "Bandeja WhatsApp", description: "Agente de IA + asesores en WhatsApp" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body>{children}</body>
    </html>
  );
}
