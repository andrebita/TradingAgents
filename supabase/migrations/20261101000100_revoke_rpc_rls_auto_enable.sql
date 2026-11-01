-- rls_auto_enable is Supabase's event trigger that turns row level security on for every new table in public.
-- It runs as an event trigger, which needs no EXECUTE grant; the grant only exposed it over the REST API
-- (security advisor 0028/0029).
revoke execute on function public.rls_auto_enable() from public, anon, authenticated;
