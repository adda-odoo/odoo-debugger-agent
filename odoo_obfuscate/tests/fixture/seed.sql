-- Seed data for anon_tool fixture DB.
-- Values are intentionally recognisable so post-run assertions can confirm replacement.

INSERT INTO res_country (id, name, code) VALUES
    (1, 'United States', 'US'),
    (2, 'France',        'FR');

INSERT INTO res_company (id, name, email, phone, website, vat, street, city, zip, country_id) VALUES
    (1, 'Acme Corporation', 'admin@acme.com', '+1 555-0100', 'https://acme.com', 'US123456789', '123 Main St', 'Springfield', '12345', 1);

INSERT INTO res_partner (id, name, display_name, email, phone, mobile, street, street2, zip, city, vat, website, company_name, is_company, active, company_id, country_id) VALUES
    (1, '{"en_US":"John Smith"}'::jsonb,    '{"en_US":"John Smith"}'::jsonb,    'john.smith@customer.com',     '+1 555-0101', '+1 555-0102', '10 Oak Ave',    NULL,      '10001', 'New York',    'US987654321', 'https://johnsmith.com',  '{"en_US":"Acme Corp"}'::jsonb,  FALSE, TRUE, 1, 1),
    (2, '{"en_US":"Jane Doe"}'::jsonb,      '{"en_US":"Jane Doe"}'::jsonb,      'jane.doe@example.org',        '+1 555-0201', '+1 555-0202', '20 Elm St',     'Apt 3',   '10002', 'Boston',      NULL,          NULL,                     NULL,                           FALSE, TRUE, 1, 1),
    (3, '{"en_US":"Bob Johnson"}'::jsonb,   '{"en_US":"Bob Johnson"}'::jsonb,   'bob.johnson@realcompany.com', '+1 555-0301', '+1 555-0302', '30 Pine Rd',    NULL,      '10003', 'Chicago',     NULL,          NULL,                     NULL,                           FALSE, TRUE, 1, 1),
    (4, '{"en_US":"Acme Corp"}'::jsonb,     '{"en_US":"Acme Corp"}'::jsonb,     'contact@acmecorp.biz',        '+1 555-0401', NULL,          '40 Maple Ave',  NULL,      '10004', 'Los Angeles', 'US111222333', 'https://acmecorp.biz',   NULL,                           TRUE,  TRUE, 1, 1),
    (5, '{"en_US":"Alice Martin"}'::jsonb,  '{"en_US":"Alice Martin"}'::jsonb,  'alice.martin@private.net',    '+1 555-0501', '+1 555-0502', '50 Cedar Ln',   'Suite 2', '10005', 'Seattle',     NULL,          NULL,                     NULL,                           FALSE, TRUE, 1, 1),
    (6, '{"en_US":"Carlos Rivera"}'::jsonb, '{"en_US":"Carlos Rivera"}'::jsonb, 'carlos.rivera@email.com',     '+1 555-0601', NULL,          '60 Birch Blvd', NULL,      '10006', 'Houston',     NULL,          NULL,                     NULL,                           FALSE, TRUE, 1, 1),
    (7, '{"en_US":"Diana Prince"}'::jsonb,  '{"en_US":"Diana Prince"}'::jsonb,  'diana.prince@hero.org',       '+1 555-0701', '+1 555-0702', '70 Willow Way', NULL,      '10007', 'Miami',       NULL,          NULL,                     NULL,                           FALSE, TRUE, 1, 1);

SELECT setval('res_partner_id_seq', 7);

INSERT INTO res_users (id, login, password, active, partner_id, signature) VALUES
    (1, 'admin',                         'pbkdf2+sha512$real_admin_hash',   TRUE,  1, '<p>Best regards,<br/>John Smith</p>'),
    (2, 'jane.doe@example.org',          'pbkdf2+sha512$real_jane_hash',    TRUE,  2, '<p>Regards,<br/>Jane Doe</p>'),
    (3, 'bob.johnson@realcompany.com',   'pbkdf2+sha512$real_bob_hash',     TRUE,  3, NULL),
    -- user with NULL partner_id to test fallback login generation
    (4, 'orphan.user@internal.example',  'pbkdf2+sha512$orphan_hash',       TRUE,  NULL, NULL);

