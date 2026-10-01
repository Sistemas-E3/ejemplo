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

## Cargar la lista de WhatsApp (sin fotos)

Sirve con el mensaje que ya mandan los supervisores al grupo, por ejemplo:

```
PRESUPUESTO: 2316-26 _ES
ACTIVIDAD : cuarto de Shirrock
UBICACION: Danfoss
PERSONAL :
Efraín Salazar
Jhonatan peña 1/2 día

Curso hidro 1/2 día
Jhonatan Peña
```

1. Operaciones abre *Asistencias › Campo › Cargar lista de WhatsApp*, elige supervisor y fecha y pega el mensaje
   (sirve con o sin la fecha y el nombre que agrega WhatsApp al copiar).
2. Cada `PRESUPUESTO:` (o `PROYECTO:` / `OBRA:`) abre un bloque; la obra se reconoce por su **Número de proyecto**
   (o el código analítico o el número dentro del nombre). Una línea suelta como `Curso hidro 1/2 día` o `taller 1/2 día`
   también abre un bloque: la primera vez se elige a qué proyecto va y Odoo lo recuerda (*Obras recordadas*).
3. `1/2 día`, `medio día` o `4 hrs` junto al bloque o al nombre indican la parte de la jornada.
4. Las personas se buscan entre todo el personal de campo (incluida la gente prestada de otras cuadrillas).
   Al corregir un nombre, Odoo lo recuerda (*Nombres recordados*).
5. **Registrar asistencia** crea **una asistencia por persona y día** con la jornada de su horario y la reparte entre
   las obras: por ejemplo 4 h en la 2316-26 y 4 h en el curso. Cada obra recibe su línea en la hoja de horas.
   Si la persona ya marcó con el teléfono no se duplica; si se cargan más mensajes del mismo día se suman.
   Si suma más de una jornada o es día no laborable, queda en revisión.

## Pase de lista desde el teléfono (sin escribir nombres)

Otra forma de reportar, además de WhatsApp (las dos conviven y se suman):

1. El supervisor abre su enlace (*Personal de campo › Campo › enlace*) en su teléfono aprobado. Entra directo a **Pase de lista**;
   el botón *Cámara* lleva al marcaje con reconocimiento facial.
2. Elige **Hoy** o **Ayer** y una de sus obras asignadas (solo ve esas).
3. Palomea a su cuadrilla y cambia a **½ día** quien corresponda. La gente prestada se agrega buscándola por nombre.
   Junto a cada persona se ve si ya tiene algo ese día (otra obra, otro supervisor o marcaje con cámara).
4. Escribe su PIN y toca **Enviar lista**. Se crean las asistencias y las horas igual que con WhatsApp.
   Si vuelve a enviar la lista de esa obra y día, **reemplaza** la anterior (sirve para corregir).

Para actividades internas (curso, taller) Operaciones debe asignarle también ese proyecto al supervisor.

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
