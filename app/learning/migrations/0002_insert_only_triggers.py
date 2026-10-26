"""Database-level guarantee that history is never rewritten.

The models already refuse to update or delete a saved row; these triggers also stop a queryset
`update()`, a `delete()` or a hand-written statement. PostgreSQL only (the project's database);
on any other engine the migration does nothing and the model-level guard remains. TRUNCATE is not
blocked, so a test database can still be reset."""

from django.db import migrations

INSERT_ONLY_TABLES = [
    "learning_liveprediction",
    "learning_predictionoutcome",
    "learning_feedbackevent",
    "learning_candidateevaluation",
    "learning_modelevent",
]

FUNCTION = """
CREATE OR REPLACE FUNCTION learning_insert_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is insert-only: rows cannot be updated or deleted', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""


def create(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    # params=None: the function body contains a literal % that must not be read as a placeholder
    schema_editor.execute(FUNCTION, params=None)
    for table in INSERT_ONLY_TABLES:
        schema_editor.execute(
            f"CREATE TRIGGER {table}_insert_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION learning_insert_only()"
        )


def drop(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in INSERT_ONLY_TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_insert_only ON {table}")
    schema_editor.execute("DROP FUNCTION IF EXISTS learning_insert_only()")


class Migration(migrations.Migration):
    dependencies = [("learning", "0001_initial")]
    operations = [migrations.RunPython(create, drop)]
