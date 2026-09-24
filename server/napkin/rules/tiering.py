"""Source tiering by a domain policy.

  primary    regulators, official statistics, governments and intergovernmental
             bodies, company filings (registries, annual reports, investor pages)
  secondary  trade press and industry bodies
  tertiary   everything else

The tier comes from the source's URL alone — never from the model and never
from what the page says about itself. The policy is data (the tables below),
so it is reviewed as data.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

PRIMARY_SUFFIXES = (
    ".gov", ".gov.ie", ".gov.uk", ".gouv.fr", ".bund.de", ".gov.au", ".govt.nz", ".gc.ca",
    ".europa.eu", ".oireachtas.ie", ".police.uk", ".nhs.uk", ".hse.ie", ".int",
)
PRIMARY_DOMAINS = {
    # official statistics
    "cso.ie", "ons.gov.uk", "eurostat.ec.europa.eu", "destatis.de", "insee.fr", "census.gov",
    "oecd.org", "data.oecd.org", "imf.org", "worldbank.org", "iea.org",
    # regulators and state agencies (IE / GB / EU)
    "seai.ie", "cru.ie", "comreg.ie", "centralbank.ie", "ccpc.ie", "asai.ie", "adstandards.ie", "hpra.ie", "grai.ie",
    "rsa.ie", "ndls.ie", "revenue.ie", "citizensinformation.ie", "epa.ie", "bai.ie", "cnam.ie",
    "asa.org.uk", "cap.org.uk", "ofcom.org.uk", "ofgem.gov.uk", "fca.org.uk", "cma.gov.uk",
    "dvla.gov.uk", "gov.ie", "gov.uk", "europa.eu", "ec.europa.eu", "eea.europa.eu",
    # company filings and registries
    "sec.gov", "cro.ie", "companieshouse.gov.uk", "find-and-update.company-information.service.gov.uk",
    "annualreports.com",
}
# URL paths that mark a company's own filing (annual report, investor relations).
FILING_PATH = re.compile(r"/(investor[s-]?(relations)?|ir|annual[-_]?report[s]?|financial[-_]?report[s]?|"
                         r"quarterly[-_]?statement[s]?|results)(/|[-_.]|$)", re.I)
SECONDARY_DOMAINS = {
    # industry bodies
    "simi.ie", "smmt.co.uk", "acea.auto", "acea.be", "evireland.ie", "ibec.ie", "drinksireland.ie",
    "iapi.ie", "ipa.co.uk", "iabireland.ie", "iabuk.com", "thinkbox.tv", "bordbia.ie", "fooddrinkeurope.eu",
    "effie.org", "warc.com", "nielsen.com", "kantar.com", "mintel.com", "statista.com", "euromonitor.com",
    # trade and business press
    "marketing.ie", "adworld.ie", "irishadvertisingnews.ie", "campaignlive.co.uk", "campaignlive.com",
    "thedrum.com", "marketingweek.com", "adage.com", "adweek.com", "mediapost.com",
    "autonews.com", "autocar.co.uk", "autoexpress.co.uk", "motortrader.com", "am-online.com",
    "electrive.com", "insideevs.com", "just-auto.com", "carzone.ie", "motors.ie", "irishcar.ie",
    "businesspost.ie", "siliconrepublic.com", "thecurrency.news", "reuters.com", "bloomberg.com",
    "ft.com", "fleet.ie", "fleetnews.co.uk", "thegrocer.co.uk", "checkout.ie", "shelflife.ie",
}
TIERS = ("primary", "secondary", "tertiary")


def host_of(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def registrable(url: str) -> str:
    """A crude registrable domain, for counting independent sources."""
    h = host_of(url)
    parts = h.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "gov", "org", "ac", "com", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def _in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def tier_for(url: str) -> str:
    host = host_of(url)
    if not host:
        return "tertiary"
    if _in(host, PRIMARY_DOMAINS) or host.endswith(PRIMARY_SUFFIXES):
        return "primary"
    path = urlparse(url).path or ""
    if FILING_PATH.search(path):
        return "primary"
    if _in(host, SECONDARY_DOMAINS):
        return "secondary"
    return "tertiary"
