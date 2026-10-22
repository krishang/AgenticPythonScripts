#!/usr/bin/env python3
"""Export SQL Server schema metadata and definitions without exporting table rows."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import re
import shutil
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

VERSION = "1.0.0"
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

METADATA_QUERIES: dict[str, str] = {
    "Schemas": """
        SELECT s.schema_id AS SchemaId, s.name AS SchemaName,
               USER_NAME(s.principal_id) AS OwnerName
        FROM sys.schemas AS s
        WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
        ORDER BY s.name;
    """,
    "Objects": """
        SELECT s.name AS SchemaName, o.name AS ObjectName, o.type AS ObjectTypeCode,
               o.type_desc AS ObjectType, o.create_date AS CreateDate,
               o.modify_date AS ModifyDate, o.is_ms_shipped AS IsMicrosoftShipped,
               CASE WHEN sm.definition IS NULL
                          AND o.type IN ('V','P','FN','IF','TF','TR')
                    THEN 1 ELSE 0 END AS DefinitionUnavailable
        FROM sys.objects AS o
        JOIN sys.schemas AS s ON s.schema_id = o.schema_id
        LEFT JOIN sys.sql_modules AS sm ON sm.object_id = o.object_id
        WHERE o.type NOT IN ('S','IT')
          AND (? = 1 OR o.is_ms_shipped = 0)
        ORDER BY s.name, o.type_desc, o.name;
    """,
    "Columns": """
        SELECT s.name AS SchemaName, o.name AS ObjectName, o.type_desc AS ObjectType,
               c.column_id AS ColumnId, c.name AS ColumnName,
               CASE WHEN t.is_user_defined = 1
                    THEN SCHEMA_NAME(t.schema_id) + '.' + t.name ELSE t.name END AS DataType,
               c.max_length AS MaxLengthBytes, c.precision AS [Precision], c.scale AS Scale,
               c.collation_name AS CollationName, c.is_nullable AS IsNullable,
               c.is_identity AS IsIdentity, ic.seed_value AS IdentitySeed,
               ic.increment_value AS IdentityIncrement, c.is_computed AS IsComputed,
               cc.definition AS ComputedDefinition, cc.is_persisted AS IsPersisted,
               dc.name AS DefaultConstraintName, dc.definition AS DefaultDefinition,
               c.is_rowguidcol AS IsRowGuid, c.is_sparse AS IsSparse,
               c.is_filestream AS IsFileStream, c.generated_always_type_desc AS GeneratedAlwaysType
        FROM sys.columns AS c
        JOIN sys.objects AS o ON o.object_id = c.object_id
        JOIN sys.schemas AS s ON s.schema_id = o.schema_id
        JOIN sys.types AS t ON t.user_type_id = c.user_type_id
        LEFT JOIN sys.identity_columns AS ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id
        LEFT JOIN sys.computed_columns AS cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
        LEFT JOIN sys.default_constraints AS dc
            ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
        WHERE o.type IN ('U','V','TF','IF')
          AND (? = 1 OR o.is_ms_shipped = 0)
        ORDER BY s.name, o.name, c.column_id;
    """,
    "Keys": """
        SELECT s.name AS SchemaName, t.name AS TableName, kc.name AS ConstraintName,
               kc.type_desc AS ConstraintType, i.name AS IndexName,
               ic.key_ordinal AS KeyOrdinal, c.name AS ColumnName,
               ic.is_descending_key AS IsDescending
        FROM sys.key_constraints AS kc
        JOIN sys.tables AS t ON t.object_id = kc.parent_object_id
        JOIN sys.schemas AS s ON s.schema_id = t.schema_id
        JOIN sys.indexes AS i ON i.object_id = kc.parent_object_id AND i.index_id = kc.unique_index_id
        JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE ? = 1 OR t.is_ms_shipped = 0
        ORDER BY s.name, t.name, kc.name, ic.key_ordinal;
    """,
    "ForeignKeys": """
        SELECT fk.name AS ForeignKeyName, ps.name AS ParentSchema, pt.name AS ParentTable,
               pc.name AS ParentColumn, rs.name AS ReferencedSchema, rt.name AS ReferencedTable,
               rc.name AS ReferencedColumn, fkc.constraint_column_id AS ColumnOrdinal,
               fk.delete_referential_action_desc AS DeleteAction,
               fk.update_referential_action_desc AS UpdateAction, fk.is_disabled AS IsDisabled,
               fk.is_not_trusted AS IsNotTrusted,
               fk.is_not_for_replication AS IsNotForReplication
        FROM sys.foreign_keys AS fk
        JOIN sys.foreign_key_columns AS fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.tables AS pt ON pt.object_id = fk.parent_object_id
        JOIN sys.schemas AS ps ON ps.schema_id = pt.schema_id
        JOIN sys.columns AS pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
        JOIN sys.tables AS rt ON rt.object_id = fk.referenced_object_id
        JOIN sys.schemas AS rs ON rs.schema_id = rt.schema_id
        JOIN sys.columns AS rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
        WHERE ? = 1 OR pt.is_ms_shipped = 0
        ORDER BY ps.name, pt.name, fk.name, fkc.constraint_column_id;
    """,
    "Indexes": """
        SELECT s.name AS SchemaName, t.name AS TableName, i.name AS IndexName,
               i.type_desc AS IndexType, i.is_unique AS IsUnique,
               i.is_primary_key AS IsPrimaryKey, i.is_unique_constraint AS IsUniqueConstraint,
               i.has_filter AS HasFilter, i.filter_definition AS FilterDefinition,
               i.fill_factor AS [FillFactor], i.is_disabled AS IsDisabled,
               i.allow_row_locks AS AllowRowLocks, i.allow_page_locks AS AllowPageLocks,
               ic.key_ordinal AS KeyOrdinal, ic.is_included_column AS IsIncludedColumn,
               ic.is_descending_key AS IsDescending, c.name AS ColumnName,
               ds.name AS DataSpaceName
        FROM sys.indexes AS i
        JOIN sys.tables AS t ON t.object_id = i.object_id
        JOIN sys.schemas AS s ON s.schema_id = t.schema_id
        LEFT JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        LEFT JOIN sys.columns AS c ON c.object_id = i.object_id AND c.column_id = ic.column_id
        LEFT JOIN sys.data_spaces AS ds ON ds.data_space_id = i.data_space_id
        WHERE i.index_id > 0 AND (? = 1 OR t.is_ms_shipped = 0)
        ORDER BY s.name, t.name, i.name, ic.is_included_column, ic.key_ordinal, ic.index_column_id;
    """,
    "CheckConstraints": """
        SELECT s.name AS SchemaName, t.name AS TableName, cc.name AS ConstraintName,
               cc.definition AS Definition, cc.is_disabled AS IsDisabled,
               cc.is_not_trusted AS IsNotTrusted, cc.is_not_for_replication AS IsNotForReplication
        FROM sys.check_constraints AS cc
        JOIN sys.tables AS t ON t.object_id = cc.parent_object_id
        JOIN sys.schemas AS s ON s.schema_id = t.schema_id
        WHERE ? = 1 OR t.is_ms_shipped = 0
        ORDER BY s.name, t.name, cc.name;
    """,
    "Parameters": """
        SELECT s.name AS SchemaName, o.name AS ObjectName, o.type_desc AS ObjectType,
               p.parameter_id AS ParameterId, p.name AS ParameterName,
               CASE WHEN ty.is_user_defined = 1
                    THEN SCHEMA_NAME(ty.schema_id) + '.' + ty.name ELSE ty.name END AS DataType,
               p.max_length AS MaxLengthBytes, p.precision AS [Precision], p.scale AS Scale,
               p.is_output AS IsOutput, p.has_default_value AS HasDefaultValue,
               p.default_value AS DefaultValue, p.is_readonly AS IsReadOnly,
               p.is_cursor_ref AS IsCursorReference
        FROM sys.parameters AS p
        JOIN sys.objects AS o ON o.object_id = p.object_id
        JOIN sys.schemas AS s ON s.schema_id = o.schema_id
        JOIN sys.types AS ty ON ty.user_type_id = p.user_type_id
        WHERE ? = 1 OR o.is_ms_shipped = 0
        ORDER BY s.name, o.name, p.parameter_id;
    """,
    "Dependencies": """
        SELECT OBJECT_SCHEMA_NAME(d.referencing_id) AS ReferencingSchema,
               OBJECT_NAME(d.referencing_id) AS ReferencingObject,
               ro.type_desc AS ReferencingType,
               d.referenced_server_name AS ReferencedServer,
               d.referenced_database_name AS ReferencedDatabase,
               d.referenced_schema_name AS ReferencedSchema,
               d.referenced_entity_name AS ReferencedEntity,
               CASE WHEN d.referenced_id IS NOT NULL AND d.referenced_minor_id > 0
                    THEN COL_NAME(d.referenced_id, d.referenced_minor_id) END AS ReferencedColumn,
               d.is_schema_bound_reference AS IsSchemaBound,
               d.is_ambiguous AS IsAmbiguous,
               d.referenced_id AS ReferencedObjectId
        FROM sys.sql_expression_dependencies AS d
        LEFT JOIN sys.objects AS ro ON ro.object_id = d.referencing_id
        WHERE d.referencing_id IS NOT NULL
        ORDER BY ReferencingSchema, ReferencingObject, ReferencedSchema, ReferencedEntity;
    """,
    "Triggers": """
        SELECT COALESCE(OBJECT_SCHEMA_NAME(tr.parent_id), SCHEMA_NAME(o.schema_id)) AS ParentSchema,
               CASE WHEN tr.parent_class_desc = 'DATABASE' THEN DB_NAME()
                    ELSE OBJECT_NAME(tr.parent_id) END AS ParentObject,
               tr.name AS TriggerName, tr.parent_class_desc AS ParentClass,
               tr.is_instead_of_trigger AS IsInsteadOf, tr.is_disabled AS IsDisabled,
               tr.is_not_for_replication AS IsNotForReplication, te.type_desc AS TriggerEvent
        FROM sys.triggers AS tr
        LEFT JOIN sys.objects AS o ON o.object_id = tr.object_id
        LEFT JOIN sys.trigger_events AS te ON te.object_id = tr.object_id
        WHERE ? = 1 OR tr.is_ms_shipped = 0
        ORDER BY ParentSchema, ParentObject, tr.name, te.type_desc;
    """,
    "Synonyms": """
        SELECT SCHEMA_NAME(schema_id) AS SchemaName, name AS SynonymName,
               base_object_name AS BaseObjectName, create_date AS CreateDate, modify_date AS ModifyDate
        FROM sys.synonyms
        ORDER BY SchemaName, SynonymName;
    """,
    "Sequences": """
        SELECT SCHEMA_NAME(s.schema_id) AS SchemaName, s.name AS SequenceName,
               SCHEMA_NAME(t.schema_id) AS TypeSchema, t.name AS TypeName,
               s.start_value AS StartValue, s.increment AS IncrementValue,
               s.minimum_value AS MinimumValue, s.maximum_value AS MaximumValue,
               s.is_cycling AS IsCycling, s.is_cached AS IsCached, s.cache_size AS CacheSize,
               s.create_date AS CreateDate, s.modify_date AS ModifyDate
        FROM sys.sequences AS s
        JOIN sys.types AS t ON t.user_type_id = s.user_type_id
        ORDER BY SchemaName, SequenceName;
    """,
    "ExtendedProperties": """
        SELECT s.name AS SchemaName, o.name AS ObjectName, o.type_desc AS ObjectType,
               CASE ep.class WHEN 0 THEN 'DATABASE'
                    WHEN 1 THEN CASE WHEN ep.minor_id = 0 THEN 'OBJECT' ELSE 'COLUMN' END
                    WHEN 3 THEN 'SCHEMA' WHEN 6 THEN 'TYPE' WHEN 7 THEN 'INDEX'
                    ELSE ep.class_desc END AS PropertyLevel,
               CASE WHEN ep.class = 1 AND ep.minor_id > 0
                    THEN COL_NAME(ep.major_id, ep.minor_id) END AS ColumnName,
               ep.name AS PropertyName, CONVERT(nvarchar(max), ep.value) AS PropertyValue
        FROM sys.extended_properties AS ep
        LEFT JOIN sys.objects AS o ON ep.class = 1 AND ep.major_id = o.object_id
        LEFT JOIN sys.schemas AS s ON o.schema_id = s.schema_id
        ORDER BY SchemaName, ObjectName, PropertyLevel, ColumnName, PropertyName;
    """,
    "DatabasePrincipals": """
        SELECT name AS PrincipalName, type_desc AS PrincipalType,
               authentication_type_desc AS AuthenticationType, default_schema_name AS DefaultSchema,
               create_date AS CreateDate, modify_date AS ModifyDate, is_fixed_role AS IsFixedRole
        FROM sys.database_principals
        WHERE principal_id > 4 AND name NOT IN ('guest','INFORMATION_SCHEMA','sys')
        ORDER BY name;
    """,
    "RoleMemberships": """
        SELECT USER_NAME(rm.role_principal_id) AS RoleName,
               USER_NAME(rm.member_principal_id) AS MemberName
        FROM sys.database_role_members AS rm
        ORDER BY RoleName, MemberName;
    """,
    "Permissions": """
        SELECT USER_NAME(p.grantee_principal_id) AS Grantee,
               USER_NAME(p.grantor_principal_id) AS Grantor, p.state_desc AS PermissionState,
               p.permission_name AS PermissionName, p.class_desc AS PermissionClass,
               CASE p.class WHEN 0 THEN DB_NAME()
                    WHEN 1 THEN QUOTENAME(OBJECT_SCHEMA_NAME(p.major_id)) + '.' + QUOTENAME(OBJECT_NAME(p.major_id))
                    WHEN 3 THEN SCHEMA_NAME(p.major_id) WHEN 6 THEN TYPE_NAME(p.major_id)
                    ELSE CONVERT(nvarchar(128), p.major_id) END AS SecuredObject,
               CASE WHEN p.class = 1 AND p.minor_id > 0
                    THEN COL_NAME(p.major_id, p.minor_id) END AS ColumnName
        FROM sys.database_permissions AS p
        ORDER BY Grantee, PermissionClass, SecuredObject, PermissionName;
    """,
}

DATABASE_INFO_QUERY = """
    SELECT @@SERVERNAME AS ServerName,
           CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS ProductVersion,
           CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(128)) AS ProductLevel,
           CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS Edition,
           d.name AS DatabaseName, d.compatibility_level AS CompatibilityLevel,
           d.collation_name AS CollationName, d.recovery_model_desc AS RecoveryModel,
           d.containment_desc AS Containment, d.is_read_only AS IsReadOnly,
           d.is_auto_close_on AS IsAutoCloseOn, d.is_auto_shrink_on AS IsAutoShrinkOn,
           d.is_auto_create_stats_on AS IsAutoCreateStatsOn,
           d.is_auto_update_stats_on AS IsAutoUpdateStatsOn,
           d.snapshot_isolation_state_desc AS SnapshotIsolationState,
           d.is_read_committed_snapshot_on AS IsReadCommittedSnapshotOn,
           CONVERT(nvarchar(50), SYSDATETIMEOFFSET(), 127) AS ExportedAt
    FROM sys.databases AS d
    WHERE d.name = DB_NAME();
