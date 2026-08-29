# Native Windows backend tests

Use the repository-local virtual environment. It is ignored by Git and is
created from the bundled native Windows Python runtime when needed.

From the repository root, install or refresh the development dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
```

Run backend tests with:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q -p no:cacheprovider --basetemp backend\.pytest-local
```

Use a focused test path in place of `backend\tests` during development. The
PostgreSQL integration tests remain skipped unless an explicitly configured
local `PV_TEST_DATABASE_URL` is available; never point that variable at the
production database.

For any change affecting PostgreSQL-backed behaviour, run the relevant
PostgreSQL-backed tests. For architectural migrations, focused tests alone are
not sufficient: audit production consumers of the changed contract and record
any intentional compatibility paths.
