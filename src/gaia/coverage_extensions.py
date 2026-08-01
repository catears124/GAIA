from __future__ import annotations

from . import career_surface_collector as career

_EXTRA_CAREER_PATHS = (
    "/internships",
    "/students",
    "/student-opportunities",
    "/student-careers",
    "/graduates",
    "/graduate-programs",
    "/campus",
    "/campus-recruiting",
    "/early-careers",
    "/early-talent",
    "/emerging-talent",
    "/university",
    "/university-recruiting",
    "/university-programs",
    "/careers/search",
    "/careers/jobs",
    "/jobs/search",
    "/job-search",
    "/open-roles",
    "/open-positions",
    "/vacancies",
)
_EXTRA_CAREER_MARKERS = (
    "internship",
    "internships",
    "student-career",
    "student-opportunit",
    "graduate-program",
    "campus-recruit",
    "early-career",
    "early-talent",
    "emerging-talent",
    "university-recruit",
    "university-program",
    "job-search",
    "search-jobs",
    "open-roles",
    "open-positions",
)
_EXTRA_DETAIL_MARKERS = (
    "/job-detail/",
    "/job-details/",
    "/posting/",
    "/postings/",
    "/position/",
    "/positions/",
    "/vacancy/",
    "/vacancies/",
    "/role/",
    "/roles/",
    "/opportunity/",
    "/opportunities/",
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
    "workable.com": "workable",
    "recruitee.com": "recruitee",
    "jobvite.com": "jobvite",
    "icims.com": "icims",
    "oraclecloud.com": "oracle-recruiting",
    "successfactors.com": "successfactors",
    "smartsearchonline.com": "smartsearch",
    "catsone.com": "cats",
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