"""

MODULE_QUERY = """
    SELECT s.name AS SchemaName, o.name AS ObjectName, o.type AS ObjectTypeCode,
           o.type_desc AS ObjectType, o.is_ms_shipped AS IsMicrosoftShipped,
           sm.definition AS Definition
    FROM sys.objects AS o
    JOIN sys.schemas AS s ON s.schema_id = o.schema_id
    LEFT JOIN sys.sql_modules AS sm ON sm.object_id = o.object_id
    WHERE o.type IN ('V','P','FN','IF','TF','TR','AF')
      AND o.type NOT IN ('S','IT')
      AND (? = 1 OR o.is_ms_shipped = 0)
    ORDER BY s.name, o.type_desc, o.name;
"""

COLUMNS_FOR_TABLE_QUERY = """
    SELECT c.column_id AS ColumnId, c.name AS ColumnName,
           SCHEMA_NAME(ty.schema_id) AS TypeSchema, ty.name AS TypeName,
           ty.is_user_defined AS IsUserDefined, ty.is_assembly_type AS IsAssemblyType,
           c.max_length AS MaxLengthBytes, c.precision AS [Precision], c.scale AS Scale,
           c.collation_name AS CollationName, c.is_nullable AS IsNullable,
           c.is_identity AS IsIdentity, ic.seed_value AS IdentitySeed,
           ic.increment_value AS IdentityIncrement, c.is_computed AS IsComputed,
           cc.definition AS ComputedDefinition, cc.is_persisted AS IsPersisted,
           dc.name AS DefaultConstraintName, dc.definition AS DefaultDefinition,
           c.is_sparse AS IsSparse, c.is_rowguidcol AS IsRowGuid,
           c.is_filestream AS IsFileStream, c.generated_always_type_desc AS GeneratedAlwaysType
    FROM sys.columns AS c
    JOIN sys.types AS ty ON ty.user_type_id = c.user_type_id
    LEFT JOIN sys.identity_columns AS ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id
    LEFT JOIN sys.computed_columns AS cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
    LEFT JOIN sys.default_constraints AS dc
        ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
    WHERE c.object_id = OBJECT_ID(?)
    ORDER BY c.column_id;
