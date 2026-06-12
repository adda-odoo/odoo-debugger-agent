-- Minimal Odoo-shaped schema for anon_tool tests.
-- Tables are ordered so FK dependencies are satisfied.

CREATE TABLE IF NOT EXISTS ir_model (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(64),
    model       VARCHAR(64) NOT NULL,
    transient   BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS ir_model_fields (
    id                SERIAL PRIMARY KEY,
    name              VARCHAR(64)  NOT NULL,
    model             VARCHAR(64),
    model_id          INTEGER      REFERENCES ir_model(id) ON DELETE CASCADE,
    ttype             VARCHAR(64),
    field_description VARCHAR(256),
    relation          VARCHAR(64),
    required          BOOLEAN      NOT NULL DEFAULT FALSE,
    store             BOOLEAN      NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS res_country (
    id   SERIAL PRIMARY KEY,
    name VARCHAR(64),
    code CHAR(2)
);

CREATE TABLE IF NOT EXISTS res_company (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(128) NOT NULL,
    email        VARCHAR(128),
    phone        VARCHAR(64),
    website      VARCHAR(128),
    vat          VARCHAR(32),
    street       VARCHAR(128),
    street2      VARCHAR(128),
    zip          VARCHAR(24),
    city         VARCHAR(128),
    company_type VARCHAR(16)  DEFAULT 'company',
    image_1920   BYTEA,
    country_id   INTEGER      REFERENCES res_country(id)
);

CREATE TABLE IF NOT EXISTS res_partner (
    id            SERIAL PRIMARY KEY,
    name          JSONB,
    display_name  JSONB,
    complete_name VARCHAR(256),
    email         VARCHAR(128),
    phone         VARCHAR(64),
    mobile        VARCHAR(64),
    street        VARCHAR(128),
    street2       VARCHAR(128),
    zip           VARCHAR(24),
    city          VARCHAR(128),
    vat           VARCHAR(32),
    website       VARCHAR(128),
    company_name  JSONB,
    image_1920    BYTEA,
    image_128     BYTEA,
    is_company    BOOLEAN      NOT NULL DEFAULT FALSE,
    active        BOOLEAN      NOT NULL DEFAULT TRUE,
    type          VARCHAR(16)  DEFAULT 'contact',
    company_id    INTEGER      REFERENCES res_company(id),
    country_id    INTEGER      REFERENCES res_country(id)
);

CREATE TABLE IF NOT EXISTS res_users (
    id         SERIAL PRIMARY KEY,
    login      VARCHAR(64)  UNIQUE NOT NULL,
    password   VARCHAR(256),
    active     BOOLEAN      NOT NULL DEFAULT TRUE,
    partner_id INTEGER      REFERENCES res_partner(id),
    signature  TEXT
);

CREATE TABLE IF NOT EXISTS hr_employee (
    id               SERIAL PRIMARY KEY,
    name             VARCHAR(128),
    work_email       VARCHAR(128),
    work_phone       VARCHAR(64),
    mobile_phone     VARCHAR(64),
    job_title        VARCHAR(128),
    birthday         DATE,
    private_email    VARCHAR(128),
    private_phone    VARCHAR(64),
    ssnid            VARCHAR(64),
    passport_id      VARCHAR(64),
    image_1920       BYTEA,
    address_home_id  INTEGER      REFERENCES res_partner(id),
    company_id       INTEGER      REFERENCES res_company(id)
);

CREATE TABLE IF NOT EXISTS mail_message (
    id           SERIAL PRIMARY KEY,
    body         TEXT,
    email_from   VARCHAR(128),
    reply_to     VARCHAR(128),
    subject      VARCHAR(256),
    author_id    INTEGER      REFERENCES res_partner(id),
    model        VARCHAR(128),
    res_id       INTEGER,
    message_type VARCHAR(32)  DEFAULT 'email',
    date         TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ir_attachment (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(256),
    store_fname VARCHAR(256),
    db_datas    BYTEA,
    mimetype    VARCHAR(64),
    res_model   VARCHAR(128),
    res_id      INTEGER,
    type        VARCHAR(16)  DEFAULT 'binary',
    url         TEXT
);

CREATE TABLE IF NOT EXISTS crm_lead (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(256),
    contact_name  VARCHAR(128),
    email_from    VARCHAR(128),
    phone         VARCHAR(64),
    partner_name  VARCHAR(128),
    description   TEXT,
    partner_id    INTEGER      REFERENCES res_partner(id),
    active        BOOLEAN      NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS ir_config_parameter (
    id    SERIAL PRIMARY KEY,
    key   VARCHAR(256) UNIQUE NOT NULL,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ir_module_module (
    id             SERIAL PRIMARY KEY,
    name           VARCHAR(128) UNIQUE NOT NULL,
    state          VARCHAR(16)  DEFAULT 'installed',
    latest_version VARCHAR(64)
);

-- ── Phase 4 infrastructure tables ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ir_mail_server (
    id                   SERIAL PRIMARY KEY,
    name                 VARCHAR(128),
    smtp_host            VARCHAR(128),
    smtp_port            INTEGER,
    smtp_user            VARCHAR(128),
    smtp_pass            VARCHAR(256),
    smtp_ssl_certificate BYTEA,
    smtp_ssl_private_key BYTEA,
    active               BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS fetchmail_server (
    id        SERIAL PRIMARY KEY,
    name      VARCHAR(128),
    server    VARCHAR(128),
    "user"    VARCHAR(128),
    password  VARCHAR(256),
    active    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS ir_cron (
    id        SERIAL PRIMARY KEY,
    name      VARCHAR(128),
    model_id  INTEGER,
    active    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS payment_provider (
    id    SERIAL PRIMARY KEY,
    name  VARCHAR(128),
    state VARCHAR(16) DEFAULT 'enabled'
);

CREATE TABLE IF NOT EXISTS mail_alias (
    id              SERIAL PRIMARY KEY,
    alias_name      VARCHAR(128),
    alias_model_id  INTEGER,
    alias_defaults  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS ir_logging (
    id      SERIAL PRIMARY KEY,
    name    VARCHAR(256),
    type    VARCHAR(16),
    level   VARCHAR(16),
    message TEXT,
    path    VARCHAR(256),
    func    VARCHAR(256),
    line    VARCHAR(16),
    create_date TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS discuss_channel (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(128),
    channel_type VARCHAR(32) DEFAULT 'channel',
    active       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS account_journal (
    id                        SERIAL PRIMARY KEY,
    name                      VARCHAR(128),
    code                      VARCHAR(16),
    restrict_mode_hash_table  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS account_move (
    id                       SERIAL PRIMARY KEY,
    name                     VARCHAR(64),
    secure_sequence_number   INTEGER,
    inalterable_hash         VARCHAR(64),
    state                    VARCHAR(16) DEFAULT 'draft'
);
