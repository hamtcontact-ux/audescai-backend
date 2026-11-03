# AudescAI — Planes, tokens y rentabilidad

Este documento explica cómo quedó el modelo de negocio después del rediseño y
cómo se calcula cada precio. La fuente de verdad de los valores está en `.env`
y en `tu_script.py`; la UI de `index.html`, `dashboard.html` y `admin.html`
muestra esos mismos números.

---

## 1. Seguridad de la API key de OpenAI

La clave de OpenAI ya **no se pide ni se guarda en el navegador**.

- `OPENAI_API_KEY` vive únicamente en el `.env` del servidor.
- Las claves de los usuarios Pro se guardan en la subcolección privada
  `usuarios/{uid}/privado/credenciales`.
- Solo el Superadministrador (`editor.online.latam@gmail.com`) puede leer o
  escribir esa subcolección.
- El documento público del usuario solo guarda `has_api_key: true/false`.
- Al procesar un video, el backend resuelve la clave por `uid` desde Redis.
  El frontend nunca envía la clave.
- Los usuarios Free usan los modelos gratuitos de OpenRouter y **no consumen**
  la API key de OpenAI.
- El panel de administración migra automáticamente cualquier clave antigua que
  todavía esté en el documento público.

### Flujo para asignar una clave a un usuario Pro

1. Entra a `admin.html` con la cuenta Superadministrador.
2. Busca al usuario y pulsa **Gestionar**.
3. Cambia el rol a **Pro**.
4. Pega la API key de OpenAI en el campo privado.
5. Ajusta la cuota de tokens (el preset **5.44M / 180 min · Pro** ya está listo).
6. Guarda. El panel escribe la clave en la subcolección privada y la sincroniza
   con Redis para que el backend pueda usarla.

---

## 2. Supuestos de consumo

| Concepto | Valor |
|---|---:|
| Tokens de visión por minuto | 30,000 |
| Tokens de texto por minuto | 200 |
| **Tokens totales por minuto** | **30,200** |
| Costo conservador por 1M tokens | $2.50 USD |
| **Costo directo por minuto** | **$0.0755 USD** |

El costo de $2.50/1M es conservador: si el consumo real es menor, el margen
sube. El cálculo no incluye costos de infraestructura, almacenamiento ni
pasarela de pago; esos deben cubrirse con el margen restante.

---

## 3. Planes

### Renovación de cuota

La cuota de tokens se renueva automáticamente cada **30 días** desde la última
recarga o desde la creación del perfil. El backend detecta el vencimiento al
procesar el siguiente video y reinicia el contador antes de validar la cuota.
El administrador también puede reiniciar el consumo manualmente desde el panel.

### Gratis — $0/mes

- 50,000 tokens/mes.
- Equivalencia: **≈ 1.66 min de video**.
- Modelos gratuitos de OpenRouter.
- No requiere API key de OpenAI.
- Costo para el negocio: $0 en API de OpenAI (sujeto a límites de OpenRouter).

### Pro — $29/mes

- 5,436,000 tokens/mes.
- Equivalencia: **180 min de video (3 horas)**.
- Modelo GPT-4o-mini con visión.
- Incluye API key de OpenAI asignada por el administrador.
- Lotes de hasta 50 proyectos y procesamiento prioritario.
- Minutos extra: **$0.19/min**.

| Cálculo | Valor |
|---|---:|
| Tokens del plan | 5,436,000 |
| Costo API estimado | $13.59 |
| Precio | $29.00 |
| Ganancia bruta | **$15.41** |
| Margen bruto | **53.1%** |
| Costo por minuto | $0.0755 |
| Precio por minuto | $0.1611 |
| Precio mínimo para 50% de margen | $27.18 |

**Por qué $29:** con 180 minutos el costo conservador es $13.59. Para garantizar
al menos 50% de margen, el precio mínimo es $27.18. $29 deja un pequeño colchón
y un margen de 53.1%, por encima del objetivo.

### Empresarial — desde $99/mes

- 18,120,000 tokens/mes.
- Equivalencia: **600 min de video (10 horas)**.
- API key dedicada, SLA, SSO, LMS y despliegue on-premise.

| Cálculo | Valor |
|---|---:|
| Tokens del plan | 18,120,000 |
| Costo API estimado | $45.30 |
| Precio desde | $99.00 |
| Ganancia bruta | **$53.70** |
| Margen bruto | **54.2%** |

---

## 4. Minutos extra y margen

- Precio de venta: **$0.19/min**.
- Costo conservador: **$0.0755/min**.
- Ganancia: **$0.1145/min**.
- Margen: **60.3%**.

Esto permite vender recargas sin destruir el margen del plan base.

---

## 5. Dónde cambiar los valores

En `.env`:

```env
OPENAI_API_KEY=sk-...                 # solo servidor, nunca frontend
TARGET_GROSS_MARGIN_PERCENT=50
DEFAULT_VISION_TOKENS_PER_MIN=30000
DEFAULT_TEXT_TOKENS_PER_MIN=200
API_COST_PER_MILLION_TOKENS=2.50

PLAN_FREE_TOKENS=50000
PLAN_PRO_TOKENS=5436000
PLAN_PRO_MINUTES=180
PLAN_PRO_PRICE_USD=29
PLAN_CUSTOM_TOKENS=18120000
PLAN_CUSTOM_MINUTES=600
PLAN_CUSTOM_PRICE_USD=99
PLAN_OVERAGE_PRICE_PER_MIN=0.19
```

Después de cambiar precios o cuotas, actualiza también los textos visibles de
`index.html` y `dashboard.html` si quieres que la landing y el panel muestren
los mismos números.

---

## 6. Recomendaciones operativas

1. **Configura `OPENAI_API_KEY` en el servidor** antes de probar el plan Pro.
2. **Configura `OPENROUTER_API_KEY`** para que los usuarios Free puedan usar los
   modelos gratuitos; sin esa clave, OpenRouter rechaza las solicitudes.
3. **Despliega las reglas de Firestore** (`firestore.rules`) para activar la
   subcolección privada y la protección de `has_api_key`.
4. **Verifica la sincronización con Redis** al asignar una clave desde el panel.
   Si Redis se reinicia, vuelve a guardar la clave del usuario Pro desde el panel.
5. **Monitorea el consumo real** en el panel de administración. Si el promedio
   real baja de 30,200 tokens/min, puedes subir los minutos del plan o bajar el
   precio sin perder el margen objetivo.
6. **No compartas la API key del servidor** ni la incluyas en repositorios,
   capturas o mensajes. El archivo `.env` ya está en `.gitignore`.
