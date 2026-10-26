"""
manage.py promote_model <name> --actor <superuser> --reason "..."   |   --rollback

The only way the active estimation method changes. Needs an active superuser, a written reason, a
registered candidate the live estimator can run, no business-rule block, and a recent RECOMMENDED
evaluation. Every action is stored in the model history. `--rollback` restores the method that was
active before the last promotion.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from learning import registry


class Command(BaseCommand):
    help = "Promote a candidate method to active, or roll the last promotion back (audited)."

    def add_arguments(self, parser):
        parser.add_argument("name", nargs="?", help="Candidate method to promote.")
        parser.add_argument("--actor", required=True, help="Username of an active superuser.")
        parser.add_argument("--reason", required=True)
        parser.add_argument("--rollback", action="store_true")

    def handle(self, *args, **opts):
        user = get_user_model().objects.filter(username=opts["actor"]).first()
        try:
            if opts["rollback"]:
                event = registry.rollback(user, opts["reason"])
            else:
                if not opts["name"]:
                    raise CommandError("Give the name of the candidate to promote.")
                model = registry.ModelVersion.objects.filter(
                    in_registry=True, name=opts["name"]).first()
                if model is None:
                    raise CommandError(f"No registered method called {opts['name']!r}.")
                event = registry.promote(model, user, opts["reason"])
        except registry.RegistryError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{event.get_kind_display()}: {event.model.name} (event #{event.pk})")
