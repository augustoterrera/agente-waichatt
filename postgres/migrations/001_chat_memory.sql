-- Memoria conversacional del agente vendedor de Waichatt + CRM mínimo de leads.
-- Correr una vez en la database propia del agente (Postgres del VPS):
--   psql "$DATABASE_URL" -f postgres/migrations/001_chat_memory.sql
--
-- A diferencia de la versión Supabase de ferrepro, acá NO hay funciones RPC: el agente
-- habla con Postgres directo (psycopg) y las operaciones atómicas son sentencias únicas.

-- RESET (opcional): si querés recrear desde cero, descomentá este bloque.
-- drop table if exists crm_leads cascade;
-- drop table if exists chat_outbox_messages cascade;
-- drop table if exists chat_webhook_jobs cascade;
-- drop table if exists chat_processed_events cascade;
-- drop table if exists chat_messages cascade;
-- drop table if exists chat_conversations cascade;

create table if not exists chat_conversations (
  id bigserial primary key,
  channel text not null,
  external_conversation_id text not null,   -- teléfono en YCloud; conversation id en Chatwoot
  external_contact_id text,
  account_id text,                          -- número propio en YCloud; account id en Chatwoot
  state jsonb not null default '{}'::jsonb, -- bot_apagado, contexto del contacto, última clasificación
  locked_until timestamptz,
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (channel, external_conversation_id)
);

create index if not exists chat_conversations_last_seen_idx
  on chat_conversations (last_seen_at desc);

create table if not exists chat_messages (
  id bigserial primary key,
  conversation_id bigint not null references chat_conversations(id) on delete cascade,
  external_message_id text,
  role text not null check (role in ('user', 'assistant', 'system')),
  content text not null,
  raw_payload jsonb not null default '{}'::jsonb,
  processing_status text not null default 'processed',
  processed_at timestamptz,
  processing_error text,
  created_at timestamptz not null default now(),
  unique (conversation_id, external_message_id, role)
);

create index if not exists chat_messages_conversation_created_idx
  on chat_messages (conversation_id, created_at desc);
create index if not exists chat_messages_processing_idx
  on chat_messages (conversation_id, processing_status, created_at);

create table if not exists chat_processed_events (
  event_key text primary key,
  channel text not null,
  external_conversation_id text,
  external_message_id text,
  raw_payload jsonb not null default '{}'::jsonb,
  status text not null default 'received',
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists chat_processed_events_created_idx
  on chat_processed_events (created_at desc);

create table if not exists chat_webhook_jobs (
  id bigserial primary key,
  event_key text not null references chat_processed_events(event_key) on delete cascade,
  channel text not null,
  external_conversation_id text not null,
  external_message_id text,
  status text not null default 'queued',
  attempts integer not null default 0,
  max_attempts integer not null default 5,
  error text,
  raw_payload jsonb not null default '{}'::jsonb,
  run_at timestamptz not null default now(),
  locked_at timestamptz,
  worker_id text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  completed_at timestamptz
);

create unique index if not exists chat_webhook_jobs_event_key_idx
  on chat_webhook_jobs (event_key);
create index if not exists chat_webhook_jobs_status_created_idx
  on chat_webhook_jobs (status, created_at);
create index if not exists chat_webhook_jobs_status_run_at_idx
  on chat_webhook_jobs (status, run_at);
create index if not exists chat_webhook_jobs_locked_at_idx
  on chat_webhook_jobs (status, locked_at);

create table if not exists chat_outbox_messages (
  id bigserial primary key,
  conversation_id bigint not null references chat_conversations(id) on delete cascade,
  external_conversation_id text not null,
  channel text not null,
  content text not null,
  -- Mensaje con adjunto del catálogo: {"type": "image"|"video"|"document", "link": url, "caption": str|null}
  media jsonb,
  status text not null default 'pending',
  idempotency_key text not null,
  attempts integer not null default 0,
  max_attempts integer not null default 5,
  error text,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  unique (idempotency_key)
);

create index if not exists chat_outbox_messages_status_created_idx
  on chat_outbox_messages (status, created_at);

-- CRM mínimo: un lead por teléfono con etapa del embudo, flags y datos de calificación.
-- Lo llena el clasificador después de cada respuesta del bot. Consultable desde cualquier
-- dashboard que se conecte a esta database.
create table if not exists crm_leads (
  id bigserial primary key,
  phone text not null unique,
  name text,
  inmobiliaria text,
  es_dueno boolean,
  consultas text,        -- volumen de consultas que dice recibir (texto libre: "50 por día")
  equipos text,          -- equipos/áreas de la inmobiliaria (texto libre)
  stage text not null default 'curioso',
  flags jsonb not null default '[]'::jsonb,
  conversation_id bigint references chat_conversations(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists crm_leads_stage_idx on crm_leads (stage, updated_at desc);

-- ── Triggers ──────────────────────────────────────────────────────────────
create or replace function touch_chat_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists touch_chat_conversations_updated_at on chat_conversations;
create trigger touch_chat_conversations_updated_at
before update on chat_conversations
for each row execute function touch_chat_updated_at();

drop trigger if exists touch_chat_processed_events_updated_at on chat_processed_events;
create trigger touch_chat_processed_events_updated_at
before update on chat_processed_events
for each row execute function touch_chat_updated_at();

drop trigger if exists touch_crm_leads_updated_at on crm_leads;
create trigger touch_crm_leads_updated_at
before update on crm_leads
for each row execute function touch_chat_updated_at();
