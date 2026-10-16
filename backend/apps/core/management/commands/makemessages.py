"""makemessages: 似た既存訳の自動流用（fuzzy）を止める。fuzzy は誤訳になりやすいため。"""

from django.core.management.commands import makemessages


class Command(makemessages.Command):
    msgmerge_options = [*makemessages.Command.msgmerge_options, "--no-fuzzy-matching"]
