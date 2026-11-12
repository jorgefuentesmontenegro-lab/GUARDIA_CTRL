create table if not exists public.perfiles (
    user_id uuid primary key references auth.users (id) on delete cascade,
    nombre text not null,
    rol text not null check (rol in ('guardia', 'conductor', 'administrador')),
    created_at timestamptz not null default now()
);

create or replace function public.es_administrador()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.perfiles p
        where p.user_id = (select auth.uid())
          and p.rol = 'administrador'
    );
$$;

revoke all on function public.es_administrador() from public;
grant execute on function public.es_administrador() to authenticated;

alter table public.perfiles enable row level security;
grant select on table public.perfiles to authenticated;

drop policy if exists "perfiles_select_self_or_admin" on public.perfiles;
create policy "perfiles_select_self_or_admin"
on public.perfiles
for select
to authenticated
using (
    user_id = (select auth.uid())
    or (select public.es_administrador())
);

-- Crea cada cuenta en Supabase Auth y luego asígnale un rol desde el editor SQL:
-- insert into public.perfiles (user_id, nombre, rol)
-- select id, 'Nombre completo', 'administrador'
-- from auth.users
-- where email = 'admin@example.com';
