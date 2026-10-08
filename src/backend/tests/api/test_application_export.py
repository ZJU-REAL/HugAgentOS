"""CSV export preserves values while rendering spreadsheet formulas as text."""

from tests.api.test_application_hosting import create, data, hosted, table


def test_csv_export_treats_formula_prefixes_as_text(hosted):
    import csv
    import io

    client, _ = hosted
    app_id = create(client)
    table(client, app_id)
    values = ["=1+1", "\t=1+1", "\r=1+1", "\n=1+1", "＝1+1", "  =1+1"]
    path = f"/v1/applications/{app_id}/tables/entries"
    data(
        client.post(
            path + "/records",
            json={
                "rows": [
                    {"name": value, "email": f"csv-{index}@example.test"}
                    for index, value in enumerate(values)
                ]
            },
        )
    )
    response = client.get(path + "/export")
    assert response.status_code == 200
    exported = list(csv.DictReader(io.StringIO(response.text)))
    assert {row["name"] for row in exported} == {"'" + value for value in values}
    assert {row["name"] for row in data(client.get(path + "/records"))["items"]} == set(values)