"""

TABLE_CONSTRAINT_QUERY = """
    SELECT kc.name AS ConstraintName, kc.type_desc AS ConstraintType,
           ic.key_ordinal AS KeyOrdinal, c.name AS ColumnName, ic.is_descending_key AS IsDescending
    FROM sys.key_constraints AS kc
    JOIN sys.indexes AS i ON i.object_id = kc.parent_object_id AND i.index_id = kc.unique_index_id
    JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
    JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
    WHERE kc.parent_object_id = OBJECT_ID(?)
    ORDER BY kc.name, ic.key_ordinal;
"""

CHECK_CONSTRAINT_QUERY = """
    SELECT name AS ConstraintName, definition AS Definition, is_disabled AS IsDisabled
    FROM sys.check_constraints
    WHERE parent_object_id = OBJECT_ID(?)
    ORDER BY name;
"""

FOREIGN_KEY_QUERY = """
    SELECT fk.name AS ConstraintName, fkc.constraint_column_id AS ColumnOrdinal,
           pc.name AS ParentColumn, rs.name AS ReferencedSchema,
           rt.name AS ReferencedTable, rc.name AS ReferencedColumn,
           fk.delete_referential_action_desc AS DeleteAction,
           fk.update_referential_action_desc AS UpdateAction, fk.is_disabled AS IsDisabled
    FROM sys.foreign_keys AS fk
    JOIN sys.foreign_key_columns AS fkc ON fkc.constraint_object_id = fk.object_id
    JOIN sys.columns AS pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
    JOIN sys.tables AS rt ON rt.object_id = fk.referenced_object_id
    JOIN sys.schemas AS rs ON rs.schema_id = rt.schema_id
    JOIN sys.columns AS rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
    WHERE fk.parent_object_id = OBJECT_ID(?)
    ORDER BY fk.name, fkc.constraint_column_id;
