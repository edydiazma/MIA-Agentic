/** Menú del panel (misma estructura que Atom). `soon` = módulo de una fase posterior. */
export type NavItem = { label: string; href: string; soon?: boolean; adminOnly?: boolean };
export type NavGroup = { label: string; items: NavItem[] };
export type NavEntry = { label: string; icon: string; href?: string; adminOnly?: boolean; groups?: NavGroup[] };

export const NAV: NavEntry[] = [
  { label: "Inicio", icon: "🏠", href: "/" },
  { label: "Conversaciones", icon: "💬", href: "/conversaciones" },
  { label: "Tablero", icon: "📊", href: "/tablero" },
  { label: "Clientes", icon: "👥", href: "/clientes" },
  { label: "Clientes Bloqueados", icon: "⛔", href: "/clientes-bloqueados" },
  { label: "Negocios", icon: "💼", href: "/negocios" },
  { label: "Campañas", icon: "📣", href: "/campanas" },
  {
    label: "Automatizaciones",
    icon: "⚙️",
    adminOnly: true,
    groups: [
      {
        label: "Flujos",
        items: [
          { label: "Gestión de flujos", href: "/automatizaciones/flujos" },
          { label: "Historial de conversaciones", href: "/automatizaciones/historial" },
        ],
      },
      {
        label: "Cortex",
        items: [
          { label: "Agentes de IA", href: "/automatizaciones/cortex" },
          { label: "Conexiones y failover", href: "/automatizaciones/cortex/conexiones" },
          { label: "Memoria", href: "/automatizaciones/memoria" },
          { label: "Mejor vendedor", href: "/automatizaciones/mejor-vendedor" },
          { label: "Catálogo de productos", href: "/automatizaciones/catalogo" },
          { label: "Historial JSON", href: "/automatizaciones/revisiones" },
        ],
      },
      {
        label: "Llamadas",
        items: [
          { label: "Agentes de voz", href: "/automatizaciones/agentes-voz" },
          { label: "Historial de llamadas", href: "/automatizaciones/llamadas" },
        ],
      },
      {
        label: "General",
        items: [
          { label: "Tareas automatizadas", href: "/automatizaciones/tareas" },
          { label: "Webhooks", href: "/automatizaciones/webhooks" },
        ],
      },
    ],
  },
  {
    label: "Reportes",
    icon: "📈",
    groups: [
      {
        label: "General",
        items: [
          { label: "En tiempo real", href: "/reportes/tiempo-real" },
          { label: "Reporte general", href: "/reportes/general" },
          { label: "Etapas de contactos", href: "/reportes/etapas" },
          { label: "IA y Cortex", href: "/reportes/ia" },
          { label: "Análisis de flujos", href: "/reportes/flujos", soon: true },
          { label: "Llamadas", href: "/reportes/llamadas" },
        ],
      },
      {
        label: "Inbound",
        items: [
          { label: "Resumen", href: "/reportes/inbound" },
          { label: "Bots", href: "/reportes/bots" },
          { label: "Click to WA Meta", href: "/reportes/click-to-wa-meta" },
          { label: "Click to WA Google", href: "/reportes/click-to-wa-google" },
          { label: "Tráfico web", href: "/reportes/trafico-web" },
          { label: "Atribución y conversiones", href: "/reportes/atribucion" },
        ],
      },
      {
        label: "Outbound",
        items: [
          { label: "Resumen", href: "/reportes/outbound" },
          { label: "Campañas de plantillas", href: "/reportes/campanas" },
          { label: "Plantillas individuales", href: "/reportes/plantillas" },
          { label: "Webhooks", href: "/reportes/webhooks" },
        ],
      },
      {
        label: "Categorías",
        items: [{ label: "Tipificaciones", href: "/reportes/tipificaciones" }],
      },
      {
        label: "Agentes",
        items: [
          { label: "Nivel de servicio", href: "/reportes/nivel-servicio" },
          { label: "Estado de agentes", href: "/reportes/estado-agentes" },
        ],
      },
      { label: "Cuenta", items: [{ label: "Facturación", href: "/reportes/facturacion" }] },
    ],
  },
  { label: "Seguimiento", icon: "📌", href: "/seguimiento" },
  {
    label: "Configuraciones",
    icon: "🛠️",
    adminOnly: true,
    groups: [
      {
        label: "",
        items: [
          { label: "Plataforma", href: "/configuraciones/plataforma" },
          { label: "Mensajería", href: "/configuraciones/mensajeria" },
          { label: "Conversaciones", href: "/configuraciones/conversaciones" },
          { label: "Magia de IA", href: "/configuraciones/magia" },
          { label: "Clasificación IA", href: "/configuraciones/clasificacion" },
          { label: "Campos de cliente", href: "/configuraciones/campos" },
          { label: "Gestión usuarios", href: "/configuraciones/usuarios" },
          { label: "Reportes", href: "/configuraciones/reportes" },
          { label: "Mi empresa", href: "/configuraciones/empresa" },
          { label: "Gestor de recursos", href: "/configuraciones/recursos" },
          { label: "Citas", href: "/configuraciones/citas" },
          { label: "Integraciones (CRM y Ads)", href: "/configuraciones/integraciones" },
          { label: "Atribución web", href: "/configuraciones/atribucion" },
          { label: "Conversiones", href: "/configuraciones/conversiones" },
          { label: "Plan y facturación", href: "/configuraciones/plan" },
        ],
      },
    ],
  },
];
