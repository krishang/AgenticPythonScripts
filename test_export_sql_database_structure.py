import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from export_sql_database_structure import (
    DATABASE_INFO_QUERY,
    METADATA_QUERIES,
    execute_rows,
    make_connection_string,
    markdown_cell,
    quote_identifier,
    safe_filename,
    sequence_ddl,
    table_ddl,
    write_csv,
    write_json,
)


class FakeCursor:
    def __init__(self, responses):
        self.responses = responses
        self.description = None
        self.rows = []
        self.parameters = None

    def execute(self, query, parameters=()):
        self.parameters = parameters
        for prefix, columns, rows in self.responses:
            if query.lstrip().startswith(prefix):
                self.description = [(column,) for column in columns]
                self.rows = rows
                return self
        raise AssertionError(f"Unexpected SQL query: {query[:80]}")

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, responses):
        self.responses = responses
        self.last_cursor = None

    def cursor(self):
        self.last_cursor = FakeCursor(self.responses)
        return self.last_cursor


class ExporterHelperTests(unittest.TestCase):
    def test_indexes_query_brackets_fill_factor_alias(self):
        self.assertIn("i.fill_factor AS [FillFactor]", METADATA_QUERIES["Indexes"])

    def test_database_export_timestamp_is_string(self):
        self.assertIn(
            "CONVERT(nvarchar(50), SYSDATETIMEOFFSET(), 127) AS ExportedAt",
            DATABASE_INFO_QUERY,
        )

    def test_identifier_and_filename_safety(self):
        self.assertEqual(quote_identifier("a]b"), "[a]]b]")
        self.assertEqual(safe_filename("a/b. "), "a_b")
        self.assertEqual(safe_filename(""), "_")

    def test_markdown_cell_escapes_table_delimiters(self):
        self.assertEqual(markdown_cell("a|b\nc"), r"a\|b c")
        self.assertEqual(markdown_cell(None), "")

    def test_sequence_ddl_uses_valid_system_type_name(self):
        ddl = sequence_ddl(
            "dbo",
            "OrderNumber",
            {
                "TypeSchema": "sys",
                "TypeName": "bigint",
                "StartValue": 1,
                "IncrementValue": 1,
                "MinimumValue": None,
                "MaximumValue": None,
                "IsCycling": False,
                "IsCached": True,
                "CacheSize": 50,
            },
        )
        self.assertIn("CREATE SEQUENCE [dbo].[OrderNumber] AS [bigint]", ddl)
        self.assertIn("NO MINVALUE", ddl)
        self.assertIn("CACHE 50", ddl)

    def test_connection_string_escapes_values_without_selecting_system_driver(self):
        args = SimpleNamespace(
            server="sql;host",
            database="db}name",
            encrypt=True,
            trust_server_certificate=False,
            username="user",
        )
        connection_string = make_connection_string(args, "p}ass")
        self.assertIn("Server={sql;host}", connection_string)
        self.assertIn("Database={db}}name}", connection_string)
        self.assertIn("PWD={p}}ass}", connection_string)
        self.assertIn("Encrypt=yes", connection_string)
        self.assertNotIn("DRIVER=", connection_string.upper())

    def test_windows_authentication_uses_trusted_connection(self):
        args = SimpleNamespace(
            server="sqlserver",
            database="testdb",
            encrypt=False,
            trust_server_certificate=False,
            username=None,
        )
        connection_string = make_connection_string(args, None)
        self.assertIn("Trusted_Connection=yes", connection_string)
        self.assertNotIn("UID=", connection_string)

    def test_execute_rows_passes_db_api_parameter_sequence(self):
        connection = FakeConnection([("SELECT value", ["Value"], [(7,)])])
        rows = execute_rows(connection, "SELECT value", (7,))
        self.assertEqual(rows, [{"Value": 7}])
        self.assertEqual(connection.last_cursor.parameters, (7,))

    def test_table_ddl_includes_columns_constraints_and_index(self):
        responses = [
            (
                "SELECT c.column_id AS ColumnId",
                ["ColumnId", "ColumnName", "TypeSchema", "TypeName", "IsUserDefined",
                 "IsAssemblyType", "MaxLengthBytes", "Precision", "Scale", "CollationName",
                 "IsNullable", "IsIdentity", "IdentitySeed", "IdentityIncrement", "IsComputed",
                 "ComputedDefinition", "IsPersisted", "DefaultConstraintName", "DefaultDefinition",
                 "IsSparse", "IsRowGuid", "IsFileStream", "GeneratedAlwaysType"],
                [
                    (1, "Id", "sys", "int", False, False, 4, 10, 0, None, False, True,
                     1, 1, False, None, False, None, None, False, False, False, "NOT_APPLICABLE"),
                    (2, "Name", "sys", "nvarchar", False, False, 100, 0, 0, None, True, False,
                     None, None, False, None, False, "DF_Example_Name", "('unknown')",
                     False, False, False, "NOT_APPLICABLE"),
                ],
            ),
            (
                "SELECT kc.name AS ConstraintName",
                ["ConstraintName", "ConstraintType", "KeyOrdinal", "ColumnName", "IsDescending"],
                [("PK_Example", "PRIMARY_KEY_CONSTRAINT", 1, "Id", False)],
            ),
            (
                "SELECT name AS ConstraintName, definition AS Definition",
                ["ConstraintName", "Definition", "IsDisabled"],
                [("CK_Example_Name", "([Name]<>N'')", False)],
            ),
            (
                "SELECT fk.name AS ConstraintName",
                ["ConstraintName", "ColumnOrdinal", "ParentColumn", "ReferencedSchema",
                 "ReferencedTable", "ReferencedColumn", "DeleteAction", "UpdateAction", "IsDisabled"],
                [],
            ),
            (
                "SELECT i.name AS IndexName",
                ["IndexName", "IndexType", "IsUnique", "IsPrimaryKey", "IsUniqueConstraint",
                 "HasFilter", "FilterDefinition", "IsDisabled", "KeyOrdinal", "IsIncludedColumn",
                 "IsDescending", "ColumnName"],
                [("IX_Example_Name", "NONCLUSTERED", False, False, False, False,
                  None, False, 1, False, False, "Name")],
            ),
        ]
        ddl, limitations = table_ddl(FakeConnection(responses), "dbo", "Example")
        self.assertIn("CREATE TABLE [dbo].[Example]", ddl)
        self.assertIn("[Id] int IDENTITY(1,1) NOT NULL", ddl)
        self.assertIn("CONSTRAINT [DF_Example_Name] DEFAULT ('unknown') NULL", ddl)
        self.assertIn("CONSTRAINT [PK_Example] PRIMARY KEY ([Id] ASC)", ddl)
        self.assertIn("CREATE NONCLUSTERED INDEX [IX_Example_Name]", ddl)
        self.assertEqual(limitations, [])

    def test_csv_and_json_writers(self):
        with tempfile.TemporaryDirectory() as temp:
            csv_path = Path(temp) / "data.csv"
            json_path = Path(temp) / "data.json"
            write_csv(csv_path, [{"Name": "one", "Count": 1}])
            write_json(json_path, {"Name": "one"})
            with csv_path.open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(next(csv.DictReader(stream)), {"Name": "one", "Count": "1"})
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["Name"], "one")


if __name__ == "__main__":
    unittest.main()
