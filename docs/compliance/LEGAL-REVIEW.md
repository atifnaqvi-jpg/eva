# Legal readiness review — EVA AI customer-support platform

**Reviewer:** Talos (engineering agent), on instruction from Atif Naqvi
**Date:** 2026-11-20
**Scope:** the three templates in `docs/compliance/` against current EU/UK/US law, for a
product that will be sold to businesses in multiple jurisdictions
**Status:** engineering review, not legal advice. Items marked **MUST** are places where
the current text is incomplete or risks being wrong. Items marked **RECOMMENDED** are
industry practice that materially reduces risk.

---

## 1. Executive summary

The existing templates are unusually good: they are **generated from the code**, a test
fails if a factual statement drifts, and they state limits rather than hiding them
(`§7` "One limit, stated rather than hidden"). That is better than most commercial
privacy notices.

**Three gaps matter, in this order:**

1. **The EU AI Act Article 50 transparency duty is not referenced anywhere.** It applied
   from **2 August 2026** — i.e. it is live now — and it applies directly to a chatbot.
   The templates describe the AI disclosure in prose (`§9` "The assistant is an AI system
   and says so in the chat window") but never cite the obligation, never state the
   "unless obvious" test, and never address the provider/deployer split that determines
   *who* owes the duty. **MUST fix.**
2. **The sub-processor table has three of four rows unresolved** (`[[FILL:
   provider_terms_ollama_cloud]]`, `deepseek`, `hosting_sub_processor`,
   `hosting_location`). A processor list is the one part of a privacy notice that cannot
   be a placeholder at launch — GDPR Art. 28(2) requires the controller to authorise
   named sub-processors in advance. **MUST fix before any EU customer.**
3. **No Terms of Service / Acceptable Use / AI-specific disclaimer exists at all.** There
   is a privacy notice, a DPA and a US notice — the documents that govern *liability*
   between you and your business customers are absent. **MUST fix before commercial sale.**

---

## 2. Research findings (with sources)

### 2.1 EU AI Act — Article 50 transparency

Source: European Commission, *Transparency obligations under Article 50 of the AI Act*
(FAQ, digital-strategy.ec.europa.eu); AI Act Service Desk Art. 50; Commission Guidelines
C/2026/6005.

Verified facts:

- **Applies from 2 August 2026.** A limited grace period to **2 December 2026** exists
  only for systems placed on the market *before* 2 Aug 2026, and only for the
  machine-readable **marking** duty (Art. 50(2)) — not for the disclosure duty.
- **Who owes what:**
  - **Provider** (Art. 50(1)): must design the system so people are *informed they are
    interacting with an AI system*, unless that is obvious. Four cumulative criteria:
    it is an AI system; a genuine two-way exchange (not merely automated responses);
    direct interaction; with natural persons.
  - **Provider** (Art. 50(2)): generative outputs must carry **machine-readable marks**
    detectable as AI-generated.
  - **Deployer** (Art. 50(4)): must label AI-generated text **published to inform the
    public on matters of public interest**. Human review or editorial control exempts it —
    and "superficial, solely formal or procedural checks (e.g. spell-checking)" do **not**
    count.
- **Notification must occur "from the start of the first interaction"**, in a clear and
  distinguishable manner, and must meet accessibility requirements.
- A **Code of Practice on Transparency of AI-Generated Content** exists and has been
  assessed as adequate by the Commission and the AI Board; signatories get legal
  certainty. Non-signatories must demonstrate compliance by other adequate means.

**What this means for EVA specifically:**

| Obligation | Who | Current state |
|---|---|---|
| Tell the person they're talking to AI | Provider — **that is you**, for the platform; the business is a deployer | Implemented in the widget (`SEC-14`), described in `§9`, but **not attributed to Art. 50** and the "unless obvious" exception is not addressed |
| Machine-readable marking of generated output | Provider | **Not implemented.** The answers are plain text. Arguably in scope for Art. 50(2) |
| Label AI text on matters of public interest | Deployer (the business) | Not applicable to customer-support answers; should be **stated as out of scope with the reason** |
| Accessibility of the disclosure | Provider | The widget claims WCAG 2.1 AA (`WIDG-11`); the disclosure line itself should be named in that claim |

**The "unless obvious" exception should not be relied on.** The Commission says it must be
interpreted **restrictively** because it deprives people of transparency. For a chat
window styled to look like a support agent, "obvious" is a weak argument.

**The Art. 50(2) marking question is genuinely uncertain and I will not guess it.** The
guidelines exclude "a short sequence of numbers, symbols or letters" and *intend* outputs
consumed by humans rather than machines; a customer-support answer is neither clearly in
nor clearly out. **Recommendation: record the position explicitly in the privacy notice —
either "answers carry no machine-readable AI mark because [reason]" or implement C2PA-style
provenance.** Silence is the one option that looks like an oversight at audit.

### 2.2 California ADMT rules — do they apply?

Source: CCPA regulations text (privacy.ca.gov); National Law Review, "Deadline Approaches
for Complying with California Rules on Automated Decision-making Technology"
(19 Nov 2026), citing CCR §7200/§7220.

Verified facts:

- ADMT obligations take effect **1 January 2027**.
- They bite where ADMT is used for **significant decisions** about a consumer's
  **employment, finances, healthcare, housing or education**.
- Obligations: **pre-use notice** (specific description, purpose, data used, opt-out
  route, human role, accessible), **opt-out right** (with narrow exceptions), **access
  right**, and an **explanation** of the specific output when a significant decision is
  made.

**Assessment for EVA: out of scope, and this should be stated rather than assumed.** A
customer-support assistant answering "what are your hours" is not making a significant
decision about anyone's employment, credit, health, housing or education. **But** the
moment a business connects EVA to a system that *does* — eligibility, credit, hiring,
tenancy — the deployer becomes subject to ADMT, and some of that becomes your problem as
the vendor whose product made it easy.

**Recommendation:** state in the US notice that EVA is **not designed or permitted for
ADMT purposes**, and put the same sentence in the Terms of Service as a **permitted-use
restriction**. That converts an assumption into a contractual term, which is the
difference between "we think we're fine" and "we told you not to do that".

### 2.3 GDPR / UK GDPR — what the existing text gets right and what it misses

Gets right: lawful bases per purpose; retention with a real number (90 days, 1–730
range) and the actual scheduler time; the data-subject rights list; the honest statement
that audit records survive erasure; the Art. 13(1)(c) recipient table.

**Missing or incomplete:**

1. **No international transfer mechanism is named.** `§6` says a provider "may process a
   prompt outside the EU; where it does, the transfer is covered by that provider's own
   safeguards". GDPR Chapter V requires *identifying* the mechanism — an adequacy
   decision, or standard contractual clauses plus a transfer impact assessment. "Their own
   safeguards" is not a mechanism, and a supervisory authority will ask which one.
   **MUST.**
2. **No DPO / representative contact.** If you are not established in the EU but offer
   services to EU data subjects, Art. 27 requires an **EU representative**. Whether you
   need one depends on your establishment — but the notice must say who to contact either
   way.
3. **No statement on automated decision-making under Art. 22.** The bot declines rather
   than decides, and cannot take actions — that is the right answer, but it should be
   *stated* ("no decision with legal or similarly significant effects is made about you"),
   because its absence reads as an oversight.
4. **The `[[FILL: legal_basis_review]]` marker is doing too much work.** It asks the
   operator to "confirm the basis for your own jurisdiction and sector" — but under GDPR
   the *controller* (the business) chooses the basis, and it is the same analysis for
   every EU customer. That should be settled once in the template, not re-litigated per
   customer.
5. **Children.** No statement that the assistant is not directed at children. For an SMB
   chatbot, low risk, but it is a standard field and its absence is noticed.
6. **Complaint route is generic.** "You may complain to a supervisory authority in the
   country where you live or work" is correct but thin; naming the lead authority (or the
   EDPB's one-stop-shop explanation) is better practice.

### 2.4 Liability documents that do not exist

There is **no Terms of Service, no Acceptable Use Policy, and no AI-output disclaimer.**
For a B2B SaaS product these carry more commercial risk than the privacy notice:

- **Limitation of liability and the AI-output warranty question.** A customer will ask
  "what if it gives wrong advice and I lose a customer?" The answer has to be a
  contractual term, not a hope.
- **No-warranty on generated answers.** The bot is designed to say "I don't know" rather
  than guess, which is a genuine safety property worth stating — but the software still
  cannot warrant that every answer is correct.
- **Permitted-use restrictions.** No ADMT use; no regulated advice (medical, legal,
  financial); no processing of special-category data; no use as the sole channel for
  legally-required notices.
- **Indemnity and IP.** Who owns the conversation data; what happens to uploaded documents
  at termination; whether the business grants you a licence to process its content.
- **Service levels and the honest caveat** — p90 28.6 s at 50 concurrent is documented in
  the plan; an SLA should reflect measured behaviour, not aspiration.

---

## 3. What I recommend changing, by document

### 3.1 Privacy notice (`privacy-notice-template.md`)

| # | Change | Priority |
|---|---|---|
| P1 | Add a section **"AI systems and transparency (EU AI Act Art. 50)"**: you are the AI provider, the business is the deployer; the disclosure appears at first interaction; the "unless obvious" exception is not relied on; state the position on machine-readable marking | **MUST** |
| P2 | Name the **international transfer mechanism** per provider (adequacy decision / SCCs + TIA) | **MUST** |
| P3 | Complete the sub-processor table: hosting provider, region, and each LLM provider's terms | **MUST** |
| P4 | Add an explicit **"no automated decision-making with legal or similarly significant effects"** statement (Art. 22) | RECOMMENDED |
| P5 | Add **children** statement | RECOMMENDED |
| P6 | Add **EU representative** contact if not EU-established (Art. 27) | RECOMMENDED |
| P7 | Resolve `[[FILL: legal_basis_review]]` once in the template, not per customer | RECOMMENDED |
| P8 | Name the disclosure line in the accessibility claim | RECOMMENDED |

### 3.2 DPA (`dpa-skeleton.md`)

| # | Change | Priority |
|---|---|---|
| D1 | Name the **transfer mechanism** and attach SCCs by reference | **MUST** |
| D2 | Add an **Art. 28(3)(h) audit** clause that is realistic (a questionnaire plus a shared report, not an on-site right you cannot staff) | RECOMMENDED |
| D3 | State **breach notification window** concretely (the marker is still `[[FILL: breach_notice_window]]`; 72 hours is the GDPR outer limit, but your obligation to the controller should be shorter — 24–48 h is normal practice) | **MUST** |
| D4 | Add **sub-processor change notification** (advance notice + right to object), which Art. 28(2) expects | **MUST** |
| D5 | State what happens to data **on termination** — export window, then deletion, with the audit-log exception carried forward | RECOMMENDED |
| D6 | Add the **AI-provider specific terms**: no training on customer data by default, retention at the provider, and what happens if a provider changes its terms | **MUST** |

### 3.3 US state notice

| # | Change | Priority |
|---|---|---|
| U1 | Add the **ADMT out-of-scope statement** and the permitted-use restriction that backs it | **MUST** |
| U2 | Complete `us_state_scope` — name the states you actually target, rather than implying all of them | **MUST** |
| U3 | Complete `us_opt_in_mechanism` — today there is no sale or sharing, but the notice must say how an opt-in *would* be taken if that ever changes | RECOMMENDED |
| U4 | Add the **45-day response** commitment with the extension mechanics (CCPA allows +45 days with notice) | RECOMMENDED |

### 3.4 Missing documents (new)

| # | Document | Why | Priority |
|---|---|---|---|
| N1 | **Terms of Service** — SLA, liability cap, IP, termination, data ownership | The commercial foundation; nothing governs it today | **MUST** |
| N2 | **Acceptable Use Policy** — no ADMT, no regulated advice, no special-category data, no sole-channel legal notices | Converts assumptions into contractual restrictions | **MUST** |
| N3 | **AI output disclaimer** (may sit inside the ToS) — answers may be wrong; the product declines rather than guesses; the business remains responsible for what it publishes | Manages the single most likely dispute | **MUST** |
| N4 | **Sub-processor list** as a standalone, versioned page with change history | Art. 28(2) needs a named, current list — and customers need to see changes | **MUST** |
| N5 | **Security overview / trust page** — encryption, tenancy isolation, RLS, audit trail, pen-test posture | Every B2B buyer's security questionnaire asks for it | RECOMMENDED |
| N6 | **Cookie policy for the business's own site** (distinct from the widget's storage statement) | The widget statement is about the guest, not the business's staff dashboard | RECOMMENDED |

