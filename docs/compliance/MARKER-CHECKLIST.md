# Compliance document markers — completion checklist

Generated from the templates and from `facts.OPERATOR_FIELDS`, so this list cannot drift
from the code that defines the markers. Regenerate after any change to a template.

**46 markers across 27 distinct fields.**

Work top to bottom. The first group blocks any customer at all; the rest unblock
specific markets. A marker left as `[[FILL: ...]]` renders as literal text a
customer reads — which is why the templates ship as templates, not as published
documents.

## Before any customer

| Marker | Appears in | Guidance |
|---|---|---|
| `abuse_contact` | Acceptable use policy | Where a suspected AUP breach is reported. |
| `backup_retention` | Data processing agreement (B2B) | How long backups are kept — they outlive a deletion, so the notice must say so. |
| `database_at_rest` | Data processing agreement (B2B) | The at-rest encryption mechanism, stated accurately. |
| `effective_date` | Privacy notice (visitor-facing), US state privacy notice, Data processing agreement (B2B), Terms of service (B2B) | The date this version of the documents takes effect. |
| `fees_term` | Terms of service (B2B) | Fee schedule, billing period and failed-payment handling. |
| `governing_law` | Data processing agreement (B2B), Terms of service (B2B) | Which country's law governs the contract, and which courts hear disputes. |
| `hosting_location` | Privacy notice (visitor-facing) | The region the servers physically run in. A privacy notice must not guess this. |
| `hosting_sub_processor` | Privacy notice (visitor-facing), Data processing agreement (B2B) | Your VPS/hosting provider's trading name — it is a sub-processor because it holds the database. |
| `liability` | Data processing agreement (B2B), Terms of service (B2B) | The liability cap and excluded loss categories. A commercial decision with an insurance consequence. |
| `platform_address` | Data processing agreement (B2B), Terms of service (B2B) | Your registered address, as it appears on filings. |
| `platform_legal_name` | Privacy notice (visitor-facing), Data processing agreement (B2B), Terms of service (B2B) | Your company's registered legal name. Appears in every commercial document. |
| `provider_terms_deepseek` | Privacy notice (visitor-facing), Data processing agreement (B2B) | DeepSeek's DPA/terms, retention statement and processing region. |
| `provider_terms_ollama_cloud` | Privacy notice (visitor-facing), Data processing agreement (B2B) | Ollama Cloud's DPA/terms, retention statement and processing region, recorded verbatim by reference. |
| `security_contact` | Acceptable use policy | Where a penetration-test authorisation or vulnerability report goes. |
| `support_channel` | Terms of service (B2B) | How a customer reaches support: address, portal, or queue. |
| `support_target` | Terms of service (B2B) | The response target you actually commit to. An aspiration stated as a number is a term you can breach. |
| `termination_term` | Terms of service (B2B) | Notice periods and the data-export window on termination. |

## Before first EU customer

| Marker | Appears in | Guidance |
|---|---|---|
| `breach_notice_window` | Data processing agreement (B2B) | How fast you notify the business of a personal-data breach. 24–48h is normal; 72h is the GDPR outer limit the controller faces. |
| `d3_position` | Data processing agreement (B2B) | The processor-chain description, if one is required instead of the DPA's own chain. |
| `legal_basis_review` | Privacy notice (visitor-facing) | Confirm the lawful basis for the target jurisdiction and sector. |

## Before first US customer

| Marker | Appears in | Guidance |
|---|---|---|
| `us_opt_in_mechanism` | US state privacy notice | How a tenant would grant explicit opt-in, if a training use is ever offered. Today there is none. |
| `us_request_channel` | US state privacy notice | Where a US consumer submits a privacy request. |
| `us_request_verification` | US state privacy notice | How a request is verified as genuine. |
| `us_state_scope` | US state privacy notice | Which US state laws apply and the revenue/volume basis for saying so. |

## Per customer

| Marker | Appears in | Guidance |
|---|---|---|
| `business_address` | Privacy notice (visitor-facing), Data processing agreement (B2B) | Filled by each business. |
| `business_contact` | Privacy notice (visitor-facing) ×3 | Filled by each business. |
| `business_legal_name` | Privacy notice (visitor-facing), Data processing agreement (B2B), AI output disclaimer (visitor-facing) ×4 | Filled by each business on its own copy — not by you. |

## How to verify you are done

```bash
cd backend/api && python -m app.compliance.render && python -m app.compliance.render_legal
grep -rn '\[\[FILL:' ../../docs/compliance/ | grep -v LEGAL-REVIEW | grep -v '\[\[FILL: \.\.\.\]\]'
```

The second command should print nothing. `[[FILL: ...]]` (the literal ellipsis) is the
template's own instruction line, not a field.

## What is not a marker, and still needed

- **A lawyer's review** of the completed text for your jurisdiction. The markers make
  the documents *complete*; they do not make them *reviewed*.
- **The sub-processor list as a versioned page.** GDPR Art. 28(2) wants a current,
  named list that customers can see change. The table inside the privacy notice is a
  snapshot; the live list should be a page with a change log.
- **Whether you are EU-established.** Decides whether Art. 27 (EU representative)
  applies and whether a representative's details must appear in the notice.

