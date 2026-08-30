"""Premarket Approval (PMA) decisions — openFDA `device/pma` endpoint,
plus the actual FDA approval order letter for each decision.

https://open.fda.gov/apis/device/pma/

Mirrors clearances_510k.py closely — same shape, same reasoning — since
PMA decisions are the PMA pathway's exact counterpart to 510(k)
clearances: an ever-growing activity stream (56,995+ records and
counting, confirmed live), not a stable reference table, so this walks a
`decision_date` lookback window (default `lookback_days`=30) on every
run rather than re-walking the full historical archive daily.

Two documents per decision, same reasoning as clearances_510k.py: openFDA's
record is *metadata about* the decision (applicant, device name, decision
date, ...), not the regulatory document itself. The actual approval
order — FDA's own decision letter, the part a reviewer would actually want
to read — lives at a predictable accessdata.fda.gov URL (confirmed live,
same convention as 510(k) summary PDFs and HDE approval orders — see
hde.py's module docstring for the cross-source pattern):

    documents/<pma_number><supplement_number>/metadata/current.json
    documents/<pma_number><supplement_number>/order/current.pdf

`supplement_number` is empty for an original approval, e.g. "S014" for a
supplement — both share the same PMA number's own embedded year digits for
the URL's `pdf{yy}/` folder (a supplement's own decision_date can be years
after the original approval; the URL folder still keys off the *original*
PMA number's digits, not the supplement's decision year — confirmed live:
P160035's supplement S006, decided 2020, still resolves under `pdf16/`).

Unlike clearances_510k.py's `statement_or_summary` field, openFDA's PMA
records carry no equivalent "does an order letter exist" signal — so,
unlike clearances_510k, this can't skip attempting one in advance.
Confirmed live, though: roughly *half* of all supplement records have no
order letter posted at all (only originals and some major/panel-track
supplements do — routine 30-day-notice/real-time-process supplements
mostly don't) — a much higher, material miss rate than 510(k)'s own rare
edge case (a "Summary"-type k-number whose PDF still 404s despite the
field predicting otherwise). At that rate, treating every miss as a bare
`record_error` — as `fetch_and_save` does, and as 510(k)'s own rare-case
misses do — would mean re-attempting roughly half the entire lookback
window's decisions, forever, on every single run (`already_have` only
ever recognizes a `save_document` call, and `record_error` never makes
one). So this deliberately deviates from both: `_fetch_order` hand-rolls
the fetch (same bot-block-aware shape as clearances_510k's own
`_fetch_summary_pdf`) and persists a `not_applicable` sentinel via
`save_document` on a routine (non-block) miss — discovered empirically
per decision, rather than predicted from a field, but landing on the
same "settled, don't re-attempt" state either way.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from ...base_scraper import BaseScraper, BotBlockDetected, BudgetExhausted, HardStop, PreviewInfo
from ...http_client import looks_like_bot_block
from ...logging_setup import log_extra
from ...manifest import RunSummary
from .openfda_common import iter_openfda_results

ENDPOINT = "https://api.fda.gov/device/pma.json"
# Built-in fallback when neither --lookback-days nor
# regulations.fda.sources.pma.lookback_days sets one — see
# BaseScraper.lookback_days / effective_lookback_days below.
DEFAULT_LOOKBACK_DAYS = 30
# Where the approval order letters actually live — see
# clearances_510k.py's own ACCESSDATA_ORIGIN comment.
ACCESSDATA_ORIGIN = "https://www.accessdata.fda.gov"


class PmaScraper(BaseScraper):
    regulation = "fda"
    name = "pma"
    description = "FDA Premarket Approval (PMA) decisions (openFDA device/pma) + approval order letters"
    label = "PMA Decisions"

    @property
    def effective_lookback_days(self) -> int:
        return self.lookback_days if self.lookback_days is not None else DEFAULT_LOOKBACK_DAYS

    def run(self) -> RunSummary:
        end = datetime.now(UTC).date()
        start = end - timedelta(days=self.effective_lookback_days)
        # See recalls.py's comment - a literal space, not "+".
        search = f"decision_date:[{start:%Y-%m-%d} TO {end:%Y-%m-%d}]"
        self.log.info(f"pma: fetching decisions {start} .. {end}")

        try:
            for record in iter_openfda_results(self.http, ENDPOINT, search, sort="decision_date:desc"):
                decision_id = _decision_id(record)
                if not self.already_have(f"{decision_id}/metadata"):
                    self._save_metadata(decision_id, record)

                if self.already_have(f"{decision_id}/order"):
                    self.manifest.summary.skipped_already_known += 1
                    continue
                try:
                    self._fetch_order(decision_id, record)
                except BudgetExhausted:
                    self.log.info(f"pma: budget reached ({self.max_new_documents} new order letters)")
                    self.manifest.summary.stop_reason = "budget_reached"
                    return self.manifest.finalize()
                except HardStop as e:
                    self.log.warning(f"pma: stopping run early: {e}")
                    self.manifest.summary.stop_reason = "bot_block" if isinstance(e, BotBlockDetected) else "hard_stop"
                    return self.manifest.finalize()
            self.manifest.summary.stop_reason = "completed"
        except (BudgetExhausted, HardStop):
            raise  # programming error if this fires — the inner try should have caught it
        except Exception as e:  # noqa: BLE001 - the listing call itself failed after retries
            self.log.warning(f"pma: listing call failed, stopping run early: {e}")
            self.manifest.record_error("__listing__", url=ENDPOINT, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"

        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # Full pagination of the lookback window, but no PDF fetches - same
        # reasoning as clearances_510k.py's own estimate().
        end = datetime.now(UTC).date()
        start = end - timedelta(days=self.effective_lookback_days)
        search = f"decision_date:[{start:%Y-%m-%d} TO {end:%Y-%m-%d}]"
        try:
            decision_ids = [
                _decision_id(record)
                for record in iter_openfda_results(self.http, ENDPOINT, search, sort="decision_date:desc")
            ]
        except Exception as e:  # noqa: BLE001
            return PreviewInfo(total_available=None, already_known=None, note=f"could not query openFDA: {e}")
        # "Already known" means the order letter (the actually
        # network-costly part) is already captured — not just the free
        # metadata, same convention as clearances_510k.py.
        known = sum(1 for decision_id in decision_ids if self.already_have(f"{decision_id}/order"))
        return PreviewInfo(
            total_available=len(decision_ids), already_known=known,
            note=f"counts decisions made in the last {self.effective_lookback_days} days only",
            next_available_at=self.http.next_available_at(ACCESSDATA_ORIGIN),
            next_available_note=self.http.visiting_hours_description(ACCESSDATA_ORIGIN),
        )

    def _save_metadata(self, decision_id: str, record: dict) -> None:
        content = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
        self.manifest.save_document(
            f"{decision_id}/metadata",
            content,
            url=f"{ENDPOINT}?search=pma_number:{record.get('pma_number')}",
            title=record.get("trade_name") or record.get("generic_name"),
            ext="json",
            content_type="application/json",
            http_status=200,
            source_metadata={
                "pma_number": record.get("pma_number"),
                "supplement_number": record.get("supplement_number"),
                "applicant": record.get("applicant"),
                "product_code": record.get("product_code"),
                "decision_date": record.get("decision_date"),
                "decision_code": record.get("decision_code"),
                "supplement_type": record.get("supplement_type"),
                "supplement_reason": record.get("supplement_reason"),
                "advisory_committee": record.get("advisory_committee"),
            },
        )

    def _fetch_order(self, decision_id: str, record: dict) -> None:
        """Raises BudgetExhausted (once the cap is hit) or HardStop (only
        for a suspected bot-management block or a network/retry failure —
        see BaseScraper.fetch_and_save's own docstring for why those two
        specifically stop the whole run). A routine 4xx/5xx for one
        specific decision (a denial, a routine supplement with no order
        letter posted, a pre-URL-convention record, ...) is persisted as
        a not_applicable sentinel — not just a record_error — so
        already_have() recognizes it and future runs don't blindly
        re-attempt the same settled miss forever. See the module
        docstring for why that matters so much more here than in
        clearances_510k.py's own equivalent case."""
        document_id = f"{decision_id}/order"
        pma_number = record.get("pma_number") or ""
        year_digits = pma_number[1:3] if len(pma_number) >= 3 else None
        if not year_digits or not year_digits.isdigit():
            # A data oddity with this one record, not a settled "no
            # document exists" fact — NOT persisted as not_applicable, so
            # a fixed/corrected upstream record gets a fresh chance on the
            # next run instead of being stuck on a bad first guess forever.
            self.manifest.record_error(
                document_id, url=None,
                error=f"cannot derive PDF path from pma_number {pma_number!r}",
            )
            return

        if self._budget_exhausted():
            raise BudgetExhausted()

        url = f"https://www.accessdata.fda.gov/cdrh_docs/pdf{year_digits}/{decision_id}A.pdf"
        try:
            response = self.http.get(url)
        except Exception as e:
            log_extra(
                self.log, logging.WARNING, "order_letter_fetch_failed",
                decision_id=decision_id, url=url, error=str(e),
            )
            self.manifest.record_error(document_id, url=url, error=str(e))
            raise HardStop(str(e)) from e

        # Checked regardless of status code, before deciding whether a
        # non-PDF response is a block (hard stop) or a routine miss (a
        # settled not_applicable) — same reasoning as
        # clearances_510k.py's own maintenance note: Akamai's apology
        # page has been observed served with a 404 status, not just
        # 200/403.
        if looks_like_bot_block(response):
            content_type = response.headers.get("Content-Type", "")
            error = f"likely bot-management block (status={response.status_code}, content-type={content_type!r})"
            log_extra(
                self.log, logging.WARNING, "bot_detection_suspected",
                decision_id=decision_id, url=url, status=response.status_code, content_type=content_type,
            )
            self.manifest.record_error(document_id, url=url, error=error)
            raise BotBlockDetected(error)

        content_type = response.headers.get("Content-Type", "")
        if response.status_code != 200 or "pdf" not in content_type.lower():
            log_extra(
                self.log, logging.INFO, "order_letter_not_found",
                decision_id=decision_id, url=url, status=response.status_code, content_type=content_type,
            )
            self.manifest.save_document(
                document_id,
                b'{"not_applicable": true}',
                url=url,
                title=f"PMA Approval Order — not applicable ({decision_id})",
                ext="json",
                content_type="application/json",
                http_status=200,
                source_metadata={
                    "pma_number": pma_number,
                    "supplement_number": record.get("supplement_number"),
                    "not_applicable": True,
                    "reason": f"no order letter found (status={response.status_code})",
                },
            )
            return

        self.manifest.save_document(
            document_id,
            response.content,
            url=url,
            title=f"PMA Approval Order — {record.get('trade_name', decision_id)}",
            ext="pdf",
            content_type="application/pdf",
            http_status=response.status_code,
            source_metadata={"pma_number": pma_number, "supplement_number": record.get("supplement_number")},
        )
        self._consume_budget()


def _decision_id(record: dict) -> str:
    """The natural per-decision key: the PMA number, plus its supplement
    number when this record IS a supplement (empty for an original
    approval) - matches the accessdata.fda.gov PDF-path convention
    exactly, so this id doubles as the URL-derivable identifier."""
    return f"{record.get('pma_number', 'unknown')}{record.get('supplement_number') or ''}"