SELECT setval('res_users_id_seq', 4);

INSERT INTO hr_employee (id, name, work_email, work_phone, mobile_phone, job_title, birthday, private_email, private_phone, ssnid, passport_id, company_id) VALUES
    (1, 'John Smith',   'john.smith@acme.com',    '+1 555-0110', '+1 555-0112', 'Sales Manager',      '1985-03-15', 'john.private@home.net',  '+1 555-0113', '123-45-6789', 'A12345678', 1),
    (2, 'Jane Doe',     'jane.doe@acme.com',      '+1 555-0210', '+1 555-0212', 'HR Specialist',      '1990-07-22', 'jane.private@home.net',  '+1 555-0213', '234-56-7890', 'B23456789', 1),
    (3, 'Bob Johnson',  'bob.johnson@acme.com',   '+1 555-0310', '+1 555-0312', 'Software Engineer',  '1988-11-30', 'bob.private@home.net',   '+1 555-0313', '345-67-8901', 'C34567890', 1),
    (4, 'Alice Martin', 'alice.martin@acme.com',  '+1 555-0410', NULL,          'Accountant',         '1992-04-08', 'alice.private@home.net', NULL,          '456-78-9012', NULL,        1),
    (5, 'Carlos Rivera','carlos.rivera@acme.com', '+1 555-0510', '+1 555-0512', 'Project Coordinator','1995-09-19', 'carlos.private@home.net','+1 555-0513', '567-89-0123', 'D45678901', 1);

SELECT setval('hr_employee_id_seq', 5);

INSERT INTO mail_message (id, body, email_from, reply_to, subject, author_id, model, res_id, message_type, date) VALUES
    (1, '<p>Hello John, please call me at +1 555-0199. My SSN is 999-00-1234.</p>', 'jane.doe@example.org',        'jane.doe@example.org',    'Follow-up',      2, 'res.partner', 1, 'comment', '2024-01-10 09:00:00'),
    (2, '<p>Meeting confirmed for 3pm. Bob Johnson will attend.</p>',               'admin@acme.com',              'admin@acme.com',           'Meeting',        1, 'res.partner', 3, 'email',   '2024-01-11 10:30:00'),
    (3, '<p>Invoice attached. Amount: $1,234.56. Account: 1234-5678-9012-3456</p>', 'billing@acme.com',            NULL,                      'Invoice',        4, 'account.move',1, 'email',   '2024-01-12 14:00:00'),
    (4, '<p>Your password is: secret123. Please change it immediately.</p>',        'it@acme.com',                 NULL,                      'Password Reset', 1, 'res.users',   2, 'email',   '2024-01-13 08:00:00'),
    (5, '<p>Address updated: 10 Oak Ave, New York, NY 10001.</p>',                  'john.smith@customer.com',     'john.smith@customer.com', 'Address Update', 1, 'res.partner', 1, 'comment', '2024-01-14 11:00:00');

SELECT setval('mail_message_id_seq', 5);

INSERT INTO ir_attachment (id, name, store_fname, db_datas, mimetype, res_model, res_id, type) VALUES
    (1, 'invoice_001.pdf',  'ab/abc123def456', NULL, 'application/pdf',   'account.move', 1, 'binary'),
    (2, 'photo_john.jpg',   'cd/cde234efg567', NULL, 'image/jpeg',        'res.partner',  1, 'binary'),
    (3, 'contract_acme.pdf','ef/efg345ghi678', NULL, 'application/pdf',   'hr.employee',  1, 'binary');

SELECT setval('ir_attachment_id_seq', 3);

