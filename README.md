# Foreign Capital Gains PL Tax Support

> **PROPRIETARY SOFTWARE — NO LICENSE GRANTED**
>
> Copyright © 2026 Bartosz Paternoga. All rights reserved. No separate software license is granted to run, use, modify, distribute, sublicense, or create derivative works from the original software. Rights to view or fork this public repository are governed by GitHub’s Terms; any broader permission must be granted in writing by the copyright holder.
>
> **NO WARRANTY; LIMITATION OF LIABILITY.** The software and its reports are provided “AS IS,” without warranties of any kind, to the fullest extent permitted by applicable law. This is not tax or legal advice; independently verify every result before relying on it. To the fullest extent permitted by applicable law, the author disclaims liability for loss, damages, tax assessments, penalties, or claims arising from access to, use of, inability to use, or reliance on the software. Nothing in this notice excludes liability that cannot lawfully be excluded. Third-party components remain subject to their own licenses.

A Streamlit workspace for Polish foreign-capital-gains tax calculations and PIT-38 / PIT-ZG audit reports.

The intended scope is an IBKR Ireland calculation pack. It reports the IBKR contribution for manual consolidation with other broker documents; it is not a multi-broker return preparer.

## AI assistance

The app includes optional, question-based AI help at two points in the workflow:

- **Import and settings help:** Ask in plain language about detected import issues, column mappings, required settings, and how app features work. The assistant can use a detailed guide to the app together with the file format, transaction counts, issue codes, and selected safe settings to explain what needs review.
- **Calculation-results help:** Ask follow-up questions about the current calculation. The assistant receives selected aggregate PLN figures and review/reconciliation statuses, along with the same app guide, so it can explain what the results mean and which assumptions still need checking.
- **Interactive follow-up:** Each panel accepts typed questions and follow-ups; up to eight recent messages are included so the conversation can use its recent context. Chat history stays in the current Streamlit session.
- **User-controlled and read-only:** AI is optional, and the consent checkbox must be enabled before you can send a message. It does not run or modify tax calculations, change app settings, or mark review items resolved, and AI responses are not included in the PDF/CSV tax-office working papers. Treat its responses as explanations to check, not tax advice.

Configure `OPENROUTER_API_KEY` in the server environment or Streamlit Secrets to enable it; `OPENROUTER_MODEL` defaults to `openrouter/free`. A request sends the question, up to eight recent messages, the app guide, safe selected-setting indicators, and the relevant whitelisted import or results summary. The app does not attach the uploaded CSV, preview rows, transaction ledger, account identifiers, ticker names or mapping text, or entered unit quantities. Anything typed into a question is sent. OpenRouter may route requests to providers with their own data policies, so review those policies before using AI assistance.

## Look and feel

The app uses the same light slate theme, typography, component styling, and sidebar brand treatment as Investment Copilot.

## Login and user management

Both app.py and agent.py use the same PostgreSQL-backed, administrator-provisioned account flow as Investment Copilot. Set these values in Streamlit Secrets:

    DATABASE_URL = "postgresql://..."
    BOOTSTRAP_ADMIN_USERNAME = "admin"
    BOOTSTRAP_ADMIN_PASSWORD = "use-a-unique-random-password-of-at-least-12-characters"

Use the same DATABASE_URL as Investment Copilot to share its account registry. With a separate database, the first configured bootstrap administrator creates that app's account registry. Public self-registration is disabled. Administrators can create accounts, issue temporary passwords, reset passwords, and disable or reactivate accounts from the sidebar. New accounts must change their temporary password at first sign-in.

Uploaded CSVs and calculation results remain in the current Streamlit session; the app does not save a user's tax records to PostgreSQL.

## Local development

For single-user local development without a PostgreSQL database:

    ETF_APP_LOCAL_DEV_MODE=1 streamlit run app.py

This local-only mode bypasses login. The app also accepts FOREIGN_TAX_LOCAL_DEV_MODE as an alias. To test account login locally, provide a PostgreSQL DATABASE_URL and the bootstrap administrator secrets instead. Do not deploy with either local-development flag enabled.

Install dependencies with:

    python -m pip install -r requirements.txt

## Import and calculation safeguards

