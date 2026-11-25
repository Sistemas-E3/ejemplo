# Asistencias en campo (Odoo 19)

Registro de asistencia por reconocimiento facial: **entrada, salida a comer, regreso de comer y salida**.
Todos los empleados se manejan igual (no hay diferencia entre personal de campo y de oficina) y no se
cargan horas a proyectos. El teléfono del supervisor, la tablet de oficina y el checador se combinan:
cada marca se suma al mismo día de la persona, sin importar dónde la hizo.

## Dónde se marca

| Medio | Cómo | Qué marca |
|---|---|---|
| **Teléfono del supervisor** | La persona se pone frente a la cámara y el supervisor toca **Entrada**, **Salida a comer**, **Regreso de comer** o **Salida** | La que se toca |
| **Tablet de oficina** | Se para frente a la cámara (no toca nada) o pasa su tarjeta | Se deduce sola (ver abajo) |
| **Checador ZKTeco (ADMS)** | Huella, rostro o tarjeta en el checador; los marcajes llegan solos a Odoo | Se deduce sola (ver abajo) |
| **Enlace personal** | Desde el teléfono de la propia persona, con botones o automático | La que se toca, o se deduce sola |

**Cómo se deduce la marca en la tablet, el checador y el enlace automático:**

1. Primera marca del día: **entrada**.
2. La siguiente, si cae dentro del horario de comida (12:00 a 15:00 por defecto) y aún no comió: **salida a comer**.
3. La siguiente después de comer: **regreso de comer**.
4. La siguiente: **salida**.
5. Una segunda marca en menos de 30 minutos se ignora (repetida).

Por ejemplo: entra en el checador, sale a comer con el teléfono del supervisor, regresa en la tablet y sale en el checador.
Todo queda en el mismo día con sus cuatro marcas.

## Configurar a cada empleado

Todos los empleados activos aparecen en el reporte y pueden marcar. En la ficha del empleado, pestaña **Campo**:

- **Rostro**: **Registrar rostro** (requiere consentimiento firmado). Sin rostro no lo reconoce la cámara.
- **Supervisor de su cuadrilla**: para el filtro por supervisor del reporte.
- **Enlace** (solo quien necesita uno propio):
  - *Supervisor*: su teléfono marca a cualquier empleado.
  - *Enlace personal con botones* / *automático*: la persona marca desde su teléfono.
  - *Tablet de oficina*: un "empleado" que representa la tablet de la entrada (por ejemplo "Tablet recepción").
  - Con enlace: **PIN** y **Generar enlace nuevo**. Cada teléfono o tablet se aprueba en *Asistencias › Equipos › Teléfonos y tabletas*.

En *Asistencias › Personal › Personal registrado* se editan varios empleados a la vez.

## Checadores ZKTeco (ADMS)

*Asistencias › Equipos › Checadores*.

1. En el checador: *Menú › Comunicación › Servidor en la nube (ADMS)*: activa **Nombre de dominio**, pon la dirección
   de Odoo (sin `https://`), puerto **443** con HTTPS activado (o la IP de la PC y puerto **8069** para pruebas en local).
2. El checador aparece como **Pendiente de aprobar**: ponle nombre, zona horaria y los **departamentos que checan ahí**
   (vacío = todos; los marcajes de otros departamentos quedan como *ignorados*). **Aprobar**.
3. **Enviar empleados activos**: los da de alta con su clave (número de registro o ID de credencial) y nombre.
4. **Enviar rostros (foto)**, en checadores de luz visible como el **MB10-VL**: el checador registra el rostro con la
   foto tomada en el teléfono al registrar el rostro en Odoo (o la foto de la ficha del empleado), sin pararse frente
   al aparato. Desde entonces, cada rostro nuevo que se registre con el teléfono se manda solo a los checadores
   donde checa esa persona.
5. En *Registros* se ve cada marca que llegó (tomada, ignorada o con error) y en *Órdenes al checador* si el checador
   aceptó cada alta o rostro (*Hecha*) o la rechazó (*Con error*, con su código).

## Horario, comida y tiempo extra

- Horario de **7:00 a 17:00** (hora del empleado; revisa que su zona horaria sea *America/Mexico_City*).
  Entrar después de la hora más la tolerancia es **retardo**.
- Si no marcó la comida, se descuenta **1 hora** de jornadas de 6 horas o más.
- Si alguien sale **una hora o más después** del horario, el teléfono pregunta **¿Es tiempo extra?**
  - *Sí*: queda **por validar** (*Asistencias › Marcajes › Tiempo extra por validar*).
  - *No*: solo cuenta hasta la hora de salida.
  - En la tablet y el checador nadie contesta: una salida tarde queda por validar.
- Horario, tolerancias y comida se cambian en *Asistencias › Configuración › Ajustes*.

## Revisión

Quedan **en revisión** (*Asistencias › Marcajes › Por revisar*): regresos de comer tarde, salidas antes de tiempo y días sin salida (se cierran al día siguiente para que RH capture la hora).

## Reporte de asistencia

*Asistencias › Reporte de asistencia*: entradas, retardos, faltas, vacaciones, permisos e incapacidades por día,
filtrable por **empresa**, **departamento** (los del módulo Empleados), **supervisor** y búsqueda por nombre,
número o puesto. Se descarga en Excel o PDF. En *Marcajes* también se puede filtrar y agrupar por departamento.

## Cargar asistencias a mano

*Asistencias › Cargar › Asistencias a mano*: día, supervisor (agrega su cuadrilla) o *Agregar todo el personal*,
palomea **Asistió** y ajusta entrada, salida y comida. Quedan marcadas como *Cargada a mano por*.

## Registrar rostros

- **Desde el enlace del supervisor o la tablet**: *Registrar rostro de un empleado*, consentimiento, 3 fotos y PIN del dueño del enlace.
- **Con la foto de la ficha** (*Asistencias › Personal › Registrar rostros con foto*): registra de golpe a quien tiene foto.
  Con una sola foto reconoce menos; a quien no reconozca, agréguenle 3 fotos en vivo.

## Clave del empleado

La clave es el **Número de registro del empleado** (pestaña Nómina). Si no tiene, se usa su **ID de credencial**.
Sirve para la tarjeta en la tablet y para buscar en el reporte.

## Seguridad

- Enlace con token secreto; **Generar enlace nuevo** invalida el anterior y revoca sus teléfonos.
- Cada teléfono debe ser aprobado por Recursos Humanos; la clave del teléfono se guarda cifrada (hash).
- La asistencia solo se marca con el rostro (o con tarjeta en la tablet de oficina); no se puede marcar con PIN.
  El PIN solo autoriza el registro de rostros y se bloquea 15 minutos tras 5 intentos fallidos.
- Las huellas faciales no salen del servidor: el teléfono solo envía la huella del marcaje actual.

## Notas

- Reconocimiento facial con [face-api](https://github.com/vladmandic/face-api) (MIT), incluido en `static/lib`.
- La cámara requiere HTTPS (Odoo.sh ya lo tiene) o `localhost`.
- Los datos biométricos son datos personales sensibles (LFPDPPP): recaba consentimiento expreso y por escrito.
