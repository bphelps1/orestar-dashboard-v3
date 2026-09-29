-- User roles for role-based access control
-- Roles: 'admin' (full access), 'reviewer' (can see admin page, review merges)

create table if not exists user_roles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references auth.users(id) on delete cascade,
  role text not null default 'viewer',
  assigned_at timestamptz default now()
);

alter table user_roles enable row level security;

-- Any authenticated user can read their own role
create policy "Read own role" on user_roles
  for select to authenticated
  using (auth.uid() = user_id);

-- Admins can read all roles
create policy "Admin read all roles" on user_roles
  for select to authenticated
  using (
    exists (select 1 from user_roles where user_id = auth.uid() and role = 'admin')
  );

-- Admins can manage all roles
create policy "Admin manage roles" on user_roles
  for all to authenticated
  using (
    exists (select 1 from user_roles where user_id = auth.uid() and role = 'admin')
  )
  with check (
    exists (select 1 from user_roles where user_id = auth.uid() and role = 'admin')
  );
