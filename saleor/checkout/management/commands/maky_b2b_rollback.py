"""MAKY-B2B-V1: prepare a rollback to a Core without the B2B patch.

See docs/operations/MAKY-B2B-V1-OPERATIONS.md §5.
"""

from django.core.management.base import BaseCommand, CommandError

from ...maky_b2b import MODE_DRAIN, get_mode
from ...maky_b2b_rollback import (
    RollbackNotAllowedError,
    UnmanageStatus,
    inventory,
    unmanage_all,
)

CHECK = "check"
UNMANAGE = "unmanage"


def _transactions(items) -> str:
    return ",".join(f"{item.pk}:{item.psp_reference or '-'}" for item in items) or "-"


class Command(BaseCommand):
    help = (
        "check: list managed B2B checkouts and their payment attempts; fails "
        "unless the mode is 'drain' and no managed checkout is left. "
        "unmanage: in mode 'drain', turn managed checkouts without payment "
        "attempts into ordinary checkouts."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=[CHECK, UNMANAGE])

    def handle(self, *args, **options):
        if options["action"] == UNMANAGE:
            self._unmanage()
        else:
            self._check()

    def _check(self):
        mode = get_mode()
        managed = inventory()
        self.stdout.write(f"mode={mode} managed_checkouts={len(managed)}")
        for item in managed:
            self.stdout.write(
                f"checkout={item.checkout_pk} "
                f"tax_exemption={item.tax_exemption} "
                f"decision={item.decision} "
                f"state_signature_valid={item.state_verified} "
                f"attempts={item.attempts.status} "
                f"funded={_transactions(item.attempts.funded)} "
                f"unresolved={_transactions(item.attempts.unresolved)} "
                f"being_created={item.attempts.being_created}"
            )
        blocking = sum(1 for item in managed if item.blocks_rollback)
        if mode != MODE_DRAIN:
            raise CommandError(
                f"NOT READY: mode is '{mode}'. Set MAKY_B2B_VAT_EXEMPTION_MODE="
                f"{MODE_DRAIN} in every Core process first."
            )
        if managed:
            raise CommandError(
                f"NOT READY: {len(managed)} managed checkout(s), {blocking} with "
                "payment attempts to resolve."
            )
        self.stdout.write("READY: no managed checkout is left.")

    def _unmanage(self):
        try:
            results = unmanage_all()
        except RollbackNotAllowedError as e:
            raise CommandError(e.message) from e
        for result in results:
            line = f"checkout={result.checkout_pk} {result.status}"
            if result.status == UnmanageStatus.BLOCKED and result.attempts:
                line += f" attempts={result.attempts.status}"
            self.stdout.write(line)
        counts = {
            status: sum(1 for result in results if result.status == status)
            for status in (
                UnmanageStatus.UNMANAGED,
                UnmanageStatus.BLOCKED,
                UnmanageStatus.SKIPPED,
            )
        }
        self.stdout.write(
            " ".join(f"{status.lower()}={count}" for status, count in counts.items())
        )
