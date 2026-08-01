from __future__ import annotations

from . import career_surface_collector as career

_ADDITIONAL_PROVIDER_HOSTS = {
    "clearcompany.com": "clearcompany",
    "hirebridge.com": "hirebridge",
    "silkroad.com": "silkroad",
    "silkroad-recruiting.com": "silkroad",
    "peoplefluent.com": "peoplefluent",
    "talentreef.com": "talentreef",
    "neogov.com": "neogov",
    "governmentjobs.com": "neogov",
    "jobappnetwork.com": "jobapp-network",
    "careerplug.com": "careerplug",
}


def install_provider_expansion() -> None:
    """Register tenant-scoped ATS surfaces without marking them validated."""
    career.PROVIDER_HOST_FRAGMENTS.update(_ADDITIONAL_PROVIDER_HOSTS)


__all__ = ["install_provider_expansion"]
