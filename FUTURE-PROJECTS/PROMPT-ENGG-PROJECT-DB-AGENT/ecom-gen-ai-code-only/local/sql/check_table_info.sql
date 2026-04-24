-- check_table_info.sql
-- Retrieves table and column metadata, including descriptions, for a specified schema
-- Replace 'ecommerce' with the desired schema name

-- List all tables in the schema with their descriptions
SELECT
    pgns.nspname AS schema_name,
    pgclass.relname AS table_name,
    obj_description(pgclass.oid, 'pg_class') AS table_description
FROM
    pg_catalog.pg_class pgclass
JOIN
    pg_catalog.pg_namespace pgns ON pgclass.relnamespace = pgns.oid
WHERE
    pgns.nspname = 'ecommerce'
    AND pgclass.relkind = 'r'  -- Regular tables only
ORDER BY
    pgclass.relname;

-- List all columns in the schema with their data types and descriptions
SELECT
    cols.table_schema AS schema_name,
    cols.table_name,
    cols.column_name,
    cols.data_type,
    pgdesc.description AS column_description
FROM
    information_schema.columns cols
LEFT JOIN
    pg_catalog.pg_class pgclass ON cols.table_name = pgclass.relname
LEFT JOIN
    pg_catalog.pg_namespace pgns ON pgclass.relnamespace = pgns.oid
LEFT JOIN
    pg_catalog.pg_attribute pgattr ON pgclass.oid = pgattr.attrelid AND cols.column_name = pgattr.attname
LEFT JOIN
    pg_catalog.pg_description pgdesc ON pgattr.attrelid = pgdesc.objoid AND pgattr.attnum = pgdesc.objsubid
WHERE
    cols.table_schema = 'ecommerce'
ORDER BY
    cols.table_name, cols.column_name;


DO $$
DECLARE
    r RECORD;
BEGIN
    -- Remove all table comments
    FOR r IN
        SELECT schemaname, tablename
        FROM pg_tables
        WHERE schemaname = 'marketing'  -- Replace with your schema
    LOOP
        EXECUTE format('COMMENT ON TABLE %I.%I IS NULL',
                      r.schemaname, r.tablename);
    END LOOP;

    -- Remove all column comments
    FOR r IN
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'marketing'  -- Replace with your schema
    LOOP
        EXECUTE format('COMMENT ON COLUMN %I.%I.%I IS NULL',
                      r.table_schema, r.table_name, r.column_name);
    END LOOP;
END $$;