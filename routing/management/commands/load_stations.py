"""Management command to import authoritative fuel station data."""

from pathlib import Path
from django.core.management.base import BaseCommand, CommandError

from routing.importer import (
    DataPipelineError,
    get_default_csv_path,
    import_stations_from_csv,
)


class Command(BaseCommand):
    help = (
        "Load fuel-price CSV: validates required fields and prices, excludes Canadian/unknown locations, "
        "applies deterministic duplicate policy (lowest price tier), and resolves coordinates from city gazetteer."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "csv",
            nargs="?",
            default=None,
            help="Path to fuel price CSV file (defaults to routing/data/fuel-prices-for-be-assessment.csv).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report without modifying the database.",
        )

    def handle(self, *args, **opts):
        csv_path = Path(opts["csv"]) if opts["csv"] else get_default_csv_path()
        dry_run = opts["dry_run"]
        verbosity = opts.get("verbosity", 1)

        self.stdout.write(f"Authoritative CSV input: {csv_path}")
        if dry_run:
            self.stdout.write(self.style.WARNING("Mode: DRY RUN (database will not be modified)"))

        try:
            stats = import_stations_from_csv(csv_path, dry_run=dry_run)
        except (DataPipelineError, FileNotFoundError) as e:
            raise CommandError(f"Data import failed: {e}")

        self.stdout.write(self.style.SUCCESS("\n--- Import Summary ---"))
        self.stdout.write(stats.summary())

        if stats.unmatched_stations and verbosity >= 1:
            self.stdout.write(self.style.WARNING(
                f"\nUnmatched US stations ({len(stats.unmatched_stations)} stations skipped due to missing city coords):"
            ))
            for u in stats.unmatched_stations:
                self.stdout.write(f"  - OPIS {u['opis_id']}: {u['name']} ({u['city']}, {u['state']})")

        if not dry_run:
            from routing.services import station_index
            station_index.cache_clear()
            self.stdout.write(self.style.SUCCESS(
                f"\nSuccessfully populated database with {stats.loaded_stations_count} stations."
            ))