INSERT INTO crm_lead (id, name, contact_name, email_from, phone, partner_name, description, partner_id, active) VALUES
    (1, 'Enterprise Deal Q1', 'John Smith',   'john.smith@customer.com',     '+1 555-0101', 'Acme Corp',     'Hot prospect from trade show.', 1, TRUE),
    (2, 'SMB Opportunity',    'Jane Doe',     'jane.doe@example.org',        '+1 555-0201', NULL,            NULL,                            2, TRUE),
    (3, 'Big Corp Contract',  'Bob Johnson',  'bob.johnson@realcompany.com', '+1 555-0301', 'Big Corp Inc',  'Referred by a partner.',        3, TRUE);

SELECT setval('crm_lead_id_seq', 3);

INSERT INTO ir_config_parameter (key, value) VALUES
    ('web.base.url',            'https://mycompany.odoo.com'),
    ('database.uuid',           'a1b2c3d4-e5f6-7890-abcd-ef1234567890'),
    ('auth_oauth.authorization_data', '{"token": "real_oauth_token_abc123"}'),
    ('smtp.server.password',    'smtp_real_password_xyz'),
    ('mail.catchall.domain',    'mycompany.odoo.com'),
    ('mail.default.from',       'notifications@mycompany.odoo.com'),
    ('web.base.url.freeze',     'False');

INSERT INTO ir_model (id, name, model, transient) VALUES
    (1,  'Contact',        'res.partner',       FALSE),
    (2,  'User',           'res.users',         FALSE),
    (3,  'Employee',       'hr.employee',       FALSE),
    (4,  'Company',        'res.company',       FALSE),
    (5,  'Message',        'mail.message',      FALSE),
    (6,  'Attachment',     'ir.attachment',     FALSE),
    (7,  'Config Param',   'ir.config_parameter', FALSE),
    (8,  'Model',          'ir.model',          FALSE),
    (9,  'Field',          'ir.model.fields',   FALSE);

SELECT setval('ir_model_id_seq', 9);

INSERT INTO ir_model_fields (name, model, model_id, ttype, field_description, required, store) VALUES
    -- res.partner fields
    ('name',        'res.partner', 1, 'char',    'Name',         TRUE,  TRUE),
    ('email',       'res.partner', 1, 'char',    'Email',        FALSE, TRUE),
    ('phone',       'res.partner', 1, 'char',    'Phone',        FALSE, TRUE),
    ('mobile',      'res.partner', 1, 'char',    'Mobile',       FALSE, TRUE),
    ('street',      'res.partner', 1, 'char',    'Street',       FALSE, TRUE),
    ('vat',         'res.partner', 1, 'char',    'Tax ID',       FALSE, TRUE),
    ('website',     'res.partner', 1, 'char',    'Website',      FALSE, TRUE),
    ('image_1920',  'res.partner', 1, 'binary',  'Image',        FALSE, TRUE),
    ('is_company',  'res.partner', 1, 'boolean', 'Company',      FALSE, TRUE),
    -- res.users fields
    ('login',       'res.users',   2, 'char',    'Login',        TRUE,  TRUE),
    ('password',    'res.users',   2, 'char',    'Password',     FALSE, TRUE),
    ('signature',   'res.users',   2, 'html',    'Signature',    FALSE, TRUE),
    -- hr.employee fields
    ('name',        'hr.employee', 3, 'char',    'Name',         TRUE,  TRUE),
    ('work_email',  'hr.employee', 3, 'char',    'Work Email',   FALSE, TRUE),
    ('birthday',    'hr.employee', 3, 'date',    'Date of Birth',FALSE, TRUE),
    ('ssnid',       'hr.employee', 3, 'char',    'SSN',          FALSE, TRUE),
    ('passport_id', 'hr.employee', 3, 'char',    'Passport No',  FALSE, TRUE),
    -- mail.message fields
    ('body',        'mail.message',5, 'html',    'Body',         FALSE, TRUE),
    ('email_from',  'mail.message',5, 'char',    'From',         FALSE, TRUE),
    ('subject',     'mail.message',5, 'char',    'Subject',      FALSE, TRUE),
    -- ir.model.fields fields (system table — should land Bucket 3)
    ('name',        'ir.model.fields', 9, 'char','Field Name',   TRUE,  TRUE);

