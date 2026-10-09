# Asistencias en campo (Odoo 18)

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
   - identifica a la persona comparando su huella facial en el servidor (umbral `Remote_Attendance.face_threshold`, 0.5 por defecto);
   - si no la reconoce, permite marcar con PIN y queda **en revisión**;
   - si hay varias obras activas, sugiere la más cercana por GPS;
   - fuera del radio o sin GPS, queda **en revisión**;
   - **Cambiar de obra** cierra la asistencia actual y abre otra en la nueva obra.
5. Al cerrar una asistencia válida o aprobada se crea su línea de horas en el proyecto.
   Las que están en revisión generan horas cuando Operaciones las aprueba (*Asistencias › Campo › Marcajes*).

## El día en cuatro marcajes

| Momento | Dónde | Cómo |
|---|---|---|
| Entrada | Tablet de la oficina | Se para frente a la cámara (no toca nada) o pasa su tarjeta |
| Salida a comer | Teléfono del supervisor, en la obra | Botón **Salida a comer** y cara |
| Regreso de comer | Teléfono del supervisor | Botón **Regreso de comer** y cara |
| Salida | Teléfono del supervisor | Botón **Salida** y cara |

- **Kiosco de oficina**: un empleado con rol *Kiosco de oficina* (por ejemplo "Tablet recepción") con su enlace
  y su tablet aprobada. Reconoce a todo el personal de campo de forma automática; un segundo escaneo en
  menos de 30 minutos se ignora. Acepta lectores de tarjeta que escriben el número y Enter: el número es el
  **ID de credencial** del empleado. *Marcar con PIN* para quien no se reconozca (queda en revisión).
- La entrada en oficina no tiene obra; al marcar en la obra, esas horas se cargan a la obra donde marca.
- El teléfono del supervisor reconoce a todo el personal de campo (también a gente prestada de otra cuadrilla);
  las horas van a las obras activas de quien marca.
- **Registrar comida después**: si el supervisor estaba en otra obra a la hora de comer, elige la persona,
  el día (hoy o ayer) y las horas de salida y regreso. La asistencia se parte en dos y queda en revisión de RH.
- Si alguien no marcó salida el día anterior, al marcar la entrada se cierra esa asistencia sin horas y queda
  en revisión para que RH capture la salida.
- Cuando la comida está marcada, las horas del proyecto son las reales (no se descuenta además la hora de comida del horario).

## Solo cámara en campo

En el teléfono del supervisor y en el enlace del trabajador la asistencia es **solo con cámara**: no aparecen el pase de lista,
*Marcar con PIN* ni *Registrar comida después*, y el servidor los rechaza. Las funciones siguen en el módulo; para
volver a mostrarlas pon el parámetro del sistema `Remote_Attendance.supervisor_manual` en `1`.
La tablet de oficina conserva *Marcar con PIN* y la tarjeta.

## Cargar asistencias a mano (Operaciones)

En *Asistencias › Campo › Cargar asistencias a mano*:

1. Elige el **día** y la **obra**. Si eliges un **supervisor**, se agrega su cuadrilla; *Agregar todo el personal de campo*
   pone a todos. También puedes agregar personas una por una.
2. Cada fila lleva **empleado**, **supervisor que lo trae** y **horas trabajadas**. El supervisor se llena con el de arriba
   o con el de su cuadrilla y se puede cambiar (gente prestada). Palomea **Asistió** a quien vino. Entrada y salida empiezan en 7:00 y 17:00 (cámbialas arriba para todos o por persona).
3. Escribe la salida o directamente las **horas trabajadas**: Odoo ajusta la salida sumando la hora de comida.
   Quita *Salió a comer* si la persona no comió. La columna *Otra obra* (oculta) sirve si alguien estuvo en otra obra.
4. **Cargar asistencias** crea las asistencias y sus horas en el proyecto con la misma regla del horario.
   Lo que pase de las 17:00 entra como tiempo extra ya aprobado, porque lo carga Operaciones.
   Quien ya tiene asistencia ese día se salta y se avisa.

Las asistencias cargadas a mano guardan quién las cargó (*Cargada a mano por*) y se pueden filtrar con *Cargadas a mano* en *Marcajes*.

## Horario, comida y tiempo extra

- Las horas del proyecto se cuentan solo dentro del horario: de **7:00 a 17:00** (hora del empleado; revisa que su
  zona horaria sea *America/Mexico_City*). Llegar antes de las 7 no suma.
- La comida no se cuenta: si se marcó, queda fuera sola; si no se marcó, se descuenta **1 hora** de jornadas de 6 horas o más.
- Si alguien sale **una hora o más después** del horario (18:00 o más), el teléfono pregunta **¿Es tiempo extra?**
  - *Sí*: la salida queda con tiempo extra **por validar** (*Asistencias › Campo › Tiempo extra por validar*).
    Las horas normales se cargan de inmediato y las extra al aprobarlas.
  - *No*: solo cuenta hasta las 17:00.
  - En la tablet de oficina nadie contesta: una salida tarde queda por validar.
- Se puede cambiar en *Ajustes › Técnico › Parámetros del sistema*: `Remote_Attendance.day_start` (7),
  `Remote_Attendance.day_end` (17), `Remote_Attendance.lunch_hours` (1) y `Remote_Attendance.overtime_after` (1).

## Registrar rostros sin cuenta de Odoo

- **Desde el enlace del supervisor o la tablet de oficina**: botón *Registrar rostro de un empleado*. Se elige a la
  persona (solo salen quienes aún no tienen rostro), se palomea el consentimiento, se toman 3 fotos y se confirma con
  el PIN del dueño del enlace. Queda guardado quién lo registró; reemplazar un rostro lo hace Operaciones en Odoo
  (*Borrar rostros* y registrar de nuevo).
- **Con la foto de la ficha** (*Asistencias › Campo › Registrar rostros con foto*): registra de golpe a todos los que
  tienen foto y aún no tienen rostro. Con una sola foto reconoce menos; a quien no reconozca, agréguenle 3 fotos en vivo.

## Enlace del supervisor

Abre directo en la cámara (el pase de lista queda escondido). Si el supervisor tiene **una sola obra asignada**, queda
activa sola; con varias, elige las del día en *Obras de hoy*.

## Clave del empleado

La clave es el **Número de registro del empleado** (pestaña Nómina), la misma de nómina. Si un empleado
no tiene, se usa su **ID de credencial**. La tarjeta funciona con cualquiera de los dos: el ID de credencial
solo hace falta si el número de la tarjeta es distinto al de nómina. La clave se usa para:

- la tarjeta en el kiosco de oficina;
- la lista de WhatsApp: `1608 Juan` o solo `1608` se reconoce por la clave antes que por el nombre
  (los números de 1 o 2 dígitos al inicio se toman como numeración de la lista);
- buscar gente en el pase de lista.

## Cargar la lista de WhatsApp (plan de contingencia)

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
3. Palomea a su cuadrilla y cambia a **½ día** quien corresponda. La gente prestada se agrega buscándola por nombre o clave.
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
