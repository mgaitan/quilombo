# Plan inicial de mercado y marketing

Actualizado: 2026-08-15

## Tesis

Quilombo no debe presentarse como otra aplicación de inventario. Su categoría inicial es **memoria
física conversable**: decirle a un agente qué se guardó, preguntarle dónde está y pedirle ayuda para
ordenar o reponer sin aprender una interfaz especializada.

La promesa corta:

> Tus cosas, encontrables. Mostrale o contale a tu IA qué guardaste y después preguntale dónde está.

La historia larga:

> Llamamos ordenadores a las computadoras, pero hasta ahora ordenaron sobre todo información
> digital. Quilombo conecta agentes con talleres, bibliotecas y depósitos para que también ayuden a
> ordenar la vida real.

El producto no paga inferencia de IA: el usuario trae ChatGPT, Claude u otro cliente. Quilombo
aporta persistencia, permisos, transacciones y MCP. Esa arquitectura reduce el costo variable y
permite mejorar junto con los agentes sin quedar atado a un modelo.

## Mercado: dónde empezar

No conviene calcular un TAM sumando todos los hogares, bibliotecas y almacenes. Primero hay que
probar una cuña con dolor frecuente, acceso directo a usuarios y una conversación natural como
interfaz.

### 1. Talleres personales y makers

Ejemplos: electrónica, carpintería, impresión 3D, bicicletas, costura, modelismo y mantenimiento
doméstico.

- Dolor: piezas pequeñas, compras duplicadas, ubicaciones cambiantes y proyectos intermitentes.
- Ventaja: el dueño conoce el vocabulario y puede corregir al agente.
- Adquisición: comunidades maker, canales de YouTube, clubes, Fab Labs y contactos personales.
- Disposición a pagar: baja o media; es un excelente segmento para aprendizaje y recomendación.

### 2. Pequeños talleres y equipos de reparación

Ejemplos: service de electrónica, bicicletas, mantenimiento edilicio, utilería y cooperativas.

- Dolor: el costo de no encontrar o quedarse sin una pieza supera al precio de una suscripción.
- Ventaja: varias personas consultan el mismo inventario y préstamos/reposición tienen valor claro.
- Adquisición: pilotos locales y referencias entre talleres.
- Disposición a pagar: media; es el primer candidato a plan pago.

### 3. Bibliotecas y colecciones personales

Ejemplos: libros, discos, juegos, herramientas, repuestos y archivo familiar.

- Dolor: saber si algo existe, en qué estante está, qué edición es y a quién se prestó.
- Ventaja: ISBN y metadatos públicos reducen la carga inicial; el demo visual se entiende rápido.
- Disposición a pagar: baja en general, mayor en coleccionistas con cientos o miles de piezas.

### 4. Escuelas, bibliotecas comunitarias y tool libraries

- Dolor: inventario compartido, préstamos, pérdidas y rotación de responsables.
- Ventaja: misión alineada y casos públicos interesantes.
- Riesgo: permisos, soporte y compra institucional alargan la venta.
- Estrategia: pilotos seleccionados después de validar colaboración y préstamos.

No priorizar todavía depósitos industriales, salud, alimentos regulados ni activos contables. Esos
mercados exigen códigos, auditoría, integraciones, SLA y compliance que desviarían la v1.

## Competencia y sustitutos

El competidor principal no es otra startup: es memoria + etiquetas + WhatsApp + una planilla que se
abandona. Las aplicaciones establecidas prueban que hay demanda, pero también muestran dónde no
conviene competir de frente.

