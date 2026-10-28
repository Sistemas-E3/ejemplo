ETRES - VERIFICACIÓN DE GAFETE POR QR (Odoo 18)
================================================

QUÉ HACE
- Agrega a cada empleado un código aleatorio y un QR (pestaña "Gafete QR").
- El QR abre https://<tu-odoo>/gafete/<código>, una página pública que muestra
  foto, nombre, puesto y VIGENTE (verde) / NO VIGENTE (rojo).
- NO VIGENTE en cuanto el empleado se archiva en Odoo. No hay que reimprimir.
- No muestra CURP, NSS ni datos médicos.
- Botón "Regenerar QR" para invalidar un gafete perdido.
- Acción "Descargar QR de gafetes" (lista de empleados > seleccionar > Acción)
  descarga un ZIP con los QR en PNG para cargarlos en Bodno.

INSTALAR EN ODOO.SH
1. Copiar la carpeta etres_badge_verify a la raíz del repositorio de Odoo.sh
   (o a la carpeta de addons que usen).
2. git add etres_badge_verify && git commit -m "Agrega verificación de gafete por QR"
3. Hacer push primero a una rama STAGING y probar ahí.
4. En la base de staging: Aplicaciones > Actualizar lista de aplicaciones >
   buscar "Verificación de gafete" > Instalar.
5. Probar: abrir un empleado > pestaña Gafete QR > escanear el QR con el celular.
   Archivar al empleado y volver a escanear: debe decir NO VIGENTE.
6. Si todo bien, hacer merge a la rama de PRODUCCIÓN e instalar ahí.

NOTA: la liga del QR usa el parámetro web.base.url de Odoo. Verificar que sea
el dominio público correcto (Ajustes > Técnico > Parámetros del sistema)
ANTES de imprimir gafetes.