"""

INDEX_QUERY = """
    SELECT i.name AS IndexName, i.type_desc AS IndexType, i.is_unique AS IsUnique,
           i.is_primary_key AS IsPrimaryKey, i.is_unique_constraint AS IsUniqueConstraint,
           i.has_filter AS HasFilter, i.filter_definition AS FilterDefinition,
           i.is_disabled AS IsDisabled, ic.key_ordinal AS KeyOrdinal,
           ic.is_included_column AS IsIncludedColumn, ic.is_descending_key AS IsDescending,
           c.name AS ColumnName
    FROM sys.indexes AS i
    LEFT JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
    LEFT JOIN sys.columns AS c ON c.object_id = i.object_id AND c.column_id = ic.column_id
    WHERE i.object_id = OBJECT_ID(?) AND i.index_id > 0
    ORDER BY i.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id;
"""

ROW_COUNTS_QUERY = """
    SELECT s.name AS SchemaName, t.name AS TableName, SUM(p.rows) AS ApproximateRowCount
    FROM sys.tables AS t
    JOIN sys.schemas AS s ON s.schema_id = t.schema_id
    JOIN sys.partitions AS p ON p.object_id = t.object_id AND p.index_id IN (0,1)
    WHERE (? = 1 OR t.is_ms_shipped = 0)
    GROUP BY s.name, t.name
    ORDER BY s.name, t.name;
