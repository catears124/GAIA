from __future__ import annotations

from . import career_surface_collector as career

_EXTRA_CAREER_PATHS = (
    "/internships",
    "/students",
    "/student-opportunities",
    "/early-careers",
    "/early-talent",
    "/university",
    "/university-recruiting",
    "/careers/search",
    "/jobs/search",
    "/job-search",
    "/vacancies",
)
_EXTRA_CAREER_MARKERS = (
    "internship",
    "internships",
    "student-opportunit",
    "early-career",
    "early-talent",
    "university-recruit",
    "job-search",
    "search-jobs",
)
_EXTRA_DETAIL_MARKERS = (
    "/job-detail/",
    "/job-details/",
    "/posting/",
    "/postings/",
    "/vacancy/",
    "/vacancies/",
    "/role/",
    "/roles/",
)
_EXTRA_PROVIDER_HOST_FRAGMENTS = {
    "breezy.hr": "breezy",
    "applytojob.com": "jazzhr",
    "personio.com": "personio",
    "jobs.personio.de": "personio",
    "comeet.co": "comeet",
    "pinpointhq.com": "pinpoint",
    "applicantpro.com": "applicantpro",
    "trakstar.com": "trakstar-hire",
    "hire.trakstar.com": "trakstar-hire",
    "careers-page.com": "manatal",
    "manatal.com": "manatal",
}


def _append_unique(existing: tuple[str, ...], additions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*existing, *additions)))


def install_coverage_extensions() -> None:
    """Expand bounded discovery without inventing or duplicating source records.

    Hosted ATS recognition is deliberately generic: it keeps shared hosts tenant-scoped,
    captures their observed job links as recursive provider evidence, and avoids unsafe
    root-path guessing. First-class collectors can replace these generic surfaces later.
    """

    career.CAREER_PATHS = _append_unique(career.CAREER_PATHS, _EXTRA_CAREER_PATHS)
    career.CAREER_MARKERS = _append_unique(career.CAREER_MARKERS, _EXTRA_CAREER_MARKERS)
    career.DETAIL_MARKERS = _append_unique(career.DETAIL_MARKERS, _EXTRA_DETAIL_MARKERS)
    career.PROVIDER_HOST_FRAGMENTS.update(_EXTRA_PROVIDER_HOST_FRAGMENTS)


__all__ = ["install_coverage_extensions"]
