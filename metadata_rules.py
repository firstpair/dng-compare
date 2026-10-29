"""Shared tag rules, read from metadata_rules.json.

Semantic Unison's `unison-dngscan -semdiff` reads the same file, so both tools
agree on which tags are compared, which are volatile, which are filesystem
tags and which cannot affect rendering."""
import json, pathlib, re

RULES_PATH = pathlib.Path(__file__).with_name('metadata_rules.json')


class Rules:
    def __init__(self, data):
        if data.get('format') != 'dng-compare-metadata-rules/1':
            raise ValueError('unsupported metadata rules format')
        self.copy = re.compile(data['group_copy'])
        self.never = data['never_compared']
        self.volatile_ = data['volatile']
        self.filesystem = tuple(data['filesystem_prefixes'])
        self.plain = data['non_rendering']

    @staticmethod
    def _match(rule, tag):
        return tag in rule['exact'] or tag.startswith(tuple(rule['prefixes']))

    def normalize(self, tag):
        """The tag without CopyN group components."""
        return ':'.join(part for part in tag.split(':') if not self.copy.match(part))

    def compared(self, tag):
        return not self._match(self.never, tag) and not self._match(self.volatile_, tag)

    def category(self, tag):
        return 'filesystem' if tag.startswith(self.filesystem) else 'embedded'

    def non_rendering(self, tag):
        """True only for listed tags; unknown tags may affect rendering."""
        return self._match(self.plain, self.normalize(tag))


def load(path=RULES_PATH):
    return Rules(json.loads(pathlib.Path(path).read_text()))
