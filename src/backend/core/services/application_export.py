"""One consistent, bounded-memory CSV stream of a complete application table."""
import csv
import io
import json
from core.services.application_store import owned_application
from core.services.application_relational import table_definition, json_record
from core.services.application_sql_policy import application_role
from sqlalchemy import select

def csv_cell(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠"))):
        return "'" + value
    return value

def export_csv(service, app_id, owner, name):
    # Authorization and schema errors happen before StreamingResponse sends 200.
    with service.engine.connect() as connection:
        app = owned_application(connection, app_id, owner)
        definition = table_definition(app, name)
    table = service._table(app_id, definition)
    def stream():
        with service.engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin(), application_role(connection, app_id):
                buffer = io.StringIO()
                writer = csv.DictWriter(buffer, fieldnames=list(table.c.keys()))
                writer.writeheader()
                yield buffer.getvalue()
                result = connection.execute(
                    select(table).order_by(table.c.created_at, table.c.id).execution_options(stream_results=True, yield_per=500)).mappings()
                try:
                    for row in result:
                        buffer.seek(0); buffer.truncate(0)
                        writer.writerow({k: csv_cell(v) for k, v in json_record(row).items()})
                        yield buffer.getvalue()
                finally:
                    result.close()
    return stream()