"""


def quote_identifier(value: str) -> str:
    """Quote one SQL Server identifier."""
    return "[" + value.replace("]", "]]") + "]"


def safe_filename(value: str) -> str:
    safe = INVALID_FILENAME.sub("_", value).strip().rstrip(".")
    return (safe or "_")[:180]


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"Cannot serialize {type(value).__name__} to JSON")


def rows_as_dicts(cursor: Any) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description or ()]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def execute_rows(connection: Any, query: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cursor = connection.cursor()
    try:
        cursor.execute(query, parameters)
        return rows_as_dicts(cursor)
    finally:
        cursor.close()


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def sql_type(column: dict[str, Any]) -> str:
    name = str(column["TypeName"])
    if column["IsUserDefined"]:
        return f"{quote_identifier(str(column['TypeSchema']))}.{quote_identifier(name)}"
    lowered = name.lower()
    if lowered in {"varchar", "char", "varbinary", "binary"}:
        size = int(column["MaxLengthBytes"])
        return f"{name}({'max' if size == -1 else size})"
    if lowered in {"nvarchar", "nchar"}:
        size = int(column["MaxLengthBytes"])
        return f"{name}({'max' if size == -1 else size // 2})"
    if lowered in {"decimal", "numeric"}:
        return f"{name}({column['Precision']},{column['Scale']})"
    if lowered in {"datetime2", "datetimeoffset", "time"}:
        return f"{name}({column['Scale']})"
    return name


def sequence_ddl(schema: str, name: str, sequence: dict[str, Any]) -> str:
    qualified = f"{quote_identifier(schema)}.{quote_identifier(name)}"
    type_name = str(sequence["TypeName"])
    if sequence["TypeSchema"] and sequence["TypeSchema"] != "sys":
        type_name = f"{quote_identifier(str(sequence['TypeSchema']))}.{quote_identifier(type_name)}"
    else:
        type_name = quote_identifier(type_name)
    minimum = (
        f"MINVALUE {sequence['MinimumValue']}"
        if sequence["MinimumValue"] is not None else "NO MINVALUE"
    )
    maximum = (
        f"MAXVALUE {sequence['MaximumValue']}"
        if sequence["MaximumValue"] is not None else "NO MAXVALUE"
    )
    cycle = "CYCLE" if sequence["IsCycling"] else "NO CYCLE"
    cache = f"CACHE {sequence['CacheSize']}" if sequence["IsCached"] else "NO CACHE"
    return (
        f"CREATE SEQUENCE {qualified} AS {type_name}\n"
        f"    START WITH {sequence['StartValue']}\n"
        f"    INCREMENT BY {sequence['IncrementValue']}\n"
        f"    {minimum}\n"
        f"    {maximum}\n"
        f"    {cycle}\n"
        f"    {cache};\nGO\n"
    )


def table_ddl(connection: Any, schema: str, table: str) -> tuple[str, list[str]]:
    qualified = f"{quote_identifier(schema)}.{quote_identifier(table)}"
    object_name = f"{quote_identifier(schema)}.{quote_identifier(table)}"
    columns = execute_rows(connection, COLUMNS_FOR_TABLE_QUERY, (object_name,))
    if not columns:
        raise RuntimeError(f"No visible columns found for {schema}.{table}")
    limitations: list[str] = []
    lines: list[str] = []
    for column in columns:
        name = quote_identifier(str(column["ColumnName"]))
        if column["IsComputed"]:
            expression = column["ComputedDefinition"] or "/* definition unavailable */"
            line = f"    {name} AS {expression}"
            if column["IsPersisted"]:
                line += " PERSISTED"
        else:
            line = f"    {name} {sql_type(column)}"
            if column["IsIdentity"]:
                seed = column["IdentitySeed"] if column["IdentitySeed"] is not None else 1
                increment = column["IdentityIncrement"] if column["IdentityIncrement"] is not None else 1
                line += f" IDENTITY({seed},{increment})"
            if column["IsRowGuid"]:
                line += " ROWGUIDCOL"
            if column["IsSparse"]:
                line += " SPARSE"
            if column["IsFileStream"]:
                line += " FILESTREAM"
            if column["GeneratedAlwaysType"] and column["GeneratedAlwaysType"] != "NOT_APPLICABLE":
                limitations.append(f"{column['ColumnName']}: generated-always attribute {column['GeneratedAlwaysType']} needs review")
            if column["CollationName"]:
                line += f" COLLATE {quote_identifier(str(column['CollationName']))}"
            if column["DefaultDefinition"]:
                if column["DefaultConstraintName"]:
                    line += f" CONSTRAINT {quote_identifier(str(column['DefaultConstraintName']))}"
                line += f" DEFAULT {column['DefaultDefinition']}"
            line += " NULL" if column["IsNullable"] else " NOT NULL"
        lines.append(line)

    definitions = list(lines)
    key_rows = execute_rows(connection, TABLE_CONSTRAINT_QUERY, (object_name,))
    keys: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in key_rows:
        keys[(str(row["ConstraintName"]), str(row["ConstraintType"]))].append(row)
    for (constraint_name, constraint_type), members in keys.items():
        kind = "PRIMARY KEY" if constraint_type == "PRIMARY_KEY_CONSTRAINT" else "UNIQUE"
        cols = ", ".join(
            quote_identifier(str(member["ColumnName"])) +
            (" DESC" if member["IsDescending"] else " ASC")
            for member in members
        )
        definitions.append(f"    CONSTRAINT {quote_identifier(constraint_name)} {kind} ({cols})")

    for row in execute_rows(connection, CHECK_CONSTRAINT_QUERY, (object_name,)):
        definitions.append(
            f"    CONSTRAINT {quote_identifier(str(row['ConstraintName']))} "
            f"CHECK {row['Definition']}"
        )
        if row["IsDisabled"]:
            limitations.append(f"Check constraint {row['ConstraintName']} is disabled in the source")

    foreign_keys = execute_rows(connection, FOREIGN_KEY_QUERY, (object_name,))
    grouped_fks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in foreign_keys:
        grouped_fks[str(row["ConstraintName"])].append(row)
    for name, members in grouped_fks.items():
        first = members[0]
        parent_cols = ", ".join(quote_identifier(str(row["ParentColumn"])) for row in members)
        ref_cols = ", ".join(quote_identifier(str(row["ReferencedColumn"])) for row in members)
        ref_table = (
            f"{quote_identifier(str(first['ReferencedSchema']))}."
            f"{quote_identifier(str(first['ReferencedTable']))}"
        )
        line = (
            f"    CONSTRAINT {quote_identifier(name)} FOREIGN KEY ({parent_cols}) "
            f"REFERENCES {ref_table} ({ref_cols})"
        )
        if first["DeleteAction"] != "NO_ACTION":
            line += f" ON DELETE {first['DeleteAction'].replace('_', ' ')}"
        if first["UpdateAction"] != "NO_ACTION":
            line += f" ON UPDATE {first['UpdateAction'].replace('_', ' ')}"
        if first["IsDisabled"]:
            limitations.append(f"Foreign key {name} is disabled in the source")
        definitions.append(line)

    ddl = f"CREATE TABLE {qualified} (\n" + ",\n".join(definitions) + "\n);\nGO\n"
    index_rows = execute_rows(connection, INDEX_QUERY, (object_name,))
    grouped_indexes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in index_rows:
        if row["IndexName"] and not row["IsPrimaryKey"] and not row["IsUniqueConstraint"]:
            grouped_indexes[str(row["IndexName"])].append(row)
    for name, members in grouped_indexes.items():
        first = members[0]
        if first["IndexType"] != "CLUSTERED" and first["IndexType"] != "NONCLUSTERED":
            limitations.append(f"Index {name} uses unsupported type {first['IndexType']}; see Indexes.csv")
            continue
        key_members = [row for row in members if row["KeyOrdinal"] and not row["IsIncludedColumn"]]
        included = [row for row in members if row["IsIncludedColumn"]]
        if not key_members:
            limitations.append(f"Index {name} has no scriptable key columns; see Indexes.csv")
            continue
        index_type = "UNIQUE " if first["IsUnique"] else ""
        columns_sql = ", ".join(
            quote_identifier(str(row["ColumnName"])) + (" DESC" if row["IsDescending"] else " ASC")
            for row in key_members
        )
        ddl += f"CREATE {index_type}{first['IndexType']} INDEX {quote_identifier(name)} ON {qualified} ({columns_sql})"
        if included:
            ddl += " INCLUDE (" + ", ".join(quote_identifier(str(row["ColumnName"])) for row in included) + ")"
        if first["FilterDefinition"]:
            ddl += f" WHERE {first['FilterDefinition']}"
        ddl += ";\nGO\n"
        if first["IsDisabled"]:
            limitations.append(f"Index {name} is disabled in the source and was emitted enabled")
        if first["HasFilter"] and not first["FilterDefinition"]:
            limitations.append(f"Index {name} has an unavailable filter definition")
    return ddl, limitations


def make_connection_string(args: argparse.Namespace, password: str | None) -> str:
    parts = [
        f"Server={connection_value(args.server)}",
        f"Database={connection_value(args.database)}",
        f"Encrypt={'yes' if args.encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if args.trust_server_certificate else 'no'}",
    ]
    if args.username:
        parts.extend((f"UID={connection_value(args.username)}", f"PWD={connection_value(password or '')}"))
    else:
        parts.append("Trusted_Connection=yes")
    return ";".join(parts) + ";"


def connection_value(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


def connect(args: argparse.Namespace, password: str | None) -> Any:
    try:
        import mssql_python
    except ImportError as exc:
        raise RuntimeError(
            "Microsoft's mssql-python driver is required. Install it with: "
            "python -m pip install -r requirements.txt"
        ) from exc
    return mssql_python.connect(make_connection_string(args, password), timeout=30, autocommit=True)


def write_object_markdown(path: Path, row: dict[str, Any], columns: list[dict[str, Any]],
                          parameters: list[dict[str, Any]], dependencies: list[dict[str, Any]],
                          definition: str | None, status: str, note: str) -> None:
    schema = str(row.get("SchemaName") or "")
    name = str(row.get("ObjectName") or "")
    qualified = f"{schema}.{name}" if schema else name
    lines = [
        f"# {qualified}", "",
        f"- **Object type:** {row.get('ObjectType', 'Unknown')}",
        f"- **Schema:** {schema}", f"- **Object name:** {name}",
        f"- **Export status:** {status}",
    ]
    if note:
        lines.append(f"- **Export note:** {markdown_cell(note)}")
    if columns:
        lines += ["", "## Columns", "",
                  "| Ordinal | Column | Data type | Length | Precision | Scale | Nullable | Identity | Computed | Default |",
                  "|---:|---|---|---:|---:|---:|---|---|---|---|"]
        for col in columns:
            values = (col.get("ColumnId"), col.get("ColumnName"), col.get("DataType"),
                      col.get("MaxLengthBytes"), col.get("Precision"), col.get("Scale"),
                      col.get("IsNullable"), col.get("IsIdentity"), col.get("IsComputed"),
                      col.get("DefaultDefinition"))
            lines.append("| " + " | ".join(markdown_cell(value) for value in values) + " |")
    if parameters:
        lines += ["", "## Parameters", "",
                  "| Ordinal | Parameter | Data type | Length | Precision | Scale | Output | Read-only |",
                  "|---:|---|---|---:|---:|---:|---|---|"]
        for param in parameters:
            values = (param.get("ParameterId"), param.get("ParameterName"), param.get("DataType"),
                      param.get("MaxLengthBytes"), param.get("Precision"), param.get("Scale"),
                      param.get("IsOutput"), param.get("IsReadOnly"))
            lines.append("| " + " | ".join(markdown_cell(value) for value in values) + " |")
    lines += ["", "## Dependencies recorded by SQL Server", ""]
    if not dependencies:
        lines.append("No dependency rows were recorded for this object.")
    else:
        for dep in dependencies:
            target = ".".join(str(dep[key]) for key in (
                "ReferencedServer", "ReferencedDatabase", "ReferencedSchema", "ReferencedEntity"
            ) if dep.get(key))
            if dep.get("ReferencedColumn"):
                target += "." + str(dep["ReferencedColumn"])
            lines.append(f"- `{target}`")
    lines += ["", "## Definition", ""]
    if status == "Exported" and definition:
        lines += ["```sql", definition.rstrip(), "```"]
    else:
        lines.append("The SQL definition was unavailable; treat this object as opaque.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, default=json_default) + "\n", encoding="utf-8")


def create_archives(export_root: Path, output_root: Path, export_name: str,
                    copilot_root: Path | None) -> None:
    for archive, source in (
        (output_root / f"{export_name}.zip", export_root),
        (output_root / f"{export_name}_CopilotKnowledge.zip", copilot_root),
    ):
        if source is None:
            continue
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for path in source.rglob("*"):
                if path.is_file():
                    zip_file.write(path, path.relative_to(source).as_posix())


def export(args: argparse.Namespace, connection: Any) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_name = f"{safe_filename(args.database)}_Structure_{timestamp}"
    output_root = Path(args.output).expanduser().resolve()
    export_root = output_root / export_name
    copilot_root = None if args.skip_copilot_knowledge else export_root / "CopilotKnowledge"
    archive_paths = [output_root / f"{export_name}.zip"]
    if copilot_root:
        archive_paths.append(output_root / f"{export_name}_CopilotKnowledge.zip")
    existing = [path for path in [export_root, *archive_paths] if path.exists()]
    if existing and not args.overwrite:
        raise FileExistsError("Export output already exists; use --overwrite: " + ", ".join(map(str, existing)))
    for path in existing:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    output_root.mkdir(parents=True, exist_ok=True)

    metadata_root = export_root / "Metadata"
    metadata_root.mkdir(parents=True)
    info_rows = execute_rows(connection, DATABASE_INFO_QUERY)
    database_info = info_rows[0] if info_rows else {}
    write_json(export_root / "DatabaseInfo.json", database_info)

    query_args = (int(args.include_system_objects),)
    metadata_counts: dict[str, int] = {}
    metadata_rows: dict[str, list[dict[str, Any]]] = {}
    for name, query in METADATA_QUERIES.items():
        if name == "Permissions" and not args.include_permissions:
            continue
        print(f"[{time.strftime('%H:%M:%S')}] Exporting metadata: {name}", flush=True)
        uses_filter = name in {"Objects", "Columns", "Keys", "ForeignKeys", "Indexes",
                               "CheckConstraints", "Parameters", "Triggers"}
        rows = execute_rows(connection, query, query_args if uses_filter else ())
        metadata_rows[name] = rows
        metadata_counts[name] = len(rows)
        write_csv(metadata_root / f"{name}.csv", rows)
    if args.include_row_counts:
        print(f"[{time.strftime('%H:%M:%S')}] Exporting metadata: TableRowCounts", flush=True)
        rows = execute_rows(connection, ROW_COUNTS_QUERY, query_args)
        metadata_rows["TableRowCounts"] = rows
        metadata_counts["TableRowCounts"] = len(rows)
        write_csv(metadata_root / "TableRowCounts.csv", rows)

    index: list[dict[str, Any]] = []
    combined: list[str] = []
    objects = metadata_rows["Objects"]
    columns_by_object: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    parameters_by_object: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    dependencies_by_object: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in metadata_rows["Columns"]:
        columns_by_object[(str(row["SchemaName"]), str(row["ObjectName"]))].append(row)
    for row in metadata_rows["Parameters"]:
        parameters_by_object[(str(row["SchemaName"]), str(row["ObjectName"]))].append(row)
    for row in metadata_rows["Dependencies"]:
        dependencies_by_object[(str(row["ReferencingSchema"]), str(row["ReferencingObject"]))].append(row)

    module_rows = execute_rows(connection, MODULE_QUERY, query_args)
    modules = {(str(row["SchemaName"]), str(row["ObjectName"])): row for row in module_rows}
    sequences = execute_rows(connection, METADATA_QUERIES["Sequences"])
    sequences_by_name = {
        (str(row["SchemaName"]), str(row["SequenceName"])): row for row in sequences
    }
    table_rows = execute_rows(
        connection,
        "SELECT s.name AS SchemaName, t.name AS ObjectName, t.is_ms_shipped AS IsMicrosoftShipped "
        "FROM sys.tables AS t JOIN sys.schemas AS s ON s.schema_id=t.schema_id "
        "WHERE (?=1 OR t.is_ms_shipped=0) ORDER BY s.name,t.name;",
        query_args,
    )
    object_keys = {(str(row["SchemaName"]), str(row["ObjectName"])) for row in objects}
    for table in table_rows:
        key = (str(table["SchemaName"]), str(table["ObjectName"]))
        if key not in object_keys:
            objects.append({
                "SchemaName": key[0], "ObjectName": key[1], "ObjectTypeCode": "U",
                "ObjectType": "USER_TABLE", "IsMicrosoftShipped": table["IsMicrosoftShipped"],
                "DefinitionUnavailable": 0,
            })

    copilot_dirs: dict[str, Path] = {}
    if copilot_root:
        names = {
            "Metadata": "01-Metadata", "USER_TABLE": "02-Tables",
            "VIEW": "03-Views", "SQL_STORED_PROCEDURE": "04-StoredProcedures",
            "SQL_SCALAR_FUNCTION": "05-Functions", "SQL_TABLE_VALUED_FUNCTION": "05-Functions",
            "SQL_INLINE_TABLE_VALUED_FUNCTION": "05-Functions", "SQL_TRIGGER": "06-Triggers",
            "SYNONYM": "07-Synonyms", "SEQUENCE_OBJECT": "08-Sequences",
            "TYPE": "09-UserDefinedTypes", "XML_SCHEMA_COLLECTION": "10-XmlSchemaCollections",
            "PARTITION_FUNCTION": "11-PartitionFunctions", "PARTITION_SCHEME": "12-PartitionSchemes",
            "Reference": "99-Reference",
        }
        for key, folder in names.items():
            directory = copilot_root / folder
            directory.mkdir(parents=True, exist_ok=True)
            copilot_dirs[key] = directory
        for path in metadata_root.glob("*.csv"):
            shutil.copy2(path, copilot_dirs["Metadata"] / path.name)
        shutil.copy2(export_root / "DatabaseInfo.json", copilot_dirs["Metadata"] / "DatabaseInfo.json")

    print(f"[{time.strftime('%H:%M:%S')}] Scripting database objects: {len(objects)}", flush=True)
    for position, row in enumerate(objects, 1):
        schema = str(row.get("SchemaName") or "")
        name = str(row.get("ObjectName") or "")
        object_type = str(row.get("ObjectType") or row.get("ObjectTypeCode") or "UNKNOWN")
        key = (schema, name)
        qualified = f"{quote_identifier(schema)}.{quote_identifier(name)}" if schema else quote_identifier(name)
        file_name = safe_filename(f"{schema}.{name}" if schema else name) + ".sql"
        folder_name = {
            "USER_TABLE": "Tables", "VIEW": "Views", "SQL_STORED_PROCEDURE": "StoredProcedures",
            "SQL_SCALAR_FUNCTION": "Functions", "SQL_TABLE_VALUED_FUNCTION": "Functions",
            "SQL_INLINE_TABLE_VALUED_FUNCTION": "Functions", "SQL_TRIGGER": "Triggers",
        }.get(object_type)
        status, note, definition = "Exported", "", None
        if folder_name:
            destination = export_root / folder_name / file_name
        elif object_type == "SEQUENCE_OBJECT":
            destination = export_root / "Sequences" / file_name
        else:
            destination = export_root / "OtherObjects" / file_name
        if position == 1 or position % 100 == 0:
            print(f"  [{position}/{len(objects)}] {schema}.{name}", flush=True)
        try:
            if object_type == "USER_TABLE":
                definition, limitations = table_ddl(connection, schema, name)
                if limitations:
                    note = "; ".join(limitations)
            elif key in modules and modules[key].get("Definition"):
                definition = str(modules[key]["Definition"])
                if not definition.endswith("\n"):
                    definition += "\n"
            elif object_type == "SEQUENCE_OBJECT" and key in sequences_by_name:
                sequence = sequences_by_name[key]
                definition = sequence_ddl(schema, name, sequence)
            elif object_type == "SYNONYM":
                syn = next((item for item in metadata_rows["Synonyms"]
                            if item["SchemaName"] == schema and item["SynonymName"] == name), None)
                if syn:
                    definition = f"CREATE SYNONYM {qualified} FOR {syn['BaseObjectName']};\n"
            if definition is None:
                status = "Definition unavailable"
                note = "Unsupported object type, encrypted definition, or insufficient metadata visibility."
                definition = f"-- Definition unavailable for {object_type} {schema}.{name}.\n"
        except Exception as exc:
            status = "Error"
            note = str(exc)
            definition = f"-- DDL generation failed for {object_type} {schema}.{name}.\n-- {note}\n"
            print(f"Warning: failed to script {schema}.{name}: {note}", file=sys.stderr)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(definition, encoding="utf-8")
        combined.extend([f"-- {'=' * 76}", f"-- {object_type}: {schema}.{name}",
                         f"-- {'=' * 76}", definition.rstrip(), "GO", ""])
        copilot_file = ""
        if copilot_root:
            directory = copilot_dirs.get(object_type, copilot_dirs["Reference"])
            copilot_path = directory / file_name.replace(".sql", ".md")
            write_object_markdown(
                copilot_path, row, columns_by_object.get(key, []),
                parameters_by_object.get(key, []), dependencies_by_object.get(key, []),
                definition if status == "Exported" else None, status, note,
            )
            copilot_file = copilot_path.relative_to(export_root).as_posix()
        index.append({
            "ObjectType": object_type, "SchemaName": schema, "ObjectName": name,
            "File": destination.relative_to(export_root).as_posix(),
            "CopilotFile": copilot_file, "Status": status, "Notes": note,
        })

    index.sort(key=lambda item: (item["ObjectType"], item["SchemaName"], item["ObjectName"]))
    write_csv(export_root / "ScriptedObjectIndex.csv", index)
    (export_root / "CombinedSchema.sql").write_text(
        f"/* Schema-only export: {args.server} / {args.database}; generated {datetime.now().astimezone().isoformat()}. */\n"
        f"USE {quote_identifier(args.database)};\nGO\n\n" + "\n".join(combined),
        encoding="utf-8",
    )
    counts = dict(Counter(item["ObjectType"] for item in index))
    manifest = {
        "Exporter": "export_sql_database_structure.py",
        "ExporterVersion": VERSION,
        "ServerInstance": args.server,
        "Database": args.database,
        "GeneratedAt": datetime.now().astimezone().isoformat(),
        "Authentication": "SQL authentication" if args.username else "Windows integrated authentication",
        "EncryptConnection": args.encrypt,
        "TrustServerCertificate": args.trust_server_certificate,
        "IncludeSystemObjects": args.include_system_objects,
        "IncludePermissions": args.include_permissions,
        "IncludeRowCounts": args.include_row_counts,
        "CopilotKnowledgeCreated": copilot_root is not None,
        "TableDataIncluded": False,
        "ScriptedObjectCounts": counts,
        "MetadataRowCounts": metadata_counts,
        "Warnings": [
            "Encrypted module definitions cannot be exported.",
            "Objects hidden by metadata permissions may be omitted.",
            "Table DDL is assembled from catalog metadata; verify advanced SQL Server features and indexes.",
            "Server-level objects, SQL Agent objects, credentials, certificates, and secrets are excluded.",
        ],
    }
    write_json(export_root / "Manifest.json", manifest)
    (export_root / "README.txt").write_text(
        f"SQL SERVER DATABASE STRUCTURE EXPORT\n\nServer: {args.server}\nDatabase: {args.database}\n"
        f"Generated: {manifest['GeneratedAt']}\n\n"
        "No table rows were exported. Review SQL definitions for environment-specific literals before sharing.\n"
        "See ScriptedObjectIndex.csv for object-level status and notes.\n",
        encoding="utf-8",
    )
    if copilot_root:
        for source_name in ("Manifest.json", "ScriptedObjectIndex.csv"):
            shutil.copy2(export_root / source_name, copilot_dirs["Metadata"] / source_name)
        overview = [
            f"# Database overview: {args.database}", "",
            f"- **Server:** {args.server}", f"- **Generated:** {manifest['GeneratedAt']}",
            "- **Table row data included:** No", "", "## Object counts", "",
        ]
        overview.extend(f"- **{name}:** {count}" for name, count in sorted(counts.items()))
        (copilot_root / "00-Database-Overview.md").write_text("\n".join(overview) + "\n", encoding="utf-8")
        (copilot_root / "README.md").write_text(
            "# Database agent knowledge source\n\n"
            "Use the exported metadata and object definitions as the source of truth. "
            "Do not invent objects or relationships. Treat unavailable and encrypted modules as opaque. "
            "Definitions may contain environment-specific information; review before sharing.\n",
            encoding="utf-8",
        )
        unavailable = [item for item in index if item["Status"] != "Exported"]
        register = ["# Unavailable or failed object definitions", ""]
        if unavailable:
            register += ["| Type | Schema | Object | Status | Note |", "|---|---|---|---|---|"]
            for item in unavailable:
                values = (item["ObjectType"], item["SchemaName"], item["ObjectName"], item["Status"], item["Notes"])
                register.append("| " + " | ".join(markdown_cell(value) for value in values) + " |")
        else:
            register.append("No unavailable object definitions were recorded.")
        (copilot_dirs["Reference"] / "Unavailable-Objects.md").write_text("\n".join(register) + "\n", encoding="utf-8")

    create_archives(export_root, output_root, export_name, copilot_root)
    print(f"Export completed: {export_root}")
    print(f"Technical ZIP: {archive_paths[0]}")
    if copilot_root:
        print(f"Copilot ZIP: {archive_paths[1]}")
    print(f"Objects indexed: {len(index)}")
    return export_root


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="SQL Server hostname, instance, or host,port")
    parser.add_argument("--database", required=True, help="Database name")
    parser.add_argument("--output", required=True, help="Parent directory for timestamped exports")
    parser.add_argument("--username", help="SQL login; password is requested interactively")
    parser.add_argument("--encrypt", action="store_true", help="Encrypt the SQL Server connection")
    parser.add_argument("--trust-server-certificate", action="store_true",
                        help="Skip TLS certificate validation")
    parser.add_argument("--include-system-objects", action="store_true")
    parser.add_argument("--include-permissions", action="store_true")
    parser.add_argument("--include-row-counts", action="store_true")
    parser.add_argument("--skip-copilot-knowledge", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    password = getpass.getpass("SQL password: ") if args.username else None
    connection = None
    try:
        connection = connect(args, password)
        print(f"[{time.strftime('%H:%M:%S')}] Connected to {args.server} / {args.database}", flush=True)
        export(args, connection)
        return 0
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
