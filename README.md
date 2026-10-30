# Foreign Capital Gains PL Tax Support

> **PROPRIETARY SOFTWARE — NO LICENSE GRANTED**
>
> Copyright © 2026 Bartosz Paternoga. All rights reserved. No separate software license is granted to run, use, modify, distribute, sublicense, or create derivative works from the original software. Rights to view or fork this public repository are governed by GitHub’s Terms; any broader permission must be granted in writing by the copyright holder.
>
> **NO WARRANTY; LIMITATION OF LIABILITY.** The software and its reports are provided “AS IS,” without warranties of any kind, to the fullest extent permitted by applicable law. This is not tax or legal advice; independently verify every result before relying on it. To the fullest extent permitted by applicable law, the author disclaims liability for loss, damages, tax assessments, penalties, or claims arising from access to, use of, inability to use, or reliance on the software. Nothing in this notice excludes liability that cannot lawfully be excluded. Third-party components remain subject to their own licenses.

An IBKR-focused calculation and evidence workspace for preparing the Interactive Brokers contribution to Polish PIT-38 and PIT/ZG working papers.

It is designed for manual consolidation: calculate and document the IBKR activity here, then combine it with Polish broker statements and other tax records in your final return. It is not a multi-broker tax-return preparer or filing service.

## How the AI fits

**The calculation engine calculates; the AI explains.** Tax figures are produced by the application’s rule-based calculation code. The optional OpenRouter assistant cannot run, edit, approve, or change those calculations.

The assistant supports questions in two parts of the workflow:

- **Before calculation:** explain import checks, column mappings, configuration settings, and app behavior. It receives the detected file format, transaction-type counts, issue codes, safe setting indicators, and a detailed guide to the app.
- **After calculation:** explain selected aggregate PLN figures, calculation status, and review or reconciliation categories in plain language. You can ask follow-up questions; up to eight recent chat messages are sent for context.

Each panel is gated by a consent checkbox. A request sends your question, recent messages, the app guide, limited settings, and the relevant summary. The app does not attach the original CSV, preview rows, transaction ledger, account identifiers, ticker names, country-mapping text, or entered unit quantities. **Anything you type into a question is sent.** OpenRouter may route requests to other providers; check their data policies before using the assistant. AI answers can be wrong, are not tax advice, and are not included in exported working papers.

To enable AI, set OPENROUTER_API_KEY in the server environment or Streamlit Secrets. OPENROUTER_MODEL is optional and defaults to openrouter/free.

## What the calculation and reports cover

- Reads original IBKR transaction-history CSV exports, including the embedded transaction table after the report preamble, and keeps source-row references. A normalized transaction CSV is also supported. Review the detected columns and any import issues before calculating.
- Builds FIFO inventory from imported buys and matches sales to the oldest available lots for the same symbol. The imported history must contain the acquisition basis for sold units; a sale without enough FIFO inventory stops the calculation.
- Converts supported foreign-currency amounts using NBP Table A rates effective before the transaction date. The calculation records the rate, effective date, table number, and source reference. It stops rather than inventing a rate when NBP cannot verify one.
- Separates security disposals from interest, dividends, and withholding. Cash currency and source-country settings are explicit inputs when the statement does not establish them.
- Reconciles opening units, buys, sells, transfers, reported corporate-action changes, and closing units against IBKR snapshots. A unit match checks quantities only; it does not prove the tax basis or classification.
- Lists excluded IBKR activity, including FX components, FX translation adjustments, deposits, and withdrawals. Their exclusion from this securities and investment-income calculation does not determine the separate tax treatment of any currency-exchange gain.
- Flags unresolved countries, prior-year loss assumptions, corporate actions, transfers, unsupported activity, and reconciliation gaps for review. A completed calculation is not automatically filing-ready.

The downloadable support package contains a Polish dossier PDF, summary CSV, transaction-level ledger CSV, NBP rate schedule, excluded-activity schedule, and a source-file SHA-256 fingerprint. The original source CSV can optionally be included in the ZIP. Keep it and the supporting broker records: the working papers do not replace statements, trade confirmations, withholding certificates, position snapshots, or transfer and corporate-action documents.

## Form mapping and limits

The reports show IBKR-only contributions. In the currently coded PIT/ZG(8) crosswalk, the country table identifies positions 6 and 7 and candidate figures for positions 29 and 30. Position 30 is foreign tax paid on position 29 income before the form’s credit limit. Enter the full country name for position 6 and verify the target-year form and source-country treatment before filing. The form version and instructions may change by tax year.

Interest and dividends are supporting detail for PIT-38 Section G; they are not PIT/ZG positions 29–30. The app does not calculate PIT/ZG positions 31–32 for virtual-currency income.

The app’s displayed position 35 and position 49 figures are IBKR-only estimates. They do not calculate final PIT-38 position 51 or incorporate other brokers, other income, or all return-level credit limits. Add the other sources and recalculate the combined return. For capital-gains foreign tax, position 34 and the amount ultimately deductible must be checked against the proportional limit using the complete Polish and foreign income.

Security classifications and source countries are not inferred from an IBKR account, ticker, or exchange. Verify each instrument and country from relevant documents and applicable rules. Transfers and corporate actions can affect unit reconciliation, but the app does not infer their tax-basis treatment. A transferred position without its original basis must be resolved before relying on a later sale.

## Account access and data

Hosted access is provisioned by an administrator; public self-registration is disabled. Using the same PostgreSQL DATABASE_URL as Investment Copilot shares its administrator and user registry. PostgreSQL stores account-management records, not uploaded tax files or calculation results; those remain in the active Streamlit session.

For hosted use, configure DATABASE_URL, BOOTSTRAP_ADMIN_USERNAME, and BOOTSTRAP_ADMIN_PASSWORD in Streamlit Secrets. Administrators create and manage user accounts; users must change temporary passwords at first sign-in.

## Run locally

Install dependencies:

    python -m pip install -r requirements.txt

Start a single-user local session without a database:

    ETF_APP_LOCAL_DEV_MODE=1 streamlit run app.py

This development flag bypasses login and must not be enabled in a hosted deployment. FOREIGN_TAX_LOCAL_DEV_MODE is also accepted as an alias. To test the hosted login flow locally, provide a PostgreSQL DATABASE_URL and bootstrap administrator credentials instead.

## Regression checks

Run the offline parser, FIFO, multi-year and multi-currency, account-reconciliation, export, rounding, NBP-source, and tax-credit tests:

    python -m unittest discover -s tests -v

The regression suite mocks NBP responses; it checks application behavior, not current tax advice or the legal treatment of a specific instrument.
