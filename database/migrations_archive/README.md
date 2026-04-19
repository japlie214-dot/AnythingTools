<!-- database/migration_archive/README.md -->
# Migrations Archive

This directory stores older migration scripts that have been folded into the base schema (`database/schemas/`). 
Because the migration runner enforces a strict maximum of 3 active migration files, older files must be moved here after their DDL modifications are permanently integrated into the Python schema definitions.

These files are retained strictly for historical auditing and developer reference. They are **not** executed by the runner.