-- Odoo module version (used by odoo_version() in db.py)
INSERT INTO ir_module_module (name, state, latest_version) VALUES
    ('base',        'installed', '17.0.1.3'),
    ('mail',        'installed', '17.0.1.3'),
    ('hr',          'installed', '17.0.1.0'),
    ('account',     'installed', '17.0.1.2');

-- ── Phase 4 infrastructure seed data ─────────────────────────────────────

INSERT INTO ir_mail_server (id, name, smtp_host, smtp_user, smtp_pass, smtp_ssl_certificate, smtp_ssl_private_key, active) VALUES
    (1, 'Outgoing Mail',  'smtp.real.example.com', 'real_smtp_user@acme.com', 'real_smtp_password',      '\x4d494943'::bytea, '\x4d494943'::bytea, TRUE),
    (2, 'Backup Mail',    'smtp2.acme.com',        'backup@acme.com',         'another_real_password',   NULL,                NULL,                TRUE);

INSERT INTO fetchmail_server (id, name, server, "user", password, active) VALUES
    (1, 'Incoming Mail',  'imap.real.example.com', 'real_imap@acme.com', 'real_imap_password', TRUE);

INSERT INTO ir_cron (id, name, active) VALUES
    (1, 'Send Queued Mail', TRUE),
    (2, 'Update Currency Rates', TRUE),
    (3, 'Already Disabled', FALSE);

INSERT INTO payment_provider (id, name, state) VALUES
    (1, 'Stripe Live', 'enabled'),
    (2, 'PayPal Live', 'enabled');

INSERT INTO mail_alias (id, alias_name, alias_defaults) VALUES
    (1, 'catchall',           '{}'),
    (2, 'bounce',              '{}'),
    (3, 'sales@acme.com',      '{}'),
    (4, 'support@acme.com',    '{}'),
    (5, 'info-leadgen@acme.com','{}');

INSERT INTO ir_logging (id, name, type, level, message, path, func, line) VALUES
    (1, 'odoo.http',   'server', 'INFO',    'User john.smith@customer.com logged in', '/odoo/http.py', 'login',  '123'),
    (2, 'odoo.models', 'server', 'WARNING', 'Failed to send mail to jane.doe@example.org', '/odoo/mail.py', 'send', '456'),
    (3, 'odoo.cron',   'server', 'INFO',    'Cron ran successfully',                       '/odoo/cron.py', 'run',  '789');

INSERT INTO discuss_channel (id, name, channel_type) VALUES
    (1, 'general',            'channel'),
    (2, 'announce',           'channel'),
    (3, 'John Smith, Jane Doe', 'chat'),
    (4, 'Customer: Bob Johnson live chat session', 'livechat'),
    (5, 'sales-team',         'channel');

INSERT INTO account_journal (id, name, code, restrict_mode_hash_table) VALUES
    (1, 'Customer Invoices', 'INV', TRUE),
    (2, 'Vendor Bills',      'BILL', FALSE);

INSERT INTO account_move (id, name, secure_sequence_number, inalterable_hash, state) VALUES
    (1, 'INV/2024/0001', 100, 'a1b2c3d4e5f6a7b8c9d0e1f2', 'posted'),
    (2, 'INV/2024/0002', 101, 'b2c3d4e5f6a7b8c9d0e1f2a3', 'posted');

-- Extra ir_config_parameter entries (non-credential — must NOT be scrubbed)
INSERT INTO ir_config_parameter (key, value) VALUES
    ('database.expiration_date',     '2099-01-01'),
    ('report.url',                   'http://localhost:8069');
