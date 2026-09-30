# Created by Odoo — Technical Support tooling (scrubdb).
"""Engine tests — run with zero third-party dependencies.

    python3 -m pytest tests/
    # or the zero-dep fallback runner in the README

Each test is a plain no-argument function so it works under both pytest and the
fallback runner. No live database: DB access is exercised through a mock cursor.
"""
import decimal as _decimal
import json
import os
import sys

# Put the directory that *contains* the scrubdb package (tools/) on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scrubdb import classify, cli, introspect, policy, transforms  # noqa: E402
from scrubdb.executor import apply_plan  # noqa: E402
from scrubdb.transforms import Context  # noqa: E402


class _Args:
    """Minimal stand-in for an argparse namespace (DB-free)."""
    def __init__(self, **kw):
        self.database = None
        self.db_user = self.db_password = None
        self.db_host = self.db_port = self.db_sslmode = None
        self.db_app_name = None
        self.__dict__.update(kw)


# --- mocks -----------------------------------------------------------------

class MockCursor:
    def __init__(self, columns=None, distinct=None, sample=None):
        self._columns = columns or []
        self._distinct = distinct or []
        self._sample = [] if sample is None else sample
        self._result = []
        self.updates = []
        self.selects = []
        self.deletes = []
        self.inserts = []
        self.alters = []
        self.ordered_calls = []   # [(kind, sql), ...] in execution order
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.ordered_calls.append((s.split(" ", 1)[0] if s else "", s))
        if s.startswith("ALTER"):
            self.alters.append(s)
            self._result = []
        elif "information_schema.columns" in s:
            self._result = list(self._columns)
        elif s.startswith("SELECT DISTINCT"):
            self.selects.append(s)
            self._result = [(v,) for v in self._distinct]
        elif s.startswith("INSERT"):
            self.inserts.append((s, params))
        elif "information_schema.tables" in s:
            # fetch_columns reads tables and columns as two single-view queries
            # and intersects them in Python, so the mock has to answer both.
            # Every table named in `columns` is a BASE TABLE here; view
            # filtering is covered by test_fetch_columns_restricts_to_base_tables.
            self._result = [(t,) for t in dict.fromkeys(t for t, *_ in self._columns)]
            self._result = []
        elif s.startswith("UPDATE"):
            self.updates.append((s, params))
            # A staged UPDATE (joined against the temp mapping table) has no
            # inline VALUES params of its own — approximate its rowcount from
            # how many rows were staged instead of len(params)//2.
            if "_scrubdb_map" in s and not params:
                self.rowcount = sum(len(p) // 2 for _, p in self.inserts)
            else:
                self.rowcount = (len(params) // 2) if params else 0
            self._result = []
        elif s.startswith("DELETE"):
            self.deletes.append(s)
            self.rowcount = 7      # arbitrary non-zero for reporting assertions
            self._result = []
        elif s.startswith("SELECT"):
            self._result = [(v,) for v in self._sample]
        else:
            self._result = []

    def fetchall(self):
        return self._result


class MockConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


# --- transforms ------------------------------------------------------------

def test_context_deterministic():
    a = Context(b"key-one")
    b = Context(b"key-one")
    assert a.int_for("x", 1) == b.int_for("x", 1)
    assert a.choice(["p", "q", "r"], "x") == b.choice(["p", "q", "r"], "x")


def test_context_different_keys_differ():
    a = Context(b"key-one")
    b = Context(b"key-two")
    assert a.int_for("x", 1) != b.int_for("x", 1)


def test_all_transforms_null_safe():
    ctx = Context(b"k")
    for name, fn in transforms.REGISTRY.items():
        assert fn(None, ctx) is None, f"{name} not null-safe"


def test_fake_email_domain_and_stable():
    ctx = Context(b"k")
    out = transforms.fake_email("john.doe@corp.io", ctx)
    assert out.endswith("@example.com")
    assert out == transforms.fake_email("john.doe@corp.io", ctx)


def test_fake_email_irreversible_across_runs():
    v = "john.doe@corp.io"
    assert transforms.fake_email(v, Context(b"run-1")) != \
        transforms.fake_email(v, Context(b"run-2"))


def test_scramble_digits_preserves_format():
    ctx = Context(b"k")
    out = transforms.scramble_digits("FR76-1234-5678", ctx)
    assert len(out) == len("FR76-1234-5678")
    assert out[:2] == "FR" and out[4] == "-"
    assert out != "FR76-1234-5678"


def test_scramble_digits_varied_and_consistent():
    ctx = Context(b"k")
    # digits within one value are not collapsed to a single repeated digit
    out = transforms.scramble_digits("11111111", ctx)
    assert len(set(out)) > 1
    # same input -> same output (join/dedup consistency); different -> different
    assert transforms.scramble_digits("42424242", ctx) == \
        transforms.scramble_digits("42424242", ctx)
    assert transforms.scramble_digits("42424242", ctx) != \
        transforms.scramble_digits("42424243", ctx)


def test_partial_mask():
    ctx = Context(b"k")
    fn = transforms.partial_mask(keep_last=4)
    assert fn("4111111111111111", ctx) == "************1111"


def test_generalize_number_buckets_down():
    ctx = Context(b"k")
    fn = transforms.generalize_number(bucket=10)
    assert fn(47, ctx) == 40
    assert fn(40, ctx) == 40


def test_noise_number_within_bounds_and_stable():
    ctx = Context(b"k")
    fn = transforms.noise_number(pct=10)
    out = fn(1000, ctx)
    assert 900 <= out <= 1100
    assert out == fn(1000, ctx)


def test_resolve_string_and_factory():
    # resolve() wraps every transform except "keep" (see test_resolve_ensures_changed_*
    # below), so it no longer returns the registry function by identity.
    fn = transforms.resolve("fake_email")
    assert callable(fn) and fn(None, Context(b"k")) is None
    fn = transforms.resolve({"partial_mask": {"keep_last": 2}})
    assert callable(fn)
    assert fn("abcdef", Context(b"k")).endswith("ef")


def test_resolve_keep_is_a_true_no_op():
    fn = transforms.resolve("keep")
    assert fn is transforms.keep
    assert fn(42, Context(b"k")) == 42
    assert fn("anything", Context(b"k")) == "anything"


def test_resolve_ensures_changed_on_collision():
    # empty("") returns "" unchanged for a non-null, already-empty input — a
    # genuine no-op that resolve() must not let through silently.
    fn = transforms.resolve("empty")
    assert fn("", Context(b"k")) != ""
    # None stays None regardless (null input/output is never "nudged").
    assert fn(None, Context(b"k")) is None


def test_resolve_ensures_changed_across_string_native_type_boundary():
    # executor._apply_column casts every non-text column's original value to
    # ::text before calling the transform (psycopg2 can't bind dict/date/etc.
    # back as params otherwise), so `value` arrives here as a STRING even for
    # a numeric column, while noise_number returns a native int/float. Naive
    # `result != value` is always True for e.g. `1 != "1"` regardless of the
    # actual numbers, which would make the collision check never fire for
    # exactly the case it exists to catch. Reproduces that boundary directly,
    # without a real executor/DB round-trip.
    fn = transforms.resolve({"noise_number": {}})
    for key in range(50):
        ctx = Context(key.to_bytes(4, "big"))
        for n in range(0, 5):
            # str(): the output is a native int, the input is a string — a
            # bare `!=` here would always be True regardless of the actual
            # numbers (int 1 != str "1"), which is exactly the trap this test
            # guards against reintroducing.
            result = fn(str(n), ctx)
            assert str(result) != str(n), (
                f"noise_number('{n}') collided with its own string input "
                f"(both render as {n!r}), key {key}"
            )


def test_resolve_ensures_changed_numeric_scale_mismatch():
    # An unconstrained `numeric` column preserves each row's own decimal
    # scale, so Postgres's ::text cast can render the same value as "0" on
    # one row and "0.0" on another (confirmed on hr_employee.hourly_cost in
    # production). A plain string comparison would see noise_number("0.0")'s
    # raw (pre-nudge) result of int 0 as already "different" from "0.0" and
    # skip nudging — even though they're the same number.
    fn = transforms.resolve({"noise_number": {}})
    for key in range(50):
        ctx = Context(key.to_bytes(4, "big"))
        for original in ("0.0", "0.00", "0"):
            result = fn(original, ctx)
            assert _decimal.Decimal(str(result)) != _decimal.Decimal(original), (
                f"noise_number({original!r}) collided with its own value, key {key}"
            )


def test_resolve_ensures_changed_noise_number_small_integers():
    # The bug this closes: noise_number's default 10% jitter rounds back to
    # the exact original for any integer small enough that 10% of it is <0.5
    # (transforms.py's own docstring/tests only ever exercised pct on 1000).
    # Try many keys so this isn't just lucky with one HMAC output.
    fn = transforms.resolve({"noise_number": {}})
    for key in range(50):
        ctx = Context(key.to_bytes(4, "big"))
        for n in range(0, 5):
            assert fn(n, ctx) != n, f"noise_number({n}) was a no-op with key {key}"


def test_nudge_string_no_letters_preserves_length():
    # A digit/punctuation-only string (e.g. a fixed-width customs/reference
    # number) has no letter to case-flip; nudging it must mutate a character
    # in place rather than appending, or a value already at a varchar(n)
    # column's max length overflows it (seen live on a 21-char customs
    # number field where mask_text round-tripped to its own input).
    ctx = Context(b"k")
    for value in ("26  07  3435  6010065", "12345", "---", "0"):
        nudged = transforms._nudge(value, ctx)
        assert nudged != value
        assert len(nudged) == len(value)


def test_nudge_empty_string_and_bytes():
    # Nothing to mutate in place, so growing by one is the only option —
    # harmless since an empty value can't already be at a non-zero max length.
    ctx = Context(b"k")
    assert transforms._nudge("", ctx) == "_"
    assert transforms._nudge(b"", ctx) == b"\x00"


def test_nudge_bytes_preserves_length():
    ctx = Context(b"k")
    nudged = transforms._nudge(b"\xff\xff", ctx)
    assert nudged != b"\xff\xff"
    assert len(nudged) == 2


# --- introspect ------------------------------------------------------------

def test_introspect_fetch_columns():
    cur = MockCursor(columns=[
        ("res_partner", "email", "character varying", "YES"),
        ("res_partner", "id", "integer", "NO"),
    ])
    cols = introspect.fetch_columns(cur)
    assert len(cols) == 2
    email = cols[0]
    assert email.table == "res_partner" and email.name == "email"
    assert email.is_textual is True
    assert cols[1].is_textual is False


def test_fetch_columns_restricts_to_base_tables():
    # Must not return VIEW columns (e.g. hr_employee_public) — those aren't
    # updatable and blanket masking would fail on them. The restriction is a
    # Python intersection, not SQL, so assert on the result, not the query text.
    class Cur:
        def __init__(self):
            self.sql = []
        def execute(self, sql, params=None):
            self.sql.append(" ".join(sql.split()))
        def fetchall(self):
            if "information_schema.tables" in self.sql[-1]:
                return [("res_partner",)]          # hr_employee_public is a VIEW
            return [("res_partner", "email", "character varying", "YES"),
                    ("hr_employee_public", "name", "character varying", "YES")]
    cur = Cur()
    cols = introspect.fetch_columns(cur)
    assert [(c.table, c.name) for c in cols] == [("res_partner", "email")]
    assert any("'BASE TABLE'" in s for s in cur.sql)


# --- policy ----------------------------------------------------------------

def test_policy_explicit_rule():
    cols = [introspect.Column("res_partner", "email", "character varying", True)]
    plan = policy.build_blanket_plan(
        cols, overrides={"rules": {"res_partner": {"email": "fake_email"}}})
    assert len(plan.actions) == 1
    assert plan.actions[0].transform_name == "fake_email"


# --- executor --------------------------------------------------------------

def test_apply_plan_dry_run_rolls_back():
    cols = [introspect.Column("t", "c", "text", True)]
    plan = policy.build_blanket_plan(cols, overrides={"rules": {"t": {"c": "fake_email"}}})
    cur = MockCursor(distinct=["a@x.com", "b@y.com"])
    conn = MockConn(cur)
    report = apply_plan(conn, plan, dry_run=True)
    assert conn.rolled_back is True and conn.committed is False
    assert report.dry_run is True and report.columns_changed == 1


def test_apply_plan_transforms_distinct_only():
    cols = [introspect.Column("t", "c", "text", True)]
    plan = policy.build_blanket_plan(cols, overrides={"rules": {"t": {"c": "fake_email"}}})
    cur = MockCursor(distinct=["a@x.com", "b@y.com", "c@z.com"])
    conn = MockConn(cur)
    apply_plan(conn, plan, dry_run=False)
    assert conn.committed is True
    # one UPDATE, three (old,new) pairs = 6 params
    assert len(cur.updates) == 1
    assert len(cur.updates[0][1]) == 6


def test_apply_column_high_cardinality_uses_one_atomic_update():
    # More distinct values than batch_size used to be applied as several
    # sequential UPDATE ... FROM (VALUES ...) statements, each seeing the
    # previous one's committed effect — unsafe, because a row rewritten by
    # an earlier statement to some value B could get caught again by a later
    # statement's "WHERE col = B" if B was also one of the original values
    # still pending. A single statement's SET/WHERE evaluates against one
    # consistent pre-statement snapshot, so it can't chain like that — this
    # test checks the executor actually issues only one such statement
    # against the real table (staging the mapping via chunked INSERTs into a
    # temp table first), not that Postgres behaves atomically (a real DB
    # fact this mock can't exercise either way).
    cols = [introspect.Column("t", "c", "text", True)]
    plan = policy.build_blanket_plan(cols, overrides={"rules": {"t": {"c": "fake_email"}}})
    distinct = [f"user{i}@x.com" for i in range(10)]
    cur = MockCursor(distinct=distinct)
    conn = MockConn(cur)
    apply_plan(conn, plan, dry_run=False, batch_size=3)

    assert len(cur.inserts) == 4  # ceil(10/3) staging chunks
    assert sum(len(p) // 2 for _, p in cur.inserts) == 10
    real_table_updates = [s for s, p in cur.updates if "_scrubdb_map" in s]
    assert len(cur.updates) == 1        # exactly one UPDATE touches the real table
    assert len(real_table_updates) == 1
    assert real_table_updates[0].startswith('UPDATE "public"."t"')


def test_apply_plan_jsonb_roundtrips_via_text():
    # Regression: psycopg2 returns json/jsonb as dict; it must never be bound
    # back as a parameter, and the masked value must stay valid JSON.
    cols = [introspect.Column("res_partner", "name", "jsonb", True)]
    plan = policy.build_blanket_plan(
        cols, overrides={"rules": {"res_partner": {"name": "fake_name"}}})
    assert plan.actions[0].data_type == "jsonb"
    cur = MockCursor(distinct=['{"en_US": "John Smith"}', '{"en_US": "Jane Doe"}'])
    conn = MockConn(cur)
    apply_plan(conn, plan, dry_run=False)
    assert conn.committed is True
    assert len(cur.updates) == 1
    sql, params = cur.updates[0]
    # never bind a dict/list — only text (or None)
    assert all(isinstance(p, (str, type(None))) for p in params)
    # select/match on ::text and cast the new value back to ::jsonb
    assert "::jsonb" in sql and "::text" in sql
    # new values are valid JSON with the leaf masked
    for new in params[1::2]:
        obj = json.loads(new)
        assert obj["en_US"] not in ("John Smith", "Jane Doe")


def test_apply_plan_jsonb_select_uses_text_cast():
    cols = [introspect.Column("res_partner", "name", "jsonb", True)]
    plan = policy.build_blanket_plan(
        cols, overrides={"rules": {"res_partner": {"name": "fake_name"}}})
    cur = MockCursor(distinct=['{"en_US": "Alice"}'])
    apply_plan(MockConn(cur), plan, dry_run=True)
    # the DISTINCT fetch is done on the ::text form, so originals are strings
    assert any("SELECT DISTINCT" in s and "::text" in s for s in cur.selects)


# --- CLI connection arguments (Odoo-compatible, DB-free) -------------------

def test_conn_args_dbname_from_positional():
    # The exact target database always comes from the required positional.
    kw = cli.connection_arguments(_Args(database="copy_db",
                                        db_app_name="scrubdb-{pid}"))
    assert kw["dbname"] == "copy_db"


def test_conn_args_maps_odoo_names_to_libpq():
    kw = cli.connection_arguments(_Args(
        database="copy_db", db_user="ro", db_password="secret",
        db_host="localhost", db_port=5433, db_sslmode="require",
        db_app_name="scrubdb-{pid}"))
    assert kw["dbname"] == "copy_db"
    assert kw["user"] == "ro"
    assert kw["password"] == "secret"
    assert kw["host"] == "localhost"
    assert kw["port"] == 5433
    assert kw["sslmode"] == "require"
    # {pid} is substituted with a real integer pid
    assert kw["application_name"].startswith("scrubdb-")
    assert kw["application_name"].split("-")[-1] == str(os.getpid())


def test_conn_args_drops_empty_for_libpq_fallback():
    # Only the database is given; everything else must be absent so libpq's
    # own PG* env fallback applies (no empty strings forced into the kwargs).
    kw = cli.connection_arguments(_Args(database="copy_db",
                                        db_app_name="scrubdb-{pid}"))
    assert set(kw) == {"dbname", "application_name"}
    assert kw["dbname"] == "copy_db"


def test_conn_args_app_name_trimmed_to_namedatalen():
    kw = cli.connection_arguments(_Args(database="d", db_app_name="x" * 200))
    assert len(kw["application_name"]) == 63


# --- blanket mode: transforms ----------------------------------------------

def test_mask_text_preserves_shape():
    ctx = Context(b"k")
    out = transforms.mask_text("John-42", ctx)
    assert len(out) == len("John-42")
    assert out[4] == "-"                       # punctuation preserved
    assert out[0].isupper() and out[1:4].islower()  # letter case preserved
    assert out[5:].isdigit()                   # digits stay digits


def test_mask_text_deterministic_and_null_safe():
    a, b = Context(b"k"), Context(b"k")
    assert transforms.mask_text("Hello", a) == transforms.mask_text("Hello", b)
    assert transforms.mask_text(None, a) is None


def test_date_shift_global_offset_preserves_order_and_format():
    ctx = Context(b"k")
    d1 = transforms.date_shift("2020-01-01", ctx)
    d2 = transforms.date_shift("2020-06-01", ctx)
    assert len(d1) == 10 and len(d2) == 10
    assert d1 < d2                             # single global offset keeps ordering
    ts = transforms.date_shift("2020-01-01 08:30:00", ctx)
    assert len(ts) == 19 and ts[10] == " "


def test_date_shift_unparseable_left_unchanged():
    assert transforms.date_shift("not a date", Context(b"k")) == "not a date"


# --- blanket mode: introspection -------------------------------------------

class _RowCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows


def test_fetch_foreign_key_columns_both_sides():
    fk = introspect.fetch_foreign_key_columns(
        _FkCursor([("sale_order", "partner_id", "res_partner", "id", "SET NULL")]))
    assert ("sale_order", "partner_id") in fk   # referencing side
    assert ("res_partner", "id") in fk          # referenced side


def test_fetch_unique_columns_single_vs_composite():
    rows = [
        ("uq_login", "res_users", "login"),          # single-column unique
        ("pk_partner", "res_partner", "id"),         # single-column PK
        ("uq_follow", "mail_followers", "res_model"),  # composite unique …
        ("uq_follow", "mail_followers", "res_id"),
        ("uq_follow", "mail_followers", "partner_id"),
    ]
    single, composite = introspect.fetch_unique_columns(_RowCursor(rows))
    assert ("res_users", "login") in single and ("res_partner", "id") in single
    assert ("mail_followers", "res_id") in composite
    assert ("mail_followers", "res_id") not in single


# --- blanket mode: plan builder --------------------------------------------

def _cols(*specs):
    return [introspect.Column(t, c, d, True) for (t, c, d) in specs]


def test_blanket_masks_text_keeps_structural():
    cols = _cols(
        ("res_partner", "id", "integer"),
        ("res_partner", "name", "character varying"),
        ("res_partner", "company_id", "integer"),
        ("res_partner", "create_date", "timestamp without time zone"),
    )
    plan = policy.build_blanket_plan(
        cols,
        pk_columns={("res_partner", "id")},
        fk_columns={("res_partner", "company_id")},
    )
    acted = {(a.table, a.column): a.transform_name for a in plan.actions}
    assert acted[("res_partner", "name")] == "mask_name"  # person name
    assert ("res_partner", "id") not in acted           # pk kept
    assert ("res_partner", "company_id") not in acted    # fk kept
    assert ("res_partner", "create_date") not in acted   # system kept


def test_blanket_type_routing():
    cols = _cols(
        ("t", "d", "date"),
        ("t", "amount", "numeric"),
        ("t", "flag", "boolean"),
        ("t", "uid", "uuid"),
class _FkCursor:
    """Answers the four single-view reads ``_fetch_fk_rows`` now issues.

    Takes the FK rows in the shape the old single joined query returned —
    ``(table, column, ref_table, ref_column, delete_rule)`` — and splits them
    back out per view, so the tests keep describing foreign keys rather than
    information_schema's internals.
    """

    def __init__(self, fks):
        self._fks = [(f"{t}_{c}_fkey", t, c, rt, rc, rule) for t, c, rt, rc, rule in fks]
        self._rows = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "table_constraints" in s:
            self._rows = [(n, t) for n, t, _, _, _, _ in self._fks]
        elif "key_column_usage" in s:
            self._rows = [(n, c) for n, _, c, _, _, _ in self._fks]
        elif "constraint_column_usage" in s:
            self._rows = [(n, rt, rc) for n, _, _, rt, rc, _ in self._fks]
        elif "referential_constraints" in s:
            self._rows = [(n, rule) for n, _, _, _, _, rule in self._fks]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


class _SchemaCursor(_FkCursor):
    """``_FkCursor`` plus the tables/columns reads, for introspection that needs both.

    ``columns`` rows come in as ``(table, column, is_nullable)``; the data_type
    ``_fetch_base_table_columns`` also selects is irrelevant to these tests, so
    it is filled in as text.
    """

    def __init__(self, columns, fks):
        super().__init__(fks)
        self._columns = [(t, c, "text", n) for t, c, n in columns]

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "information_schema.tables" in s:
            self._rows = [(t,) for t in dict.fromkeys(t for t, *_ in self._columns)]
        elif "information_schema.columns" in s:
            self._rows = list(self._columns)
        else:
            super().execute(sql, params)


        ("t", "payload", "jsonb"),
    )
    plan = policy.build_blanket_plan(cols)
    acted = {a.column: a.transform_name for a in plan.actions}
    assert acted["d"] == "date_shift"
    assert acted["amount"] == "noise_number"
    assert acted["payload"] == "mask_text"
    assert "flag" not in acted and "uid" not in acted    # kept: no safe transform


def test_blanket_default_excludes_selection_field():
    cols = _cols(("sale_order", "state", "character varying"),
                 ("sale_order", "note", "text"))
    plan = policy.build_blanket_plan(cols)
    acted = {a.column for a in plan.actions}
    assert "note" in acted and "state" not in acted


def test_blanket_unique_flag_and_override():
    cols = _cols(("shop", "slug", "character varying"),
                 ("res_partner", "email", "character varying"))
    plan = policy.build_blanket_plan(
        cols,
        unique_columns={("shop", "slug")},
        overrides={"rules": {"res_partner": {"email": "fake_email"}}},
    )
    by = {(a.table, a.column): a for a in plan.actions}
    assert by[("shop", "slug")].unique is True
    assert by[("res_partner", "email")].transform_name == "fake_email"   # override wins


def test_blanket_custom_exclude_pattern_no_defaults():
    cols = _cols(("t", "note", "text"), ("t", "ref", "character varying"))
    plan = policy.build_blanket_plan(cols, exclude_patterns=["ref"],
                                     use_default_excludes=False)
    acted = {a.column for a in plan.actions}
    assert "note" in acted and "ref" not in acted


# --- blanket mode: executor round-trip & uniqueness ------------------------

def test_apply_plan_unique_appends_distinct_suffix():
    import re
    cols = [introspect.Column("shop", "slug", "character varying", False)]
    plan = policy.build_blanket_plan(cols, unique_columns={("shop", "slug")})
    assert plan.actions[0].unique is True
    cur = MockCursor(distinct=["alice", "bob"])
    apply_plan(MockConn(cur), plan, dry_run=False)
    _, params = cur.updates[0]
    news = params[1::2]
    assert all(re.search(r"_[0-9a-f]{8}$", n) for n in news)  # deterministic suffix
    assert len(set(news)) == len(news)                        # stays distinct


def test_apply_plan_unique_text_column_widened_before_suffixing():
    # _unique_suffix appends 9 characters; a fixed-width varchar(n) column
    # already at its max length (e.g. Odoo's 21-char customs/pedimento
    # number) would overflow unless the cap is dropped first.
    cols = [introspect.Column("stock_landed_cost", "l10n_mx_edi_customs_number",
                              "character varying", True)]
    plan = policy.build_blanket_plan(cols, unique_columns={
        ("stock_landed_cost", "l10n_mx_edi_customs_number")})
    cur = MockCursor(distinct=["26  07  3435  6010065", "26  07  3435  6010178"])
    apply_plan(MockConn(cur), plan, dry_run=False)
    assert any("l10n_mx_edi_customs_number" in a and "TYPE text" in a for a in cur.alters)
    kinds = [kind for kind, _ in cur.ordered_calls]
    assert kinds.index("ALTER") < kinds.index("UPDATE")   # widened before the write that needs it


def test_apply_plan_retries_once_on_unique_violation_then_succeeds():
    # A hidden constraint introspection can't see (a partial/expression unique
    # index, e.g. Odoo's account_move_unique_name) never sets action.unique,
    # so masked values can collide with an adjacent, not-yet-masked original
    # in a dense sequence. The executor should retry once with the same
    # distinctness suffix used for known-unique columns instead of aborting
    # the whole run.
    import psycopg2

    class _RaiseOnceCursor(MockCursor):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.update_attempts = 0

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("UPDATE"):
                self.update_attempts += 1
                if self.update_attempts == 1:
                    raise psycopg2.errors.UniqueViolation("duplicate key value")
            super().execute(sql, params)

    cols = [introspect.Column("account_move", "name", "character varying", False)]
    plan = policy.build_blanket_plan(cols)   # no unique_columns passed in
    assert plan.actions[0].unique is False   # exactly the blind spot being simulated
    cur = _RaiseOnceCursor(distinct=["00154", "00155"])
    apply_plan(MockConn(cur), plan, dry_run=False)
    assert cur.update_attempts == 2          # first attempt failed, retry succeeded
    _, params = cur.updates[-1]
    news = params[1::2]
    assert len(set(news)) == len(news)       # retry's suffix kept them distinct


def test_apply_plan_reraises_unique_violation_when_already_suffixed():
    # If the column was already known-unique (already suffixed) and still
    # collides, that's a genuine bug elsewhere — retrying blindly would just
    # loop, so the executor must re-raise instead.
    import psycopg2

    class _AlwaysRaiseCursor(MockCursor):
        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("UPDATE"):
                raise psycopg2.errors.UniqueViolation("duplicate key value")
            super().execute(sql, params)

    cols = [introspect.Column("shop", "slug", "character varying", False)]
    plan = policy.build_blanket_plan(cols, unique_columns={("shop", "slug")})
    assert plan.actions[0].unique is True
    cur = _AlwaysRaiseCursor(distinct=["alice", "bob"])
    try:
        apply_plan(MockConn(cur), plan, dry_run=False)
        assert False, "expected UniqueViolation to propagate"
    except psycopg2.errors.UniqueViolation:
        pass


def test_apply_plan_date_column_text_roundtrip_and_cast():
    cols = [introspect.Column("t", "d", "date", True)]
    plan = policy.build_blanket_plan(cols)
    assert plan.actions[0].transform_name == "date_shift"
    cur = MockCursor(distinct=["2020-01-01", "2020-06-01"])
    apply_plan(MockConn(cur), plan, dry_run=False)
    assert any("::text" in s for s in cur.selects)     # fetched as text
    sql, params = cur.updates[0]
    assert "::date" in sql                             # cast back to the column type
    assert all(isinstance(p, str) for p in params)     # only text bound


def test_apply_plan_numeric_column_casts_back():
    cols = [introspect.Column("t", "n", "numeric", True)]
    plan = policy.build_blanket_plan(cols)
    assert plan.actions[0].transform_name == "noise_number"
    cur = MockCursor(distinct=["100", "200"])
    apply_plan(MockConn(cur), plan, dry_run=False)
    sql, params = cur.updates[0]
    assert "::numeric" in sql
    assert all(isinstance(n, (int, float)) for n in params[1::2])  # numbers, not dicts


def test_cli_load_excludes_merges_flags_and_file(tmp_path=None):
    import tempfile, os as _os
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with _os.fdopen(fd, "w") as fh:
            fh.write("# comment\nres_users.password\n\n*_token\n")
        out = cli._load_excludes(["res_partner.ref"], path)
    finally:
        _os.unlink(path)
    assert "res_partner.ref" in out
    assert "res_users.password" in out and "*_token" in out
    assert "# comment" not in out and "" not in out


# --- blanket mode: enum / CHECK hardening ----------------------------------

def test_looks_like_enum_true_and_false():
    enumish = ["draft", "done", "draft", "sent"] * 10   # 40 rows, 3 distinct short
    assert classify.looks_like_enum(enumish) is True
    freetext = [f"note {i}" for i in range(40)]          # all distinct
    assert classify.looks_like_enum(freetext) is False
    long_repeated = ["x" * 50, "y" * 50] * 15            # short-count but long values
    assert classify.looks_like_enum(long_repeated) is False
    tiny = ["a", "b"]                                    # too few rows to trust
    assert classify.looks_like_enum(tiny) is False


def test_fetch_check_constraint_columns():
    cc = introspect.fetch_check_constraint_columns(
        _RowCursor([("account_move", "move_type")]))
    assert ("account_move", "move_type") in cc


def test_blanket_check_constraint_excluded():
    cols = _cols(("t", "grade", "character varying"), ("t", "note", "text"))
    plan = policy.build_blanket_plan(cols, check_columns={("t", "grade")})
    acted = {a.column for a in plan.actions}
    assert "note" in acted and "grade" not in acted
    assert any("t.grade [check]" in e for e in plan.excluded)


def test_blanket_enum_guard_excludes_low_cardinality():
    cols = _cols(("t", "flavor", "character varying"))
    rows = [(v,) for v in (["a", "b", "c"] * 10)]        # 30 rows, 3 distinct
    plan = policy.build_blanket_plan(cols, cur=_RowCursor(rows))
    assert not plan.actions
    assert any("t.flavor [enum]" in e for e in plan.excluded)


class _CountingRowCursor(_RowCursor):
    """Like _RowCursor, but records whether it was ever queried."""
    def __init__(self, rows):
        super().__init__(rows)
        self.execute_calls = 0

    def execute(self, sql, params=None):
        self.execute_calls += 1


def test_blanket_risky_name_beats_enum_guard():
    # The real-world bug: res_partner.phone with only a handful of shared
    # company numbers (>=20 rows, few distinct, short) is exactly what
    # looks_like_enum() flags as a Selection field — but a name this obviously
    # PII should never reach that guard at all.
    cols = _cols(("res_partner", "phone", "character varying"))
    rows = [(v,) for v in (["+1-555-0100", "+1-555-0101", "+1-555-0102"] * 10)]
    cur = _CountingRowCursor(rows)
    plan = policy.build_blanket_plan(cols, cur=cur)
    assert plan.actions and plan.actions[0].transform_name == "fake_phone"
    assert "res_partner.phone" in plan.flagged
    assert not plan.excluded
    assert cur.execute_calls == 0   # never sampled — the whole point of Idea A


def test_blanket_risky_name_covers_more_than_phone():
    # Same low-cardinality trap, but for a different RISKY_PATTERNS category,
    # to confirm this isn't a phone-specific patch.
    cols = _cols(("res_partner", "vat", "character varying"))
    rows = [(v,) for v in (["FR123456789", "FR987654321"] * 15)]
    plan = policy.build_blanket_plan(cols, cur=_RowCursor(rows))
    assert plan.actions and plan.actions[0].transform_name == "scramble_digits"
    assert "res_partner.vat" in plan.flagged


def test_blanket_risky_name_only_applies_to_text():
    # RISKY_PATTERNS transforms (fake_phone, scramble_digits, ...) expect
    # string input; a numeric/date/bool column matching a pattern by
    # coincidence must still go through the normal type-based routing.
    cols = _cols(("t", "fax_count", "integer"))   # matches "fax" but is numeric
    plan = policy.build_blanket_plan(cols)
    assert plan.actions[0].transform_name == "noise_number"
    assert "t.fax_count" not in plan.flagged


def test_blanket_enum_guard_keeps_masking_high_cardinality():
    # "misc_text": a generic name that matches none of RISKY_PATTERNS, so this
    # exercises the content-guard path in isolation (see the risky-name tests
    # below for what happens when the column name IS a match).
    cols = _cols(("t", "misc_text", "text"))
    rows = [(f"unique text {i}",) for i in range(30)]    # all distinct
    plan = policy.build_blanket_plan(cols, cur=_RowCursor(rows))
    assert plan.actions and plan.actions[0].transform_name == "mask_text"


def test_blanket_override_skips_enum_guard():
    cols = _cols(("t", "flavor", "character varying"))
    rows = [(v,) for v in (["a", "b", "c"] * 10)]
    plan = policy.build_blanket_plan(
        cols, cur=_RowCursor(rows),
        overrides={"rules": {"t": {"flavor": "fake_name"}}})
    assert plan.actions and plan.actions[0].transform_name == "fake_name"


# --- blanket mode: filestore drop ------------------------------------------

def test_blanket_drops_filestore_by_default():
    cols = _cols(
        ("ir_attachment", "store_fname", "character varying"),
        ("ir_attachment", "db_datas", "bytea"),
        ("ir_attachment", "index_content", "text"),
        ("ir_attachment", "name", "character varying"),
    )
    plan = policy.build_blanket_plan(cols)
    by = {a.column: a.transform_name for a in plan.actions}
    assert by["store_fname"] == "null"
    assert by["db_datas"] == "null"          # bytea would otherwise be KEPT -> leak
    assert by["index_content"] == "null"
    assert by["name"] == "mask_text"         # normal columns unaffected


def test_blanket_keep_filestore_leaves_columns_untouched():
    # With --keep-filestore, the filestore columns are not force-nulled; since
    # ir_attachment is a technical table they then fall through to [technical].
    cols = _cols(
        ("ir_attachment", "store_fname", "character varying"),
        ("ir_attachment", "db_datas", "bytea"),
    )
    plan = policy.build_blanket_plan(cols, drop_filestore=False)
    by = {a.column: a.transform_name for a in plan.actions}
    assert "store_fname" not in by and "db_datas" not in by   # kept, not masked
    kept = " ".join(plan.excluded)
    assert "ir_attachment.store_fname [technical]" in kept

    # ...unless --include-technical, where keep-filestore does mean normal masking
    plan2 = policy.build_blanket_plan(cols, drop_filestore=False,
                                      exclude_technical=False)
    by2 = {a.column: a.transform_name for a in plan2.actions}
    assert by2["store_fname"] == "mask_text"


def test_blanket_filestore_override_wins():
    cols = _cols(("ir_attachment", "store_fname", "character varying"))
    plan = policy.build_blanket_plan(
        cols, overrides={"rules": {"ir_attachment": {"store_fname": "redact"}}})
    assert plan.actions[0].transform_name == "redact"


def test_apply_plan_null_fastpath_no_value_fetch():
    cols = [introspect.Column("ir_attachment", "db_datas", "bytea", True)]
    plan = policy.build_blanket_plan(cols)          # -> null
    assert plan.actions[0].transform_name == "null"
    cur = MockCursor(distinct=["shouldnotbefetched"])
    cur.rowcount = 3
    apply_plan(MockConn(cur), plan, dry_run=False)
    # single direct UPDATE ... SET NULL, and no SELECT DISTINCT round-trip
    assert len(cur.updates) == 1
    sql, params = cur.updates[0]
    assert "SET \"db_datas\" = NULL" in sql and params is None
    assert cur.selects == []


# --- blanket mode: ir_* technical exclusion + secret scrub -----------------

def test_blanket_keeps_ir_technical_tables():
    cols = _cols(
        ("ir_model", "model", "character varying"),
        ("ir_ui_view", "arch_db", "jsonb"),
        ("ir_config_parameter", "key", "character varying"),
        ("ir_config_parameter", "value", "text"),
        ("ir_act_server", "code", "text"),
        ("res_partner", "name", "character varying"),
    )
    plan = policy.build_blanket_plan(cols)
    acted = {a.column for a in plan.actions}
    assert acted == {"name"}                      # only the business table is masked
    kept = " ".join(plan.excluded)
    # all ir_* columns kept (some via default-exclude patterns like model/code,
    # the rest tagged [technical]) — either way, not masked
    for c in ("ir_model.model", "ir_ui_view.arch_db",
              "ir_config_parameter.key", "ir_act_server.code"):
        assert c in kept
    assert "ir_ui_view.arch_db [technical]" in kept
    assert "ir_config_parameter.value [technical]" in kept


def test_blanket_scrubs_secrets_inside_technical_tables():
    cols = _cols(
        ("ir_mail_server", "smtp_pass", "character varying"),
        ("ir_mail_server", "smtp_user", "character varying"),
        ("ir_mail_server", "name", "character varying"),          # label -> kept
        ("ir_logging", "message", "text"),
        ("ir_act_server", "webhook_url", "character varying"),
        ("ir_act_server", "code", "text"),                        # code -> kept
    )
    plan = policy.build_blanket_plan(cols)
    by = {(a.table, a.column): a.transform_name for a in plan.actions}
    assert by[("ir_mail_server", "smtp_pass")] == "null"
    assert by[("ir_mail_server", "smtp_user")] == "mask_text"
    assert by[("ir_logging", "message")] == "null"
    assert by[("ir_act_server", "webhook_url")] == "null"
    # non-secret technical columns stay kept (code via default-exclude pattern)
    assert ("ir_mail_server", "name") not in by
    assert ("ir_act_server", "code") not in by
    assert "ir_act_server.code" in " ".join(plan.excluded)


def test_blanket_ir_attachment_technical_links_kept_pii_scrubbed():
    cols = _cols(
        ("ir_attachment", "store_fname", "character varying"),   # filestore -> null
        ("ir_attachment", "db_datas", "bytea"),                  # filestore -> null
        ("ir_attachment", "name", "character varying"),          # secret -> mask
        ("ir_attachment", "access_token", "character varying"),  # secret -> null
        ("ir_attachment", "res_model", "character varying"),     # technical -> kept
        ("ir_attachment", "res_id", "integer"),                  # technical -> kept
    )
    plan = policy.build_blanket_plan(cols)
    by = {a.column: a.transform_name for a in plan.actions}
    assert by["store_fname"] == "null" and by["db_datas"] == "null"
    assert by["name"] == "mask_text" and by["access_token"] == "null"
    assert "res_model" not in by and "res_id" not in by
    kept = " ".join(plan.excluded)
    assert "ir_attachment.res_model" in kept          # kept (default-exclude pattern)
    assert "ir_attachment.res_id" in kept             # kept (res_id default-exclude)


def test_blanket_include_technical_masks_ir_tables():
    # arch_db isn't caught by a default-exclude pattern, so with technical
    # exclusion off it gets masked (jsonb -> mask_text via leaf masking).
    cols = _cols(("ir_ui_view", "arch_db", "jsonb"))
    plan = policy.build_blanket_plan(cols, exclude_technical=False)
    assert plan.actions and plan.actions[0].transform_name == "mask_text"


def test_blanket_override_beats_technical_exclusion():
    cols = _cols(("ir_config_parameter", "value", "text"))
    plan = policy.build_blanket_plan(
        cols, overrides={"rules": {"ir_config_parameter": {"value": "redact"}}})
    assert plan.actions and plan.actions[0].transform_name == "redact"


# --- verbose logging -------------------------------------------------------

def test_configure_logging_levels():
    import logging
    cli._configure_logging(verbose=True)
    assert logging.getLogger("scrubdb").level == logging.DEBUG
    cli._configure_logging(quiet=True)
    assert logging.getLogger("scrubdb").level == logging.WARNING
    cli._configure_logging()
    assert logging.getLogger("scrubdb").level == logging.INFO


def test_apply_plan_emits_progress_without_data_values():
    import logging
    cols = [introspect.Column("res_partner", "email", "character varying", True)]
    plan = policy.build_blanket_plan(
        cols, overrides={"rules": {"res_partner": {"email": "fake_email"}}})
    cur = MockCursor(distinct=["alice@real.com"])
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    lg = logging.getLogger("scrubdb.executor")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    try:
        apply_plan(MockConn(cur), plan, dry_run=True)
    finally:
        lg.removeHandler(handler)
    msgs = " ".join(records)
    assert "res_partner.email" in msgs and "fake_email" in msgs   # progress logged
    assert "alice@real.com" not in msgs                           # never the data value


# --- null on NOT NULL columns (empty instead of NULL) ----------------------

def test_empty_literal_by_type():
    from scrubdb import executor
    assert executor._empty_literal("text") == "''"
    assert executor._empty_literal("character varying") == "''"
    assert executor._empty_literal("jsonb") == "'null'::jsonb"
    assert executor._empty_literal("bytea") == "''::bytea"
    assert executor._empty_literal("integer") == "0"
    assert executor._empty_literal("boolean") == "false"
    assert executor._empty_literal("uuid") is None


def test_null_on_not_null_text_uses_empty_string():
    # ir_logging.name is a secret column (-> null) but NOT NULL: must blank, not NULL.
    cols = [introspect.Column("ir_logging", "name", "character varying", False)]
    plan = policy.build_blanket_plan(cols)
    a = plan.actions[0]
    assert a.transform_name == "null" and a.is_nullable is False
    cur = MockCursor()
    apply_plan(MockConn(cur), plan, dry_run=False)
    sql, _ = cur.updates[0]
    assert "= ''" in sql and "= NULL" not in sql


def test_null_on_nullable_column_sets_null():
    cols = [introspect.Column("ir_attachment", "index_content", "text", True)]
    plan = policy.build_blanket_plan(cols)          # filestore -> null, nullable
    assert plan.actions[0].transform_name == "null" and plan.actions[0].is_nullable
    cur = MockCursor()
    apply_plan(MockConn(cur), plan, dry_run=False)
    sql, _ = cur.updates[0]
    assert "= NULL" in sql


# --- unique-constraint safety (composite / non-text kept) ------------------

def test_blanket_keeps_composite_unique_columns():
    cols = _cols(
        ("mail_followers", "res_id", "integer"),
        ("mail_followers", "partner_id", "integer"),
    )
    plan = policy.build_blanket_plan(
        cols, composite_unique_columns={("mail_followers", "res_id"),
                                        ("mail_followers", "partner_id")})
    acted = {a.column for a in plan.actions}
    assert acted == set()                                  # both kept
    assert "mail_followers.res_id [unique]" in " ".join(plan.excluded) or \
           "mail_followers.res_id [excluded]" in " ".join(plan.excluded)


def test_blanket_keeps_non_text_single_unique():
    cols = _cols(("t", "serial_no", "integer"))
    plan = policy.build_blanket_plan(cols, unique_columns={("t", "serial_no")})
    assert not plan.actions
    assert "t.serial_no [unique]" in " ".join(plan.excluded)


def test_blanket_masks_single_unique_text_with_suffix():
    # single-column unique TEXT is still masked (suffix guarantees distinctness)
    cols = _cols(("shop", "slug", "character varying"))
    plan = policy.build_blanket_plan(cols, unique_columns={("shop", "slug")})
    assert plan.actions and plan.actions[0].transform_name == "mask_text"
    assert plan.actions[0].unique is True


def test_blanket_excludes_parent_path_by_default():
    # _parent_store materialized path: masking it breaks every child_of /
    # parent_of domain, and no PK/FK/type rule would otherwise protect it.
    cols = _cols(("stock_location", "parent_path", "character varying"),
                 ("product_category", "parent_path", "character varying"),
                 ("hr_department", "parent_path", "character varying"))
    plan = policy.build_blanket_plan(cols)
    assert not plan.actions
    kept = " ".join(plan.excluded)
    assert "stock_location.parent_path [excluded]" in kept
    assert "product_category.parent_path [excluded]" in kept
    assert "hr_department.parent_path [excluded]" in kept


def test_blanket_excludes_res_id_by_default():
    cols = _cols(("mail_message", "res_id", "integer"))
    plan = policy.build_blanket_plan(cols)      # no unique info; name pattern catches it
    assert not plan.actions
    assert "mail_message.res_id [excluded]" in " ".join(plan.excluded)


# --- Odoo Reference field guard ("model,id") -------------------------------

def test_looks_like_reference_true_and_false():
    refs = ["res.partner,42", "sale.order,17", "crm.lead,9"] * 3
    assert classify.looks_like_reference(refs) is True
    freetext = ["Jane Doe", "hello world", "a note"] * 3
    assert classify.looks_like_reference(freetext) is False
    # a comma-containing but non-reference value must not match
    assert classify.looks_like_reference(["Smith, John"] * 6) is False
    # single-word model (no dot) is not a reference
    assert classify.looks_like_reference(["partner,42"] * 6) is False


def test_blanket_keeps_reference_field():
    cols = _cols(("mail_activity", "res_ref", "character varying"))
    rows = [(v,) for v in (["res.partner,%d" % i for i in range(30)])]
    plan = policy.build_blanket_plan(cols, cur=_RowCursor(rows))
    assert not plan.actions
    assert "mail_activity.res_ref [reference]" in " ".join(plan.excluded)


def test_blanket_masks_normal_text_not_reference():
    cols = _cols(("res_partner", "misc_text", "text"))
    rows = [(f"free text note number {i}",) for i in range(30)]
    plan = policy.build_blanket_plan(cols, cur=_RowCursor(rows))
    assert plan.actions and plan.actions[0].transform_name == "mask_text"


# --- Studio x_ prefixed special columns ------------------------------------

def test_blanket_keeps_x_prefixed_special_columns():
    cols = _cols(
        ("x_my_model", "x_create_date", "timestamp without time zone"),  # audit
        ("x_my_model", "x_write_uid", "integer"),                        # audit
        ("x_my_model", "x_active", "boolean"),                           # active flag
        ("x_my_model", "x_state", "character varying"),                  # state flow
        ("x_my_model", "x_res_id", "integer"),                           # polymorphic ref
        ("x_my_model", "x_studio_ref", "character varying"),  # arbitrary Studio -> masked
        ("x_my_model", "x_partner_note", "text"),             # arbitrary custom -> masked
    )
    plan = policy.build_blanket_plan(cols)
    acted = {a.column for a in plan.actions}
    # x_ magic variants kept; arbitrary x_studio_/custom text is masked
    assert acted == {"x_studio_ref", "x_partner_note"}
    kept = " ".join(plan.excluded)
    assert "x_my_model.x_create_date [system]" in kept
    assert "x_my_model.x_write_uid [system]" in kept
    assert "x_my_model.x_active" in kept
    assert "x_my_model.x_state" in kept
    assert "x_my_model.x_res_id" in kept


def test_base_name_strips_x_only_not_studio():
    assert policy._base_name("x_create_date") == "create_date"
    assert policy._base_name("x_res_id") == "res_id"
    assert policy._base_name("x_active") == "active"
    # x_studio_ is NOT collapsed to the bare special name (stays arbitrary)
    assert policy._base_name("x_studio_active") == "studio_active"
    assert policy._base_name("partner_id") == "partner_id"      # unchanged


# --- transient (wizard / res.config.settings) tables -----------------------

def test_fetch_transient_tables():
    class Cur:
        def __init__(s): s.n = 0
        def execute(s, sql, params=None): s.n += 1; s._sql = sql
        def fetchall(s):
            # 1st call: ir_model existence check; 2nd: the transient models
            return [(1,)] if "information_schema.tables" in s._sql else \
                   [("res.config.settings",), ("base.language.install",)]
    t = introspect.fetch_transient_tables(Cur())
    assert "res_config_settings" in t and "base_language_install" in t


def test_fetch_transient_tables_non_odoo_returns_empty():
    class Cur:
        def execute(s, sql, params=None): s._sql = sql
        def fetchall(s): return []          # ir_model not present
    assert introspect.fetch_transient_tables(Cur()) == set()


def test_fetch_transient_relation_tables_finds_pure_m2m_cascade():
    # account_move_validate_account_move_rel: a pure Odoo m2m relation table
    # (two NOT NULL FK columns, nothing else) linking account_move to the
    # validate_account_move wizard. Clearing the wizard table cascades and
    # empties this table too, even though it's not a TransientModel itself.
    columns_rows = [
        ("account_move_validate_account_move_rel", "account_move_id", "NO"),
        ("account_move_validate_account_move_rel", "validate_account_move_id", "NO"),
        # a real, unrelated 3-column table must never be mistaken for one
        ("res_partner", "id", "NO"),
        ("res_partner", "name", "YES"),
        ("res_partner", "email", "YES"),
    ]
    fk_rows = [
        ("account_move_validate_account_move_rel", "account_move_id",
         "account_move", "id", "CASCADE"),
        ("account_move_validate_account_move_rel", "validate_account_move_id",
         "validate_account_move", "id", "CASCADE"),
    ]

    result = introspect.fetch_transient_relation_tables(
        _SchemaCursor(columns_rows, fk_rows), {"validate_account_move"})
    assert result == {"account_move_validate_account_move_rel"}


def test_fetch_transient_relation_tables_ignores_non_cascading_or_unrelated():
    columns_rows = [
        ("some_rel_table", "a_id", "NO"),
        ("some_rel_table", "b_id", "NO"),
    ]

    def Cur(fk_rows):
        return _SchemaCursor(columns_rows, fk_rows)

    # NOT NULL + CASCADE but neither side is actually a transient table.
    not_transient = Cur([("some_rel_table", "a_id", "table_a", "id", "CASCADE"),
                         ("some_rel_table", "b_id", "table_b", "id", "CASCADE")])
    assert introspect.fetch_transient_relation_tables(not_transient, {"some_wizard"}) == set()

    # Transient on one side, but RESTRICT instead of CASCADE — clearing the
    # wizard would fail outright, not silently cascade, so nothing to fold in.
    not_cascade = Cur([("some_rel_table", "a_id", "some_wizard", "id", "RESTRICT"),
                       ("some_rel_table", "b_id", "table_b", "id", "CASCADE")])
    assert introspect.fetch_transient_relation_tables(not_cascade, {"some_wizard"}) == set()


def test_blanket_truncates_transient_tables():
    cols = _cols(
        ("res_config_settings", "company_id", "integer"),
        ("res_config_settings", "some_config_value", "character varying"),
        ("res_partner", "name", "character varying"),
    )
    plan = policy.build_blanket_plan(
        cols, transient_tables={"res_config_settings"})
    acted = {a.column for a in plan.actions}
    assert acted == {"name"}                       # only the real table is masked
    # transient table is emptied wholesale, not masked and not per-column kept
    assert plan.truncate_tables == ["res_config_settings"]
    assert not any("res_config_settings" in e for e in plan.excluded)


def test_apply_plan_truncates_transient_tables():
    cols = _cols(("res_config_settings", "company_id", "integer"))
    plan = policy.build_blanket_plan(cols, transient_tables={"res_config_settings"})
    cur = MockCursor()
    report = apply_plan(MockConn(cur), plan, dry_run=False)
    assert cur.deletes and 'DELETE FROM "public"."res_config_settings"' in cur.deletes[0]
    assert report.truncated_tables == ["res_config_settings"]
    assert report.rows_deleted == 7


# --- --apply / --dry-run mutual exclusion ----------------------------------

def test_apply_and_dry_run_are_mutually_exclusive():
    parser = cli.build_parser()
    try:
        parser.parse_args(["kpi", "--apply", "--dry-run"])
        assert False, "expected SystemExit for --apply --dry-run together"
    except SystemExit as e:
        assert e.code == 2       # argparse usage error
    # each alone parses fine
    a = parser.parse_args(["kpi", "--apply"])
    assert a.apply and not a.dry_run
    d = parser.parse_args(["kpi", "--dry-run"])
    assert d.dry_run and not d.apply


# --- large-text server-side redaction --------------------------------------

def test_blanket_large_text_routes_to_hash_text():
    cols = _cols(("mail_message", "body", "text"))
    rows = [("<p>" + "x" * 400 + "</p>",)] * 10      # avg length ~406
    plan = policy.build_blanket_plan(cols, cur=_RowCursor(rows), big_text_threshold=200)
    assert plan.actions[0].transform_name == "hash_text"
    assert plan.actions[0].fn is None                # server-side, no Python fn


def test_blanket_short_text_stays_mask_text():
    cols = _cols(("res_partner", "misc_text", "character varying"))  # not risky-named
    rows = [("12 High St",), ("4 Oak Rd",), ("Flat 2",)] * 5      # short
    plan = policy.build_blanket_plan(cols, cur=_RowCursor(rows), big_text_threshold=200)
    assert plan.actions[0].transform_name == "mask_text"


def test_blanket_big_text_threshold_zero_disables():
    cols = _cols(("mail_message", "body", "text"))
    rows = [("x" * 500,)] * 10
    plan = policy.build_blanket_plan(cols, cur=_RowCursor(rows), big_text_threshold=0)
    assert plan.actions[0].transform_name == "mask_text"


def test_apply_plan_hash_text_is_server_side():
    cols = _cols(("mail_message", "body", "text"))
    rows = [("<p>" + "y" * 400 + "</p>",)] * 10
    plan = policy.build_blanket_plan(cols, cur=_RowCursor(rows), big_text_threshold=200)
    cur = MockCursor()
    apply_plan(MockConn(cur), plan, dry_run=False)
    assert cur.selects == []                          # no SELECT DISTINCT round-trip
    assert cur.updates and "md5(" in cur.updates[0][0]


# --- tz (timezone) must not be masked --------------------------------------

def test_blanket_keeps_timezone_fields():
    cols = _cols(
        ("res_users", "tz", "character varying"),
        ("event_event", "date_tz", "character varying"),
        ("res_users", "x_tz", "character varying"),
    )
    plan = policy.build_blanket_plan(cols)
    assert not plan.actions                           # all timezone fields kept
    kept = " ".join(plan.excluded)
    assert "res_users.tz" in kept and "event_event.date_tz" in kept
    assert "res_users.x_tz" in kept                   # x_ variant too


# --- post-mask finalization (dev copy) -------------------------------------

class _FinalizeCursor:
    """Cursor that reports Odoo tables present and records finalization SQL."""
    def __init__(self, users=None, odoo=True):
        self._users = users or []
        self._odoo = odoo
        self._result = []
        self.updates = []
        self.rowcount = 0
    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "information_schema.tables" in s:
            self._result = [(1,)] if self._odoo else []
        elif s.startswith("UPDATE"):
            self.updates.append((s, params)); self.rowcount = 5; self._result = []
        elif "FROM" in s and "res_users" in s:      # list_users SELECT
            self._result = list(self._users); self.rowcount = len(self._users)
        else:
            self._result = []
    def fetchall(self): return self._result


def test_finalize_reset_passwords():
    from scrubdb import finalize
    cur = _FinalizeCursor()
    n = finalize.reset_passwords(cur, "public", "admin")
    assert n == 5
    sql, params = cur.updates[0]
    assert 'res_users" SET password' in sql and params == ("admin",)


def test_finalize_deactivate_crons():
    from scrubdb import finalize
    cur = _FinalizeCursor()
    n = finalize.deactivate_crons(cur, "public")
    assert n == 5 and "ir_cron" in cur.updates[0][0] and "active = false" in cur.updates[0][0]


def test_finalize_list_users_joins_partner():
    from scrubdb import finalize
    cur = _FinalizeCursor(users=[(2, "xmasked", "zlogin_ab12", "admin")])
    rows = finalize.list_users(cur, "public", 100)
    assert rows == [(2, "xmasked", "zlogin_ab12", "admin")]


def test_finalize_noop_on_non_odoo():
    from scrubdb import finalize
    cur = _FinalizeCursor(odoo=False)
    assert finalize.reset_passwords(cur, "public", "admin") == 0
    assert finalize.deactivate_crons(cur, "public") == 0
    assert finalize.list_users(cur, "public", 100) == []


def test_apply_plan_runs_finalization():
    cols = _cols(("res_partner", "name", "character varying"))
    plan = policy.build_blanket_plan(cols)
    cur = _FinalizeCursor(users=[(2, "n", "l", "admin")])
    # MockConn works with any cursor
    report = apply_plan(MockConn(cur), plan, dry_run=True,
                        reset_password="admin", deactivate_crons=True, list_users=100)
    assert report.passwords_reset == 5
    assert report.crons_deactivated == 5
    assert report.user_list == [(2, "n", "l", "admin")]


# --- login normalization + root(id=1) protection ---------------------------

def test_finalize_normalize_logins_uses_user_id_skips_root():
    from scrubdb import finalize
    sqls = []
    class Cur:
        rowcount = 4
        def execute(s, sql, params=None): sqls.append(" ".join(sql.split()))
        def fetchall(s): return [(1,)]      # res_users exists
    finalize.normalize_logins(Cur(), "public")
    j = sqls[-1]
    assert "'user' || id::text" in j        # id-based handle
    assert "WHERE id <> 1" in j             # root untouched
    assert "row_number" not in j and "LIKE" not in j  # all non-root, not email-only


def test_blanket_excludes_res_users_login_for_finalize():
    cols = _cols(("res_users", "login", "character varying"),
                 ("res_users", "signature", "text"))
    plan = policy.build_blanket_plan(cols, unique_columns={("res_users", "login")},
                                     normalize_logins=True)
    acted = {a.column for a in plan.actions}
    assert "login" not in acted                       # owned by finalization
    assert "res_users.login [finalize]" in " ".join(plan.excluded)
    # with normalization off, login is masked normally
    plan2 = policy.build_blanket_plan(cols, unique_columns={("res_users", "login")},
                                      normalize_logins=False)
    assert any(a.column == "login" for a in plan2.actions)


def test_executor_never_touches_res_users_id_1():
    # every masking path on res_users must carry an id<>1 guard
    for dt, tf in [("character varying", "mask_text"),
                   ("text", "hash_text"), ("text", "null")]:
        cols = [introspect.Column("res_users", "signature", dt, True)]
        if tf == "mask_text":
            plan = policy.build_blanket_plan(cols, normalize_logins=False)
        else:
            plan = policy.Plan(actions=[policy.Action("res_users", "signature", tf,
                               (None if tf != "mask_text" else None), dt)])
        cur = MockCursor(distinct=["abc"])
        apply_plan(MockConn(cur), plan, dry_run=False)
        issued = " ".join(s for s, _ in cur.updates) + " ".join(cur.selects)
        assert "id <> 1" in issued, f"missing id<>1 guard for {tf}"


def test_reset_passwords_skips_root():
    from scrubdb import finalize
    sqls = []
    class Cur:
        rowcount = 3
        def execute(s, sql, params=None): sqls.append(" ".join(sql.split()))
        def fetchall(s): return [(1,)]
    finalize.reset_passwords(Cur(), "public", "admin")
    assert "WHERE id <> 1" in sqls[-1]


# --- person-name masking (keep first, mask rest) ---------------------------

def test_mask_name_keeps_first_masks_rest():
    ctx = Context(b"k")
    out = transforms.mask_name("John Smith", ctx)
    assert out.startswith("John ")            # first name kept
    assert out != "John Smith"                # surname masked
    assert len(out) == len("John Smith")      # shape preserved
    # three words: keep first, mask the other two
    out3 = transforms.mask_name("Mary Jane Watson", ctx)
    assert out3.startswith("Mary ") and out3.split()[1] != "Jane"


def test_mask_name_single_word_kept():
    ctx = Context(b"k")
    assert transforms.mask_name("Odoo", ctx) == "Odoo"
    assert transforms.mask_name(None, ctx) is None


def test_mask_name_deterministic():
    a, b = Context(b"k"), Context(b"k")
    assert transforms.mask_name("Jane Doe", a) == transforms.mask_name("Jane Doe", b)


def test_blanket_person_name_uses_mask_name():
    cols = _cols(("res_partner", "name", "character varying"),
                 ("hr_employee", "name", "character varying"),
                 ("res_partner", "misc_text", "text"))
    plan = policy.build_blanket_plan(cols)
    by = {(a.table, a.column): a.transform_name for a in plan.actions}
    assert by[("res_partner", "name")] == "mask_name"       # PERSON_NAME_COLUMNS wins
    assert by[("hr_employee", "name")] == "mask_name"        # over RISKY_PATTERNS' fake_name
    assert by[("res_partner", "misc_text")] == "mask_text"  # ordinary text unaffected


# --- Odoo Selection fields (core + custom/Studio) kept ---------------------

def test_fetch_selection_columns_maps_model_to_table():
    rows = [("website", "auth_signup_uninvited"),
            ("x_my_model", "x_studio_status")]     # custom/Studio selection
    class Cur:
        def __init__(s): s.n = 0
        def execute(s, sql, params=None): s.n += 1; s._sql = sql
        def fetchall(s):
            return [(1,)] if "information_schema.tables" in s._sql else rows
    sel = introspect.fetch_selection_columns(Cur())
    assert ("website", "auth_signup_uninvited") in sel
    assert ("x_my_model", "x_studio_status") in sel   # custom selection covered


def test_fetch_selection_columns_queries_ttype_selection():
    cur_sqls = []
    class Cur:
        def execute(s, sql, params=None): cur_sqls.append(" ".join(sql.split()))
        def fetchall(s): return [(1,)] if "information_schema" in cur_sqls[-1] else []
    introspect.fetch_selection_columns(Cur())
    assert any("ir_model_fields WHERE ttype = 'selection'" in q for q in cur_sqls)


def test_fetch_selection_columns_non_odoo_empty():
    class Cur:
        def execute(s, sql, params=None): s._sql = sql
        def fetchall(s): return []          # ir_model_fields absent
    assert introspect.fetch_selection_columns(Cur()) == set()


def test_blanket_keeps_selection_fields_core_and_custom():
    cols = _cols(
        ("website", "auth_signup_uninvited", "character varying"),  # core selection
        ("x_my_model", "x_studio_status", "character varying"),     # custom selection
        ("res_partner", "comment", "text"),                         # ordinary -> masked
    )
    sel = {("website", "auth_signup_uninvited"), ("x_my_model", "x_studio_status")}
    plan = policy.build_blanket_plan(cols, selection_columns=sel)
    acted = {a.column for a in plan.actions}
    assert acted == {"comment"}
    kept = " ".join(plan.excluded)
    assert "website.auth_signup_uninvited [selection]" in kept
    assert "x_my_model.x_studio_status [selection]" in kept