- The Import Check runs before calculation. It shows the detected IBKR table, selected column mapping, transaction-type counts, sample source rows, and explicit FX/cash rows excluded from PIT-38. If a header is unfamiliar or ambiguous, select its meaning manually; calculation stays disabled while required columns or invalid dates are unresolved. Unrecognized security activity requires an explicit review acknowledgement and remains excluded/flagged.
- The importer accepts IBKR transaction CSV layouts; it is not a universal parser for every broker or file type. Add and test a provider-specific adapter before using a different broker's export.
- Upload the original IBKR transaction-history report CSV. The importer finds the embedded Transaction History table after the report preamble, reads it directly, and keeps the original CSV row references; no column rearranging or editing is needed. The existing normalized transaction CSV format is still supported.
- An optional blank CSV upload template is available beside the upload control for manually prepared transaction files. Keep its headers and enter one transaction per row; original IBKR exports can still be uploaded directly.
- Explicit IBKR `Forex Trade Component` currency-pair rows, `FX Translations P&L` adjustments, deposits, and withdrawals are left out of PIT-38 security and investment-income calculations and listed in the excluded-activity audit schedule. Excluding a row from this PIT-38 calculation does not decide whether the underlying currency exchange has a separate tax treatment; review that separately before filing.
- The importer processes the selected CSV only. It does not substitute another file when the requested file is missing.
- Buy and sell directions prefer IBKR's Transaction Type field. Transactions are ordered by date, and a sale exceeding the available FIFO holdings stops the run.
- The Account Reconciliation tab tracks opening units, buys, sells, incoming/outgoing transfers, reported corporate-action unit changes, calculated closing units, IBKR closing units, and the difference by ticker. Enter opening and IBKR closing quantities in the sidebar as `SYMBOL=quantity`; quantities may be fractional. If the upload starts from a verified zero position, confirm the zero-opening checkbox.
- IBKR `REC`, `DEL`, and `CORP` records are included in unit reconciliation using their reported quantities. Their tax-basis treatment is not inferred. Transfers and corporate actions remain review items, and a sale without the acquired lot's basis stops FIFO tax calculation.
- A reconciliation mismatch is calculated closing units minus IBKR-reported closing units. If the opening quantity, closing quantity, or an event quantity is missing, the status says what is needed and does not claim a match.
- IBKR cancel/correction records stop tax calculations until matched to the original trade. Cash dividends and interest accrual rows are distinguished; accruals are not automatically treated as paid income.
- Cash rows whose currency is absent need the account currency. The cash-account country applies to interest and its withholding; dividends and security-related withholding use the mapped security source country. Missing country attribution is marked for review.
- NBP Table A rates must be fetched successfully. The calculation records the API's effective date and stops if a rate cannot be verified; there is no estimated-rate fallback.
- Interest and dividends are calculated separately from security disposals. Foreign tax credits are capped at the Polish tax for the associated passive income; tax paid and the uncredited amount are shown separately.
- A prior-year loss is an amount entered by the user. Marking it verified means the user has checked its source, five-year period, and annual limit; the app does not validate those historical facts.
- Unsupported rows, unresolved countries, unverified prior losses, and calculation errors must be reviewed. A clean import status only means the supported rows passed the app's checks; it does not mean the return is filing-ready. Exports are working papers and do not constitute a completed tax return.
- The displayed tax due combines only PIT-38 positions 35 and 49 from supported imported records. It is not position 51: positions 45, 46, and 50 are outside the app's calculation and must be considered separately.
- For manual consolidation, use IBKR proceeds/costs as the contribution to positions 22/23. Add domestic broker PIT-8C figures in positions 20/21, then recalculate the combined positions 26–35. Combine foreign passive income and withholding from all sources before finalizing positions 47–49.
- Confirm each security's Polish tax classification and source-country treatment against the instrument documents and applicable rules before relying on a PIT/ZG summary.
- PDF and CSV working papers include transaction math with NBP rate/effective date, the passive-tax credit cap, account-unit events and reconciliation, and unresolved review notes.
- The Polish tax-office support ZIP adds a print-ready PDF dossier with a full transaction ledger and NBP rate appendix, plus matching CSV schedules. It includes an IBKR source-file SHA-256 fingerprint, source-record references, PIT-38/PIT-ZG contribution tables, unit reconciliation, and translated review notes. Including the original IBKR CSV is optional.
- The app and exports include a PIT/ZG(8) form crosswalk per country: positions 6 (full country name), 7 (two-letter country code), 29 (Art. 30b(5a)/(5b) income), and 30 (foreign tax paid on position 29 income before the credit limit). The figures are IBKR-only; confirm source-country treatment and combine other sources for each country. Taxpayer details in positions 1–5 must be entered manually. Positions 31–32 for virtual-currency income are outside the app's scope. Art. 30a interest/dividends are supporting detail for PIT-38 Section G positions 47–49, not PIT/ZG fields.
- The complete print-ready dossier PDF is also available as a standalone download, alongside the summary and detailed CSV files.
- The dossier is a supporting working paper, not an official PIT-38/PIT-ZG form or a replacement for IBKR statements, trade confirmations, tax vouchers, position snapshots, or corporate-action/transfer records. Keep the source documents and provide them if requested.

## Regression tests

Run the offline parser, FIFO, multi-currency/year, reconciliation, export, rounding, rate-source, and tax-credit checks with:

    python -m unittest discover -s tests -v

The tests mock NBP responses and do not need network access.