| Alternativa | Fortaleza | Oportunidad para Quilombo |
| --- | --- | --- |
| Planilla / notas | Gratis y flexible | La conversación reduce mantenimiento y recupera contexto |
| [Libib](https://www.libib.com/pricing) | Catálogo por ISBN/UPC; gratis hasta 5.000 ítems; Pro con préstamos | Ser genérico y agent-first, no sólo una biblioteca con escáner |
| [Sortly](https://www.sortly.com/pricing/) | Inventario SMB maduro, fotos, QR, alertas y check-in/out | Mucho más simple y barato para equipos pequeños; IA del usuario |
| [Itemtopia](https://www.itemtopia.com/pricing) | Hogar, colecciones, documentos y garantías | Persistencia abierta y conversación entre distintos agentes |
| [Grocy](https://grocy.info/) | Open source, hogar, mínimos y códigos de barra | Cero instalación y dominio más amplio que alimentos/hogar |

La diferenciación defendible no es “tiene IA”. Es:

1. protocolo MCP y skill bien curada;
2. memoria física portable, con procedencia y API abierta;
3. modelo espacial flexible, desde coordenadas hasta referencias relativas;
4. costo pequeño porque la inteligencia vive en el cliente;
5. experiencia conversacional para personas como Oscar, que no quieren administrar inventario.

## ¿Alguien pagaría?

Hipótesis: **sí, pero no todos y no todavía**.

Un usuario doméstico casual tiene alternativas gratuitas y tolera bastante desorden. No se debe
construir billing suponiendo que pagará. Un taller que pierde treinta minutos por semana, compra
repuestos duplicados o necesita compartir conocimiento sí puede justificar una suscripción pequeña.

Probar tres ofertas manualmente:

| Plan hipotético | Precio a testear | Para quién | Límites/valor |
| --- | ---: | --- | --- |
| Personal | Gratis | Oscar, maker, biblioteca propia | 1 workspace, uso agentico completo, exportación |
| Plus | USD 5/mes o 45/año | Coleccionista o familia | varios espacios, préstamos, alertas e historial |
| Taller | USD 15-25/mes | equipo pequeño | colaboradores, roles, auditoría, soporte y webhooks |

No implementar pagos hasta conseguir al menos cinco compromisos explícitos del tipo «lo usaría a
USD X cuando tenga Y». Una encuesta de “¿pagarías?” no alcanza. Ofrecer un piloto y pedir tarjeta,
reserva reembolsable o carta de intención cuando la propuesta ya resuelva el trabajo.

## Economía operativa

La unidad de datos es pequeña: texto, JSON y cantidades; no guardamos fotos ni videos. El agente y
su proveedor absorben inferencia y visión. Los costos propios son web, PostgreSQL, correo
transaccional futuro, observabilidad y soporte.

- Render permite probar un web service gratis, con horas y suspensión por inactividad. Su Postgres
  gratuito expira a los 30 días, por lo que no es apropiado para datos reales persistentes. Ver
  [límites gratuitos de Render](https://render.com/docs/free).
- Neon ofrece un plan gratuito con scale-to-zero y almacenamiento limitado por proyecto; el plan
  pago es por uso. Es el mejor candidato actual para la base temprana. Ver
  [precios de Neon](https://neon.com/pricing).
- Definir alertas de costo y pasar a infraestructura paga antes de prometer confiabilidad. “Gratis”
  es un entorno de validación, no una propuesta de SLA.

Medir desde el primer piloto:

- filas y bytes por 1.000 ítems;
- conexiones y tiempo activo de base por usuario;
- requests MCP por usuario activo;
- tiempo de soporte y onboarding;
- costo mensual total / usuarios activos y / workspaces pagos.

El margen de software debería ser alto; durante los primeros meses el costo dominante será humano:
ayudar a inventariar, observar fallos del agente y mejorar la skill.

## Activación: el momento de valor

Un registro no es activación. Definición propuesta:

> Conectó un agente, guardó al menos 20 objetos y obtuvo una respuesta útil a «¿dónde está X?» en
> las primeras 48 horas.

Onboarding ideal:

1. Crear cuenta; `Home` ya existe.
2. Conectar ChatGPT o Claude mediante OAuth.
3. Elegir `Taller`, `Biblioteca` u `Otro` como plantilla conversacional, sin imponer esquema.
4. Inventariar un único cajón o estante, no todo el mundo físico.
5. Hacer una pregunta de recuperación inmediatamente.
6. Volver otro día para registrar un movimiento o corrección.

La primera sesión debe durar menos de diez minutos y producir una búsqueda exitosa. El inventario
total puede crecer de forma oportunista: registrar algo cuando se guarda, se busca o se ordena.

Mientras agregar un MCP personalizado requiera modo desarrollador, el producto necesita una guía
de tres minutos y una demo en video. No publicar un botón “Add to ChatGPT” que sólo abra
instrucciones. Crear un deep link únicamente si existe un contrato oficial estable. La documentación
oficial actual presenta plugins para extender ChatGPT y Codex, pero la disponibilidad y revisión de
un directorio deben verificarse antes de prometer distribución. Mantener como tareas separadas:

- empaquetar skill + URL MCP con una descarga;
- postular el proyecto al [showcase de OpenAI](https://developers.openai.com/community);
- seguir el camino oficial de publicación cuando esté documentado y Quilombo tenga privacidad,
  soporte, política de borrado y un onboarding probado.

## Plan de 90 días

### Días 1-14: cinco talleres reales

Objetivo: comprobar que la conversación supera a una planilla.

Acciones:

1. Reclutar a Oscar y cuatro personas conocidas con talleres distintos.
2. Hacer onboarding presencial o por videollamada; grabar notas, no medios del inventario.
3. Limitar cada piloto a un cajón o estante con 20-50 objetos.
4. Pedir tres tareas: registrar, encontrar y corregir algo movido.
5. Anotar cada momento en que el usuario necesita entender `workspace`, claves o estructura.
6. Publicar una demo de 60-90 segundos: mostrar cajón, preguntar después, encontrar objeto.

Criterio de paso: 4 de 5 completan una búsqueda útil y 3 vuelven sin recordatorio dentro de dos
semanas.

### Días 15-30: onboarding repetible

Objetivo: que alguien desconocido se conecte sin asistencia del creador.

Acciones:

1. Convertir los tropiezos en mejoras de la skill, errores y guía de conexión.
2. Implementar importación conversacional inicial, pistas visuales y correcciones fáciles.
3. Publicar el artículo del blog y compartirlo con un formulario de piloto de cinco preguntas.
4. Crear una página pública con demo, privacidad, estado del servicio y CTA `Probar con mi agente`.
5. Invitar a 15 testers desde comunidades propias; no comprar tráfico.

Criterio de paso: 50% conecta el agente, 40% llega a 20 ítems y al menos 5 usuarios hacen una
segunda sesión.

### Días 31-60: dos segmentos en paralelo

Objetivo: comparar taller vs. biblioteca sin mezclar mensajes.

Experimento A, taller:

- demo «¿dónde están los tornillos para madera?»;
- faltantes y stock bajo;
- préstamo de una herramienta;
- canal: makers, repair cafés, Fab Labs, newsletters y canales técnicos en español.

Experimento B, biblioteca:

- alta por ISBN;
- pista de lomo y libros vecinos;
- autores desparramados y préstamos;
- canal: BookTube/BookTok pequeños, clubes de lectura, bibliotecarios y coleccionistas.

Crear una landing y una secuencia de onboarding para cada una, manteniendo el mismo producto.
Entrevistar a diez usuarios por segmento.

Criterio de paso: elegir el segmento con mayor activación, retorno semanal y urgencia declarada; no
el de más likes.

### Días 61-90: disposición a pagar

Objetivo: obtener evidencia de precio.

Acciones:

1. Mostrar límites y precios hipotéticos a usuarios activos, no a audiencia general.
2. Ofrecer diez cupos de `Taller fundador` con onboarding personal y precio garantizado por un año.
3. Pedir cinco compromisos de pago antes de integrar Stripe.
4. Conseguir tres testimonios específicos con resultado medible: tiempo ahorrado, compra evitada o
   préstamo recuperado.
5. Publicar uno o dos casos de uso, incluyendo errores y cómo se corrigieron.
6. Decidir: cobrar, continuar gratis para aprender o concentrarse en otro segmento.

Criterio de paso: 5 compromisos a un precio común y retención de cuatro semanas mayor al 30% entre
usuarios activados. Si no ocurre, no añadir planes: revisar dolor, onboarding o segmento.

## Canales y piezas concretas

Prioridad alta:

- blog propio y repositorio abierto;
- demo corta vertical para compartir por WhatsApp, YouTube Shorts y redes;
- onboarding acompañado y referidos de los primeros usuarios;
- comunidades maker y de software libre en español;
- directorios/showcases oficiales de agentes cuando acepten el formato del producto.

Prioridad media:

- artículos SEO basados en problemas: «cómo inventariar tornillos», «cómo encontrar libros en casa»,
  «inventario por voz», «control de herramientas prestadas»;
- alianzas con un Fab Lab, repair café o biblioteca comunitaria;
- plantillas compartibles de taller, biblioteca y repuestos.

Evitar al inicio:

- anuncios pagos;
- Product Hunt sin onboarding autoservicio;
- acuerdos empresariales largos;
- campañas genéricas sobre “IA para inventario”.

Activos mínimos:

1. demo de Oscar o un usuario equivalente;
2. página de privacidad en lenguaje claro;
3. estado y mecanismo de exportar/borrar datos;
4. guía ChatGPT/Claude probada en móvil y escritorio;
5. tres conversaciones de ejemplo completas;
6. formulario de piloto con tipo de espacio, cantidad aproximada, agente usado, dolor principal y
   disponibilidad para una sesión.

## Mensajes para probar

General:

> Quilombo es la memoria de tus cosas. Decile a tu IA qué guardaste y después preguntale dónde está.

Taller:

> Dejá de comprar el tornillo que ya tenías. Inventariá conversando y encontrá herramientas,
> repuestos y consumibles desde el teléfono.

Biblioteca:

> Preguntale a tu biblioteca dónde quedó un libro, cómo reconocerlo y qué otros libros tiene cerca.

Equipo:

> El conocimiento del taller deja de vivir en la cabeza de una sola persona.

Evitar “organiza todo automáticamente”. El agente trabaja con observaciones falibles; la confianza
se gana mostrando procedencia, incertidumbre y correcciones.

## Métricas

Producto:

- porcentaje que completa OAuth/MCP;
- tiempo hasta primer objeto y primera búsqueda exitosa;
- objetos/ubicaciones registrados en día 1 y día 30;
- búsquedas con resultado útil / búsquedas totales;
- correcciones por cada 100 escrituras;
- usuarios que vuelven semana 1 y semana 4;
- cantidad de movimientos, reposiciones y préstamos registrados.

Negocio:

- pilotos contactados / aceptados / activados;
- referidos por usuario activado;
- compromisos de pago y precio aceptado;
- conversión a pago cuando exista billing;
- costo operativo y soporte por workspace activo;
- cancelación y motivo.

No usar cantidad de cuentas, ítems cargados o seguidores como métrica principal si no producen
búsquedas y retornos.

## Guion de entrevista

1. Contame la última vez que no encontraste algo que sabías que tenías.
2. ¿Qué hiciste y cuánto tiempo o dinero costó?
3. ¿Cómo registrás hoy ubicaciones, préstamos o faltantes?
4. ¿Qué parte de ese método abandonás y por qué?
5. Probemos Quilombo con un solo cajón/estante. ¿Qué esperabas que entendiera?
6. ¿Qué dato te incomoda guardar en un servicio externo?
7. Si mañana no pudieras usarlo, ¿qué extrañarías?
8. ¿Quién más necesita esta información?
9. Para resolver esto cada semana, ¿pagarías USD 5, 15 o 25? ¿Con qué condición concreta?

Observar conducta antes de explicar funciones. No pedir ideas de features hasta entender el trabajo
y la alternativa actual.

## Decisiones después de 90 días

- **Avanzar con talleres pagos** si el uso compartido, reposición y pérdidas generan retorno y cinco
  equipos aceptan precio.
- **Mantener personal gratis + monetizar equipos** si hogares activan y recomiendan pero no pagan.
- **Concentrarse en bibliotecas** si ISBN, vecinos y préstamos muestran mucha más retención.
- **Seguir como proyecto abierto** si hay utilidad personal/comunitaria pero no disposición a pagar.
- **Detener expansión** si ni con onboarding acompañado los usuarios vuelven a consultar. Más
  funciones no arreglarán una memoria que nadie mantiene.

La pregunta rectora no es cuántas cosas puede representar Quilombo. Es si registrar una cosa cuesta
menos que volver a perderla.
