#!/usr/bin/env bash
# Deja el entorno como recién instalado, sin recrear el esquema.
#
#   ./scripts/reset_dev.sh                       # dev: borra TODO
#   ./scripts/reset_dev.sh +5493815550000        # dev: borra solo ese teléfono
#   ./scripts/reset_dev.sh --prod +549381555000  # PRODUCCIÓN: borra solo ese teléfono
#
# Borra memoria en Postgres (conversaciones, mensajes, eventos, jobs, outbox, leads) y las
# claves de Redis de debounce/lock/requeue. No toca el prompt, Langfuse ni el catálogo.
#
# Funciona igual en la máquina local y en el VPS: encuentra los contenedores por labels de
# Compose, así que no hace falta pasar -p ni ENV_FILE.
set -euo pipefail

cd "$(dirname "$0")/.."

ENVIRONMENT=dev
if [[ "${1:-}" == "--prod" ]]; then
  ENVIRONMENT=prod
  shift
fi
PHONE="${1:-}"

# ── Resolver contenedores ────────────────────────────────────────────────────
compose_container() {  # $1 = proyecto, $2 = servicio
  docker ps -q \
    -f "label=com.docker.compose.project=$1" \
    -f "label=com.docker.compose.service=$2" | head -1
}

if [[ "$ENVIRONMENT" == prod ]]; then
  # Producción usa el Postgres compartido del VPS, que no es parte de este stack.
  PG_CONTAINER="$(docker ps -q -f name='^waichatt-postgres$' | head -1)"
  PG_USER=agente_waichatt
  REDIS_CONTAINER="$(compose_container agente-waichatt-prod redis)"
else
  # En el VPS el proyecto es agente-waichatt-dev; en local, el nombre del directorio.
  for project in agente-waichatt-dev agente-waichatt; do
    PG_CONTAINER="$(compose_container "$project" postgres)"
    [[ -n "$PG_CONTAINER" ]] && { REDIS_CONTAINER="$(compose_container "$project" redis)"; break; }
  done
  PG_USER=waichatt
fi

if [[ -z "${PG_CONTAINER:-}" ]]; then
  echo "No encontré el contenedor de Postgres para '$ENVIRONMENT'. ¿Está levantado el stack?" >&2
  exit 1
fi

psql_run() { docker exec -i "$PG_CONTAINER" psql -qtAX -U "$PG_USER" -d agente_waichatt "$@"; }
redis_run() {
  [[ -n "${REDIS_CONTAINER:-}" ]] || { echo "Aviso: no encontré Redis, salteo esas claves." >&2; return 0; }
  docker exec -i "$REDIS_CONTAINER" redis-cli "$@"
}

# ── Reset total ──────────────────────────────────────────────────────────────
if [[ -z "$PHONE" ]]; then
  if [[ "$ENVIRONMENT" == prod ]]; then
    echo "Reset total en PRODUCCIÓN no está permitido: borraría leads y conversaciones reales." >&2
    echo "Usá: ./scripts/reset_dev.sh --prod <telefono>" >&2
    exit 1
  fi
  read -r -p "Vas a borrar TODA la memoria de desarrollo. ¿Seguro? (escribí 'si'): " ok
  [[ "$ok" == "si" ]] || { echo "Cancelado."; exit 1; }

  psql_run <<'SQL'
begin;
truncate table
  crm_leads,
  chat_outbox_messages,
  chat_webhook_jobs,
  chat_processed_events,
  chat_messages,
  chat_conversations
restart identity cascade;
commit;
SQL

  redis_run --scan --pattern 'waichatt:conversation:*' | xargs -r -n 100 \
    docker exec -i "$REDIS_CONTAINER" redis-cli del >/dev/null
  echo "Memoria de desarrollo borrada por completo."
  exit 0
fi

# ── Reset de un solo teléfono ────────────────────────────────────────────────
ESCAPED="${PHONE//\'/\'\'}"

if [[ "$ENVIRONMENT" == prod ]]; then
  read -r -p "Vas a borrar la conversación y el lead de ${PHONE} en PRODUCCIÓN. ¿Seguro? (escribí 'si'): " ok
  [[ "$ok" == "si" ]] || { echo "Cancelado."; exit 1; }
fi

CONV_ID="$(psql_run -c \
  "select id from chat_conversations where external_conversation_id = '${ESCAPED}'" | tr -d '[:space:]')"

if [[ -z "$CONV_ID" ]]; then
  echo "No hay conversación para ${PHONE} en '$ENVIRONMENT'; borro el lead si existe."
fi

psql_run <<SQL
begin;
delete from crm_leads where phone = '${ESCAPED}';
delete from chat_processed_events where external_conversation_id = '${ESCAPED}';
delete from chat_conversations where external_conversation_id = '${ESCAPED}';
commit;
SQL

if [[ -n "$CONV_ID" ]]; then
  redis_run del \
    "waichatt:conversation:${CONV_ID}:debounce" \
    "waichatt:conversation:${CONV_ID}:lock" \
    "waichatt:conversation:${CONV_ID}:requeue" >/dev/null
fi

echo "Conversación de ${PHONE} borrada en '$ENVIRONMENT'. El próximo mensaje arranca de cero."
