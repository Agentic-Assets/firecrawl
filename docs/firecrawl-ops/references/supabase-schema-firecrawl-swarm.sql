-- Optional Supabase schema for firecrawl_swarm_pipeline.py

create table if not exists public.firecrawl_swarm_runs (
  id bigserial primary key,
  run_id uuid not null unique,
  created_at timestamptz not null default now(),
  summary jsonb not null,
  config jsonb not null
);

create table if not exists public.firecrawl_swarm_items (
  id bigserial primary key,
  run_id uuid not null,
  url text not null,
  stage text not null,
  model_profile text not null,
  model_name text not null,
  success boolean,
  access_status text,
  quality text,
  confidence double precision,
  markdown_len integer,
  error text,
  provenance jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_firecrawl_swarm_items_run_id on public.firecrawl_swarm_items(run_id);
create index if not exists idx_firecrawl_swarm_items_quality on public.firecrawl_swarm_items(quality);
create index if not exists idx_firecrawl_swarm_items_access on public.firecrawl_swarm_items(access_status);
