-- The investment committee's state, for one investor (review 6: memory between meetings, executions, pending
-- items). Single user: every table has row level security on and no policy, so the anon and authenticated
-- roles see nothing; only the service role (the committee's code, with a key kept outside the repository)
-- and the project's dashboard reach the data. No personal data lives in this file.

-- The written policy, versioned: the committee reads the latest one in force.
create table public.policies (
  id bigint generated always as identity primary key,
  valid_from date not null,
  body jsonb not null,                      -- InvestmentPolicy, as committee/policy.py reads it
  note text,
  created_at timestamptz not null default now()
);

-- The wealth as the committee reads it (committee/wealth.py), one snapshot per update.
create table public.wealth_snapshots (
  id bigint generated always as identity primary key,
  as_of date not null,
  source text not null,                     -- where it came from: an export, the investor, a statement
  body jsonb not null,
  note text,
  created_at timestamptz not null default now()
);

-- Every trade from the B3 "Negociação" export. ``occurrence`` numbers identical rows of the same day, so an
-- overlapping export can be loaded again without duplicating anything.
create table public.trades (
  id bigint generated always as identity primary key,
  trade_date date not null,
  side text not null check (side in ('compra', 'venda')),
  market text,
  broker text not null,
  code text not null,
  quantity numeric not null check (quantity > 0),
  price numeric not null,
  value numeric not null,
  occurrence int not null default 1,
  source text not null,
  imported_at timestamptz not null default now(),
  unique (trade_date, side, broker, code, quantity, price, occurrence)
);
create index trades_code_date on public.trades (code, trade_date);

-- The average cost of each position, with how it was obtained.
create table public.average_costs (
  code text primary key,
  quantity numeric not null,
  average_price numeric not null,
  total_cost numeric not null,
  method text not null,                     -- 'b3_negociacao', 'investidor', 'declaracao_irpf'
  assumptions text,
  computed_at timestamptz not null default now()
);

-- Each committee meeting: the decision log line (decisoes.jsonl) and where the letter is.
create table public.meetings (
  id bigint generated always as identity primary key,
  meeting_date date not null,
  mode text not null,
  record jsonb not null,
  letter_md text,
  letter_url text,
  cost_usd numeric,
  note text,
  created_at timestamptz not null default now()
);

-- The orders a meeting recommended, and what became of them: the loop from recommendation to execution.
create table public.orders (
  id bigint generated always as identity primary key,
  meeting_id bigint not null references public.meetings (id) on delete cascade,
  side text not null check (side in ('compra', 'venda', 'resgatar', 'aplicar')),
  asset text not null,
  quantity numeric,
  price numeric,
  value numeric not null,
  tax numeric,
  reason text,
  status text not null default 'proposta'
    check (status in ('proposta', 'aprovada', 'executada', 'parcial', 'cancelada', 'expirada')),
  executed_quantity numeric,
  executed_price numeric,
  executed_on date,
  note text,
  updated_at timestamptz not null default now()
);
create index orders_meeting on public.orders (meeting_id);

-- What the investor, the system or a specialist must do, kept until it is closed: no item is asked twice.
create table public.pending_items (
  id bigint generated always as identity primary key,
  key text not null unique,                 -- stable, e.g. 'preco_medio:IAAG11'
  opened_on date not null,
  due_on date,
  meeting_id bigint references public.meetings (id) on delete set null,
  owner text not null check (owner in ('investidor', 'sistema', 'especialista')),
  text text not null,
  status text not null default 'aberta' check (status in ('aberta', 'resolvida', 'cancelada')),
  resolved_on date,
  resolution text,
  updated_at timestamptz not null default now()
);
create index pending_open on public.pending_items (status) where status = 'aberta';

alter table public.policies enable row level security;
alter table public.wealth_snapshots enable row level security;
alter table public.trades enable row level security;
alter table public.average_costs enable row level security;
alter table public.meetings enable row level security;
alter table public.orders enable row level security;
alter table public.pending_items enable row level security;
