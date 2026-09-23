-- Внешняя аналитика Lamoda. Отдельная схема, чтобы не смешивать с ботами JOTO.
-- Работает и в Supabase, и в обычном Postgres на VPS.

create schema if not exists lamoda_market;

-- Справочник товаров (обновляется при каждом снимке карточки)
create table if not exists lamoda_market.products (
    sku           text primary key,           -- артикул Lamoda, например MP002XM0VXYZ
    name          text,
    brand         text,
    seller        text,
    category_path text,                       -- «Мужчинам / Одежда / Футболки»
    season        text,
    composition   text,
    url           text,
    first_seen    date not null default current_date,
    last_seen     date not null default current_date,
    updated_at    timestamptz not null default now()
);
create index if not exists products_brand_idx  on lamoda_market.products (brand);
create index if not exists products_seller_idx on lamoda_market.products (seller);

-- Ежедневный снимок по размеру: цена, остаток, рейтинг
create table if not exists lamoda_market.snapshots (
    snap_date     date    not null,
    sku           text    not null references lamoda_market.products (sku),
    size          text    not null default '',   -- '' если товар без размеров
    price         numeric(12, 2),                -- цена продажи, ₽
    old_price     numeric(12, 2),                -- цена до скидки, ₽
    stock         integer,                       -- точный остаток, если виден; иначе null
    in_stock      boolean not null,
    rating        numeric(3, 2),
    reviews_count integer,
    collected_at  timestamptz not null default now(),
    primary key (snap_date, sku, size)
);
create index if not exists snapshots_sku_idx on lamoda_market.snapshots (sku, snap_date);

-- Позиции в выдаче категорий и поисковых запросов
create table if not exists lamoda_market.positions (
    snap_date date    not null,
    source    text    not null,   -- 'category' | 'search'
    query     text    not null,   -- id/путь категории или поисковый запрос
    sku       text    not null,
    position  integer not null,   -- 1 = первое место
    primary key (snap_date, source, query, sku)
);

-- Рассчитанные продажи (заполняет lamoda_parser.sales)
create table if not exists lamoda_market.daily_sales (
    sale_date    date    not null,
    sku          text    not null references lamoda_market.products (sku),
    units        integer not null,
    revenue      numeric(14, 2) not null,
    in_stock     boolean not null,
    restocked    boolean not null default false,   -- был приход — продажи за день занижены
    method       text    not null,                 -- 'stock_diff' | 'model'
    computed_at  timestamptz not null default now(),
    primary key (sale_date, sku)
);

-- Журнал запусков сбора — для ежедневной проверки качества и алертов
create table if not exists lamoda_market.runs (
    id          bigserial primary key,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    scope       text not null,          -- ниша / категория
    products    integer,
    errors      integer,
    blocked     integer,
    note        text
);
