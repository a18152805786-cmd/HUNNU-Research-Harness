from __future__ import annotations

from collections import defaultdict

from .models import UNKNOWN, LiteratureRecord, PublicationStatus
from .normalization import normalize_doi, normalize_person, normalize_title


_STATUS_RANK = {
    PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value: 80,
    PublicationStatus.ONLINE_FIRST.value: 70,
    PublicationStatus.ACCEPTED_MANUSCRIPT.value: 60,
    PublicationStatus.CONFERENCE_PAPER.value: 50,
    PublicationStatus.WORKING_PAPER.value: 40,
    PublicationStatus.PREPRINT.value: 30,
    PublicationStatus.PROFESSIONAL_ARTICLE.value: 20,
    PublicationStatus.OTHER.value: 10,
    PublicationStatus.UNKNOWN.value: 0,
}


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _keys(record: LiteratureRecord) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    doi = normalize_doi(record.doi)
    title = normalize_title(record.title)
    first_author = normalize_person(record.first_author)
    if doi != UNKNOWN:
        keys.append(("DOI exact match", doi))
    if title != UNKNOWN and record.year != UNKNOWN:
        keys.append(("normalized title + year", f"{title}|{record.year}"))
    if title != UNKNOWN and first_author != UNKNOWN:
        keys.append(("title + first author", f"{title}|{first_author}"))
    if record.sha256 not in ("", UNKNOWN):
        keys.append(("SHA256 identical file", record.sha256.casefold()))
    return keys


def _canonical_score(record: LiteratureRecord) -> tuple[int, int, int, int]:
    return (
        _STATUS_RANK.get(record.publication_status, 0),
        int(record.doi != UNKNOWN),
        int(record.full_text_downloaded),
        int(record.year) if str(record.year).isdigit() else 0,
    )


class LiteratureDeduplicator:
    def deduplicate(self, records: list[LiteratureRecord]) -> list[LiteratureRecord]:
        union_find = _UnionFind(len(records))
        first_by_key: dict[tuple[str, str], int] = {}
        for index, record in enumerate(records):
            if record.canonical_paper_id == UNKNOWN:
                record.canonical_paper_id = record.paper_id
            for key in _keys(record):
                if key in first_by_key:
                    union_find.union(index, first_by_key[key])
                else:
                    first_by_key[key] = index

        groups: dict[int, list[int]] = defaultdict(list)
        for index in range(len(records)):
            groups[union_find.find(index)].append(index)

        for indexes in groups.values():
            canonical_index = max(indexes, key=lambda item: _canonical_score(records[item]))
            canonical = records[canonical_index]
            canonical.canonical_paper_id = canonical.paper_id
            canonical.duplicate_detected = False
            canonical.duplicate_reason = UNKNOWN
            statuses = {records[item].publication_status for item in indexes}
            titles = {normalize_title(records[item].title) for item in indexes}
            versioned = len(statuses) > 1 and len(titles) == 1
            for index in indexes:
                if index == canonical_index:
                    canonical.same_work_different_version = versioned
                    continue
                record = records[index]
                record.duplicate_detected = True
                record.canonical_paper_id = canonical.paper_id
                record.same_work_different_version = versioned
                record.archived_as_alternate_version = versioned
                record.duplicate_reason = self._reason(record, canonical)
        return records

    @staticmethod
    def _reason(record: LiteratureRecord, canonical: LiteratureRecord) -> str:
        if normalize_doi(record.doi) != UNKNOWN and normalize_doi(record.doi) == normalize_doi(canonical.doi):
            return "DOI exact match"
        if (
            normalize_title(record.title) == normalize_title(canonical.title)
            and record.year != UNKNOWN
            and record.year == canonical.year
        ):
            return "normalized title + year"
        if (
            normalize_title(record.title) == normalize_title(canonical.title)
            and normalize_person(record.first_author) != UNKNOWN
            and normalize_person(record.first_author) == normalize_person(canonical.first_author)
        ):
            return "title + first author"
        if record.sha256 != UNKNOWN and record.sha256.casefold() == canonical.sha256.casefold():
            return "SHA256 identical file"
        return "same-work evidence"

