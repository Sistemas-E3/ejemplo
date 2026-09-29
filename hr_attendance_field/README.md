# Asistencias en campo (Odoo 19)

Marcaje de asistencia desde el teléfono con reconocimiento facial y GPS, ligado a la obra
(proyecto) y convertido automáticamente en horas del proyecto.

## Cómo funciona

1. **Operaciones** (usuarios internos con el grupo *Asistencias en campo › Operaciones de campo*):
   - En *Asistencias › Campo › Obras* asigna supervisores a cada obra y captura su ubicación y radio (200 m por defecto).
   - En la ficha del empleado, pestaña **Campo**: rol (supervisor o trabajador), supervisor de la cuadrilla,
     PIN, **Generar enlace nuevo** y **Registrar rostro** (requiere consentimiento firmado).
   - En *Asistencias › Campo › Teléfonos* aprueba los teléfonos que solicitan acceso.
2. **Supervisor** (sin usuario de Odoo): abre su enlace en su teléfono, elige con su PIN las **obras de hoy**
   entre las que tiene asignadas, y su cuadrilla marca frente a su cámara.
3. **Trabajador** (sin usuario de Odoo): cuando el supervisor no está, marca desde su propio teléfono con su enlace;
   solo se reconoce su rostro y solo puede usar las obras activas de su supervisor.
4. Cada marcaje:
   - identifica a la persona comparando su huella facial en el servidor (umbral `hr_attendance_field.face_threshold`, 0.5 por defecto);
   - si no la reconoce, permite marcar con PIN y queda **en revisión**;
   - si hay varias obras activas, sugiere la más cercana por GPS;
   - fuera del radio o sin GPS, queda **en revisión**;
   - **Cambiar de obra** cierra la asistencia actual y abre otra en la nueva obra.
5. Al cerrar una asistencia válida o aprobada se crea su línea de horas en el proyecto.
   Las que están en revisión generan horas cuando Operaciones las aprueba (*Asistencias › Campo › Marcajes*).

## Seguridad

- Enlace personal con token secreto; **Generar enlace nuevo** invalida el anterior y revoca sus teléfonos.
- Cada teléfono debe ser aprobado por Operaciones; la clave del teléfono se guarda cifrada (hash).
- El PIN se bloquea 15 minutos tras 5 intentos fallidos.
- La lista de obras, la cuadrilla y el GPS se validan en el servidor.
- Las huellas faciales no salen del servidor: el teléfono solo envía la huella del marcaje actual.

## Notas

- Reconocimiento facial con [face-api](https://github.com/vladmandic/face-api) (MIT), incluido en `static/lib`.
- La cámara y el GPS requieren HTTPS (Odoo.sh ya lo tiene).
- Los datos biométricos son datos personales sensibles (LFPDPPP): recaba consentimiento expreso y por escrito.
