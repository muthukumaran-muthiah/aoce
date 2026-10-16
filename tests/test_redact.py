"""redact_query: literal values in pg_stat_activity text never reach the model (AOCE-39)."""

import pytest

from aoce.tools.db import redact_query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "SELECT * FROM users WHERE email = 'jane@example.com' AND id = 42",
            "SELECT * FROM users WHERE email = $1 AND id = $2",
        ),
        ("UPDATE t SET token = 'it''s secret', ratio = 0.75", "UPDATE t SET token = $1, ratio = $2"),
        ("SELECT E'a\\'b secret', 'x'", "SELECT $1, $2"),
        ("SELECT $body$ card 4111 1111 $body$, $$ssn$$", "SELECT $1, $2"),
        ("SELECT U&'d\\0061t', B'1010', X'1F', 1e9, .5", "SELECT $1, $2, $3, $4, $5"),
        ("INSERT INTO orders VALUES ($1, 'paid', 3)", "INSERT INTO orders VALUES ($1, $2, $3)"),
        # Truncated at 300 characters in the middle of a literal.
        ("SELECT * FROM users WHERE ssn = '123-45-67", "SELECT * FROM users WHERE ssn = $1"),
        ("SELECT * FROM t WHERE note = $q$never closed", "SELECT * FROM t WHERE note = $1"),
        # Comments can carry values (sqlcommenter) and stray apostrophes.
        (
            "SELECT 1 /* user=alice@example.com token=abc123 */ FROM t WHERE a = 'x'",
            "SELECT $1 /* */ FROM t WHERE a = $2",
        ),
        ("SELECT a FROM t WHERE b = 2 -- don't log alice@example.com", "SELECT a FROM t WHERE b = $1 --"),
        ("SELECT a /* cut off at 300 chars, bob@example.com", "SELECT a /* */"),
    ],
)
def test_literals_become_parameters(query, expected):
    assert redact_query(query) == expected


def test_identifiers_and_structure_are_kept():
    query = 'SELECT "Order Id", col2, t1.x FROM catalog.products_v2 p WHERE p.id IN ($1, $2) LIMIT 10'
    assert redact_query(query) == 'SELECT "Order Id", col2, t1.x FROM catalog.products_v2 p WHERE p.id IN ($1, $2) LIMIT $3'


def test_comment_markers_inside_literals_are_literals():
    assert redact_query("SELECT '-- not a comment', '/* x */' FROM t") == "SELECT $1, $2 FROM t"


def test_quote_inside_identifier_is_not_a_string():
    assert redact_query('SELECT "it\'s" FROM t WHERE a = \'secret\'') == 'SELECT "it\'s" FROM t WHERE a = $1'


@pytest.mark.parametrize("query", [None, ""])
def test_empty_query_passes_through(query):
    assert redact_query(query) == query