---

## 4. Things I checked and found correctly handled

Worth recording so a future review does not re-open them:

- **ePrivacy Art. 5(3) widget storage rule** — "nothing stored before the visitor opens the
  chat" matches CNIL, DSK and DPC guidance, and is audited in a real browser (`SEC-08`).
- **Controller/processor split** — correctly assigned: the business is controller, the
  platform is processor, with a DPA.
- **Retention** — a real number, a real schedule, and the "live conversations are never
  deleted" rule that most notices get wrong.
- **Erasure** — implemented (`SEC-10`) with an honest statement that the audit log records
  the fact of erasure and survives it.
- **No sale / no sharing / no training** — stated in the US notice, and true in the code
  (the US template's §4 has a test asserting it against the platform's actual behaviour).
- **Sub-processor disclosure of *what* is sent** — the "up to the last 6 messages, up to
  2000 characters per passage" level of detail is better than industry norm.
- **Attachment handling** — metadata recorded, file not opened, stated plainly.

---

## 5. Recommended sequence

1. **Now (before any customer):** P1, P2, P3, D1, D3, D4, D6, U1, U2, and N1–N4.
   These are the items a procurement or privacy review will stop on.
2. **Before first EU customer:** P4–P7, D2, D5, N5.
3. **Before first US customer:** U3, U4.
4. **Ongoing:** keep the sub-processor list versioned with a change log; re-review when
   any model provider changes its terms, and when the AI Act's remaining guidance lands.

