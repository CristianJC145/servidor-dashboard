# SERVIDOR — Centro de Operaciones

Dashboard de tareas multi-usuario para el equipo (roles admin / editor).
FastAPI + SQLite + frontend en un solo contenedor. Diseñado para EasyPanel.

## Qué hace

- **Roles:** el admin crea usuarios, crea y asigna tareas, y aprueba entregas. El editor ve sus tareas, las marca "en progreso" → "entregada", deja nota de entrega y adjunta links de archivos (Drive, Docs, Canva, lo que sea).
- **Vista "Hoy":** tareas vencidas, para hoy, y (para el admin) las entregadas por revisar.
- **Tablero:** 4 columnas por estado (Pendiente → En progreso → Entregada → Aprobada), con filtros por usuario y canal.
- **Archivos por tarea:** cada tarea guarda links etiquetados (guion, miniatura, música, video, bucle). Click = abre el archivo.
- **Equipo:** el admin crea/desactiva usuarios y resetea contraseñas.

## Desplegar en EasyPanel

1. Sube esta carpeta a un repositorio de GitHub (o usa "Upload" si tu EasyPanel lo permite).
2. En EasyPanel: **+ Service → App → Source: GitHub** (o Dockerfile).
3. Build: detecta el `Dockerfile` automáticamente.
4. **IMPORTANTE — Volumen persistente:** en *Mounts / Volumes* agrega un volumen montado en `/data`. Ahí vive la base de datos `servidor.db`. Sin esto, pierdes todo al redeplegar.
5. Puerto: expón el `8000` y asigna tu dominio (ej. `tareas.tudominio.com`) con HTTPS activado.
6. Deploy.

## Primer uso

1. Abre la URL. La primera pantalla te pide crear la **cuenta admin** (solo aparece una vez).
2. Entra, ve a **Equipo → + Nuevo usuario** y crea las cuentas de tu equipo (rol `editor`).
3. Crea tareas desde **Hoy** o **Tablero** con el botón **+ Nueva tarea**.

## Flujo de una tarea

```
PENDIENTE → EN PROGRESO → ENTREGADA → APROBADA
 (admin la crea)  (editor)     (editor + nota    (solo admin,
                                + links)          se enciende en dorado)
```

## Probar en local (opcional)

```bash
pip install -r requirements.txt
DATA_DIR=./data uvicorn main:app --port 8000
# abre http://localhost:8000
```

## Almacenamiento de archivos (dos destinos)

El sistema separa a propósito dónde vive cada cosa:

| Qué | Dónde | Cuándo |
|---|---|---|
| **Adjuntos de trabajo** (el guion que sube quien crea la tarea, para que el editor lo descargue) | En **este servidor** (`$DATA_DIR/uploads`) — temporal | Mientras la tarea está pendiente o en revisión |
| **Material del proyecto** (miniatura, guion, música, video final…) | En **tu Google Drive** | Al **marcar terminado**: se elige la carpeta local del proyecto y se sube completa, **directo del navegador a Drive** (los bytes NO pasan por el servidor; reanudable si se cae el internet) |

Organización automática en Drive: `SERVIDOR-VIDEOS / [canal] / T{id} - {título del video} /` (respeta subcarpetas).

**Limpieza automática:** al terminar una tarea con archivado, los adjuntos temporales se **borran del servidor** (disco y registro) — ya viven en Drive y quedan buscables desde el índice. Si se termina *sin* archivar, los adjuntos se conservan.

## Conectar Google Drive (una sola vez)

Lo hace el administrador desde **Ajustes → Drive** dentro de la app. Antes necesitas credenciales de Google:

1. Entra a [Google Cloud Console → Credenciales](https://console.cloud.google.com/apis/credentials) con la cuenta de Google donde quieres guardar los videos.
2. Crea un proyecto (si no tienes) y activa la **Google Drive API** (APIs y servicios → Biblioteca → busca "Google Drive API" → Habilitar).
3. Configura la **pantalla de consentimiento OAuth** (tipo "Externo"; agrega tu propio correo como usuario de prueba).
4. Crea credenciales → **ID de cliente de OAuth** → tipo **Aplicación web**.
5. En **URI de redirección autorizados**, pega la URL que la app te muestra en Ajustes → Drive
   (local: `http://localhost:8000/api/drive/callback`; en producción: `https://tu-dominio/api/drive/callback`).
6. Copia el **Client ID** y **Client Secret**, pégalos en Ajustes → Drive y pulsa **Guardar y conectar**.
7. Autoriza en la ventana de Google. Listo: el estado queda "● Conectado".

> Scope usado: `drive.file` (la app solo ve/gestiona los archivos que ella misma crea — no toca el resto de tu Drive).
> Si el refresh token se revoca, la app avisa con un banner para reconectar; las subidas fallan con mensaje claro, nunca en silencio.

## Próximas fases (ya contempladas en el diseño)

- **Índice de archivos:** tabla con hash, canal y tipo — para responder "¿este video ya se subió?" y búsqueda global.
- **Hermes:** endpoints para que Hermes registre subidas automáticas y consulte el índice desde WhatsApp.
- La base de datos actual (SQLite en `/data`) está lista para crecer con esas tablas sin migrar nada.

## Seguridad básica

- Contraseñas con PBKDF2 (200.000 iteraciones), sesiones por token.
- Usa siempre HTTPS (EasyPanel lo da con Let's Encrypt).
- No expongas el puerto 8000 directo a internet sin el proxy de EasyPanel.
