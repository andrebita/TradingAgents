-- Covering index for the pending_items -> meetings foreign key (performance advisor 0001).
create index if not exists pending_items_meeting on public.pending_items (meeting_id);