---

## 6. What I cannot answer, and who must

- **Your legal entity name, address and jurisdiction** — the `platform_legal_name` /
  `platform_address` / `governing_law` markers are yours.
- **Whether you are EU-established** — decides whether Art. 27 (EU representative) applies.
- **Your liability position** — a liability cap is a commercial choice with an insurance
  consequence.
- **Whether to sign the AI Act Code of Practice on Transparency** — a strategic decision,
  not a technical one.

**A qualified lawyer should review the final text for your jurisdiction.** This document
tells you *what to fix and why*; it is not the review itself.

---

## 7. Sources

| Claim | Source | Date |
|---|---|---|
| AI Act Art. 50 applies from 2 Aug 2026; grace to 2 Dec 2026 for pre-existing systems, marking only | European Commission, *Transparency obligations under Article 50* (FAQ) | consulted 2026-11-20 |
| Four cumulative criteria for "interacting with an AI system"; "unless obvious" interpreted restrictively; notification from first interaction | Same FAQ, citing Commission Guidelines C/2026/6005 | 2026 |
| Code of Practice on Transparency of AI-Generated Content assessed adequate by Commission and AI Board | Same FAQ | 2026 |
| Provider = develops or has developed and places on market under own name; deployer = uses under its authority | AI Act Service Desk, Art. 3(3) / Art. 50 | 2026 |
| California ADMT obligations effective 1 Jan 2027; significant decisions = employment, finances, healthcare, housing, education; pre-use notice, opt-out, access, explanation | National Law Review, Ogletree Deakins, 19 Nov 2026; CCR §7200, §7220 | 2026-11-19 |
| CCPA civil penalties $2,633 per violation / $7,988 intentional, adjusted biannually in odd years | Same article, citing CPPA | 2026 |
| ePrivacy Art. 5(3) / national implementations for chat widgets (CNIL, DSK, DPC) | Already cited in `docs/PLAN.md` §19.3 via ChatterLab analysis | 2026-10-25 |
