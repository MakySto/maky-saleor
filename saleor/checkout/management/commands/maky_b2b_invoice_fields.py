"""MAKY-B2B-V1: readable invoice rows on B2B orders created before they existed.

Without ``--apply`` only lists the orders. See
docs/operations/MAKY-B2B-V1-OPERATIONS.md §6.
"""

from django.core.management.base import BaseCommand

from ...maky_b2b_invoice import RowsStatus, refresh_order_rows


class Command(BaseCommand):
    help = (
        "List B2B orders whose readable invoice rows (maky.b2b.faktura.*) are "
        "missing or outdated. With --apply, write them; the signed snapshot "
        "maky.b2b.order.v1 is never changed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Write the missing rows."
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        rows = refresh_order_rows(apply=apply)
        for row in rows:
            action = " updated" if row.updated else ""
            self.stdout.write(
                f"order={row.number} decision={row.decision or '-'} "
                f"rows={row.status}{action}"
            )
        pending = sum(
            row.status in (RowsStatus.MISSING, RowsStatus.OUTDATED) for row in rows
        )
        self.stdout.write(
            f"mode={'apply' if apply else 'dry-run'} orders={len(rows)} "
            f"pending={pending} updated={sum(row.updated for row in rows)} "
            f"broken={sum(row.status == RowsStatus.BROKEN_SNAPSHOT for row in rows)}"
        )
