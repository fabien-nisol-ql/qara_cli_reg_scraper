"""qara-reg-scraper: a self-identifying scraper for medical-device
regulatory sources, across as many regulatory regimes as get implemented.

FDA is the first regulation implemented (eCFR Title 21 device regulations,
FDA guidance documents, 510(k)/De Novo clearances, recalls, and warning
letters) — see ``qara_reg_scraper.regulations`` for how sources are
registered per regulation and how to add another one (EU IVDR, Turkish or
Chinese device regs, ...). Every source identifies itself honestly on every
request (see ``qara_reg_scraper.http_client``) and does not attempt to
evade rate limiting, robots.txt, or bot detection.
"""

__version__ = "0.1.0"
