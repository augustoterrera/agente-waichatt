-- Borra TODA la memoria de desarrollo (conversaciones, mensajes, jobs, outbox, leads)
-- sin tocar el esquema: no hay que volver a correr la migración.
--
--   docker compose exec -T postgres psql -U waichatt -d agente_waichatt < postgres/reset_dev.sql
--
-- Para borrar solo un teléfono, usá scripts/reset_dev.sh <telefono>.

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
