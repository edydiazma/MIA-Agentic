/** Menú del panel (misma estructura que Atom). `soon` = módulo de una fase posterior. */
export type NavItem = { label: string; href: string; soon?: boolean; adminOnly?: boolean };
export type NavGroup = { label: string; items: NavItem[] };
export type NavEntry = { label: string; icon: string; href?: string; adminOnly?: boolean; staffOnly?: boolean; groups?: NavGroup[] };

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
          { label: "Pruebas de agentes", href: "/automatizaciones/cortex/pruebas" },
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
          { label: "Webhooks entrantes", href: "/automatizaciones/webhooks-entrantes" },
          { label: "Calidad (QA)", href: "/automatizaciones/calidad" },
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
          { label: "Nivel de servicio", href: "/reportes/servicio" },
          { label: "Reporte Login", href: "/reportes/login" },
          { label: "Reporte general", href: "/reportes/general" },
          { label: "Etapas de contactos", href: "/reportes/etapas" },
          { label: "IA y Cortex", href: "/reportes/ia" },
          { label: "Análisis de flujos", href: "/reportes/flujos" },
          { label: "Canales", href: "/reportes/canales" },
          { label: "Calidad y coaching", href: "/reportes/calidad" },
          { label: "Clientes (BI)", href: "/reportes/clientes" },
          { label: "Productos", href: "/reportes/productos" },
          { label: "Datos maestros", href: "/reportes/datos-maestros" },
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
          { label: "Mensajes disparadores", href: "/reportes/mensajes-disparadores" },
          { label: "Anuncios", href: "/reportes/anuncios" },
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
  { label: "Monitoreo", icon: "🛰️", href: "/monitoreo", staffOnly: true },  // admin o supervisor
  {
    label: "Mi espacio",
    icon: "🎯",
    groups: [
      {
        label: "",
        items: [
          { label: "Mi coaching", href: "/coaching" },
          { label: "Notificaciones", href: "/configuraciones/notificaciones" },
          { label: "Mi seguridad", href: "/perfil/seguridad" },
        ],
      },
    ],
  },
  {
    label: "Configuraciones",
    icon: "🛠️",
    adminOnly: true,
    groups: [
      {
        label: "",
        items: [
          { label: "Asistente de configuración", href: "/onboarding" },
          { label: "Plataforma", href: "/configuraciones/plataforma" },
          { label: "Mensajería", href: "/configuraciones/mensajeria" },
          { label: "Conversaciones", href: "/configuraciones/conversaciones" },
          { label: "Magia de IA", href: "/configuraciones/magia" },
          { label: "Clasificación IA", href: "/configuraciones/clasificacion" },
          { label: "Campos de cliente", href: "/configuraciones/campos" },
          { label: "Campos y datos maestros", href: "/configuraciones/datos-maestros" },
          { label: "Gestión usuarios", href: "/configuraciones/usuarios" },
          { label: "Estados de asesor", href: "/configuraciones/estados" },
          { label: "Horarios de atención", href: "/configuraciones/horarios" },
          { label: "Enrutamiento", href: "/configuraciones/enrutamiento" },
          { label: "Roles y permisos", href: "/configuraciones/roles" },
          { label: "Seguridad", href: "/configuraciones/seguridad" },
          { label: "Auditoría de acceso", href: "/configuraciones/auditoria" },
          { label: "Reportes", href: "/configuraciones/reportes" },
          { label: "Mi empresa", href: "/configuraciones/empresa" },
          { label: "Gestor de recursos", href: "/configuraciones/recursos" },
          { label: "Citas", href: "/configuraciones/citas" },
          { label: "Integraciones (CRM y Ads)", href: "/configuraciones/integraciones" },
          { label: "Atribución web", href: "/configuraciones/atribucion" },
          { label: "Mensajes disparadores", href: "/configuraciones/mensajes-disparadores" },
          { label: "Conversiones", href: "/configuraciones/conversiones" },
          { label: "API y conectores", href: "/configuraciones/api" },
          { label: "Plan y facturación", href: "/configuraciones/plan" },
        ],
      },
    ],
  },
];
