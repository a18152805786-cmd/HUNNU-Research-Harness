from __future__ import annotations

from datetime import datetime

from .models import LiteratureRecord, LiteratureSearchRequest, PublicationStatus, ScreeningDecision, UNKNOWN
from .normalization import normalize_doi, normalize_title


class LiteratureScreener:
    """Transparent rule-based first-pass screening; never deletes a result."""

    def screen(self, record: LiteratureRecord, request: LiteratureSearchRequest) -> LiteratureRecord:
        haystack = normalize_title(" ".join((record.title, record.abstract, " ".join(record.keywords))))
        concepts = [
            normalize_title(term)
            for term in (*request.keywords_cn, *request.keywords_en)
            if normalize_title(term) != UNKNOWN
        ]
        exact_title_match = any(normalize_title(title) == normalize_title(record.title) for title in request.exact_titles)
        exact_doi_match = any(normalize_doi(doi) == normalize_doi(record.doi) for doi in request.dois)

        matched = [concept for concept in concepts if concept in haystack]
        score = 0.0
        reasons: list[str] = []
        if exact_title_match or exact_doi_match:
            score += 50
            reasons.append("Exact user-supplied title/DOI match (+50)")
        elif matched:
            direct = min(40, 15 + 10 * len(matched))
            score += direct
            reasons.append(f"Direct topic terms matched={len(matched)} (+{direct})")
        else:
            reasons.append("No requested topic term found (+0)")

        if record.publication_status == PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value:
            score += 20
            reasons.append("Publisher-identified journal article (+20)")
        elif record.publication_status == PublicationStatus.ONLINE_FIRST.value:
            score += 15
            reasons.append("Online-first journal item (+15)")

        current_year = datetime.now().year
        if str(record.year).isdigit():
            age = current_year - int(record.year)
            if age <= 5:
                score += 15
                reasons.append("Published within five years (+15)")
            elif age <= 10:
                score += 8
                reasons.append("Published within ten years (+8)")
            if request.year_start and int(record.year) < request.year_start:
                score -= 20
                reasons.append("Older than requested start year (-20)")
            if request.year_end and int(record.year) > request.year_end:
                score -= 20
                reasons.append("Newer than requested end year (-20)")

        mechanism_terms = ("audit", "auditor", "审计", "earnings management", "盈余管理", "accrual", "应计")
        if any(normalize_title(term) in haystack for term in mechanism_terms):
            score += 10
            reasons.append("Requested mechanism/method term present (+10)")
        if record.full_text_accessible:
            score += 10
            reasons.append("Authorized full-text control present (+10)")
        if normalize_doi(record.doi) != UNKNOWN:
            score += 5
            reasons.append("DOI available (+5)")

        score = max(0.0, min(100.0, score))
        if exact_title_match or exact_doi_match or score >= 60:
            decision = ScreeningDecision.KEEP
        elif score >= 30:
            decision = ScreeningDecision.MAYBE
        else:
            decision = ScreeningDecision.REJECT

        record.relevance_score = score
        record.screening_decision = decision.value
        record.screening_reason = "; ".join(reasons)
        record.ai_assisted = False
        return record

