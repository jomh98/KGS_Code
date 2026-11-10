-- 질문 로그 테이블 (PRD §5.6, F-15)
-- Supabase 대시보드 → SQL Editor 에서 실행한다.
-- 앱은 secrets의 [supabase] url / key 로 접속한다. key는 서버에서만 쓰이므로 service_role 키를 쓰되
-- 절대 저장소에 커밋하지 않는다.

create table if not exists public.query_logs (
    id                    text primary key,
    timestamp             timestamptz not null default now(),
    session_id            text not null,          -- 무작위 익명 ID (IP 아님)
    question              text not null,          -- 개인정보 마스킹 후 저장
    answer                text not null,
    cited_sources         jsonb not null default '[]'::jsonb,
    filters               jsonb not null default '{}'::jsonb,
    latency_ms            integer not null default 0,
    model                 text not null default '',
    feedback              text,                   -- 'up' / 'down'
    data_version          text,
    insufficient_evidence boolean not null default false,
    error                 boolean not null default false,  -- API 오류로 답변 실패
    removed_citations     jsonb not null default '[]'::jsonb  -- 존재하지 않아 지운 [근거 n] (환각 인용 추적)
);

-- 이미 테이블을 만든 경우:
-- alter table public.query_logs add column if not exists error boolean not null default false;
-- alter table public.query_logs add column if not exists removed_citations jsonb not null default '[]'::jsonb;

create index if not exists query_logs_timestamp_idx on public.query_logs (timestamp desc);
create index if not exists query_logs_insufficient_idx on public.query_logs (insufficient_evidence)
    where insufficient_evidence;

-- 행 수준 보안: 익명(anon) 키로는 읽기·쓰기를 막는다. 앱은 service_role 키로 접속한다.
alter table public.query_logs enable row level security;

-- 보관 기간 1년 (PRD §5.6). Supabase의 pg_cron 확장을 켠 경우:
-- select cron.schedule('purge-query-logs', '0 3 * * *',
--   $$delete from public.query_logs where timestamp < now() - interval '1 year'$$);
