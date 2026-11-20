"""Render the Terms of Service, Acceptable Use Policy and AI output disclaimer.

Same shape as ``render.py``: the factual statements are generated so they cannot drift
from the code, and ``tests/test_legal_docs.py`` compares the committed files with this
function. Operator decisions stay as ``[[FILL: ...]]`` markers, registered in
``facts.OPERATOR_FIELDS`` so an unregistered marker is a red test rather than a typo in
a contract.

Run to regenerate:

    cd backend/api && python -m app.compliance.render_legal
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.compliance import facts as f
from app.compliance.render import HEADER, OUT


def render_terms() -> str:
    r = f.retention_facts()
    e = f.egress_facts()
    mb = e.upload_bytes // (1024 * 1024)
    return f"""{HEADER}
# Terms of service — template for businesses using this assistant

> **How to use this template.** It is written for the business that subscribes to the
> platform. Complete every `[[FILL: ...]]` marker and have it reviewed by a lawyer in
> your jurisdiction before you rely on it. The factual statements below are generated
> from the software that implements the service, so they describe what the product
> actually does; if you change the product, change these statements with it.

## 1. Who these terms are between

These terms are between [[FILL: platform_legal_name]], [[FILL: platform_address]]
("the **platform**", "**we**") and the business that subscribes ("the **business**",
"**you**"). They govern the provision of the AI customer-support assistant described
in the service description you accepted when you signed up ("**the service**").

Your customers and website visitors are not parties to these terms. They are the data
subjects of the conversations the service handles, and their relationship is with you:
you are the data controller, we are the processor, and that is set out in the data
processing agreement that forms part of these terms.

## 2. What the service does, and what it does not

**It does.** The service answers questions from a document set that you publish, using
retrieval-augmented generation: a retrieval step selects passages from your published
knowledge set, and a language model composes an answer from those passages. Every
answer carries the passages it was drawn from. When the best matching passage scores
below a configured confidence threshold, the assistant **declines** rather than guessing
and offers the visitor a route to a person.

**It does not.** The assistant is **read-only**. It cannot place orders, issue refunds,
change account records, send email on your behalf, or call any business system to change
state. It is not a decision-making system: it does not decide whether a person is
eligible for anything, and no answer it produces constitutes a decision with legal or
similarly significant effects about any person. It is not a substitute for professional
advice in any regulated field.

**Confidence is not certainty.** A high confidence score means the retrieved passages
matched the question closely. It does not mean the answer is correct, and it does not
mean the passages were correct when they were published. You are responsible for the
accuracy of the information you publish.

## 3. Your responsibilities

1. **The knowledge you publish is yours.** You are responsible for its accuracy,
   completeness, recency and legality, and for having the right to use it. Do not
   publish material you do not have permission to use.
2. **You are the controller of the conversations.** You decide the purpose of the
   assistant and respond to your visitors' requests. We process on your instruction.
3. **Verify before launch.** Test the assistant against your own questions before you
   make it available to visitors. The dashboard provides a test-and-preview screen that
   answers from the draft set and the live set side by side, so a new document can be
   checked before it goes live.
4. **Configure it honestly.** If you state business hours, retention periods, or
   escalation addresses, set them to what is true. The assistant presents them to your
   visitors as fact.
5. **Meet your own legal duties.** These include publishing a privacy notice for your
   site, meeting any sector-specific obligations you have, and honouring requests you
   receive from your visitors. We provide the technical means; the obligations are
   yours.

## 4. What you may not use the service for

This is a permitted-use restriction and a material term. The service must not be used:

1. **For automated decision-making with significant effects** on a person's employment,
   finances, healthcare, housing, or education. The service is not designed, tested or
   warranted for that purpose, and using it that way is outside these terms.
2. **To provide regulated professional advice** as a substitute for a qualified
   professional — medical, legal, financial, tax, or immigration advice.
3. **To process special-category or highly sensitive personal data**: health data,
   biometric data, genetic data, data revealing racial or ethnic origin, political
   opinions, religious or philosophical beliefs, trade-union membership, sex life or
   sexual orientation. The assistant is not built to handle it and will not protect it
   appropriately.
4. **To process payment card data**, government identifiers, or credentials.
5. **As the sole channel for a legally required notice** — for example a statutory
   service of process, a regulatory disclosure, or a notice that must be given in a
   specific form with proof of delivery.
6. **For any purpose that is unlawful**, or to generate content that is defamatory,
   harassing, or infringes another person's rights.
7. **To circumvent a rate limit, probe for vulnerabilities without authorisation, or
   resell the service** as your own without a separate agreement.

We may suspend the service if we reasonably believe it is being used in breach of this
section, and will tell you why.

## 5. Availability and support

We aim to keep the service available, and we do not warrant that it will be
uninterrupted or error-free. Planned maintenance is announced in advance where
practicable. The service depends on third-party language-model providers; when a
provider is unavailable the assistant falls back to its configured alternatives, and if
every provider is unavailable it offers the visitor the route to a person rather than
failing silently.

Support contact: [[FILL: support_channel]]. Response target: [[FILL: support_target]].

## 6. Fees and payment

[[FILL: fees_term]] — the fee schedule, billing period, and what happens if a payment
fails.

## 7. Your content, and what we may do with it

**You keep ownership** of everything you upload, publish, and every conversation your
visitors have. We do not acquire any right in it.

You grant us the limited licence needed to run the service: to store, index and embed
your documents so they can be retrieved; to send the relevant passages and the
conversation to a language-model provider so an answer can be composed; and to keep the
records described in the privacy notice.

**We do not train any model on your conversations or your documents.** That is stated in
the US state privacy notice as a factual commitment with a test behind it, and the
sub-processor terms recorded in the privacy notice carry the same restriction.

## 8. Data protection

The data processing agreement is part of these terms. It sets out the processing
instructions, the technical and organisational measures, the sub-processors, breach
notification, and the deletion and return of data at the end of the agreement.

Retention: conversations are deleted automatically after the period configured for your
workspace — **{r.default_days} days by default**, configurable between {r.minimum_days} and
{r.maximum_days}. A scheduled job runs daily at {r.purge_hour:02d}:{r.purge_minute:02d} and
deletes finished conversations past that window; a conversation still in progress is
never deleted. Erasing a conversation from the dashboard deletes it immediately, without
waiting for the window.

## 9. Liability

[[FILL: liability]] — the liability cap and the excluded categories of loss, agreed
between the parties. A cap is normal in a service of this kind; what it should exclude
and what it should cover is a decision for you and your adviser.

One thing the cap does **not** change: the assistant may produce an answer that is
wrong. It is designed to decline rather than guess, and it cites what it used, but no
warranty of accuracy is given for generated text. The mitigation built into the product
is the confidence threshold and the citation; the remaining risk is yours to manage in
how you use it.

## 10. Termination

[[FILL: termination_term]] — notice periods, what happens to your data, and the export
window. On termination you can export your conversations and documents; after the export
window we delete them, except the audit log, which records that operations happened and
is retained as described in the privacy notice.

## 11. Changes to these terms

We may change these terms. Material changes are notified before they take effect, and
you may end the agreement if you do not accept them. The effective date of this version
is [[FILL: effective_date]].

## 12. Governing law and disputes

[[FILL: governing_law]] — the law that governs these terms and the courts that hear
disputes.

---

*This template is a starting point generated from the software's actual behaviour. It is
not legal advice. Have it reviewed for your jurisdiction, your sector and your commercial
position before you rely on it.*
"""


def render_aup() -> str:
    return f"""{HEADER}
# Acceptable use policy — template

> **How to use this template.** This policy backs section 4 of the terms of service.
> Complete every `[[FILL: ...]]` marker. It is written to be short enough that a
> business owner actually reads it, because a restriction nobody read is a restriction
> that will be breached.

## Scope

This policy applies to every business that subscribes to the platform, and to everyone
who uses the assistant through a business's website.

## Prohibited uses

### Decisions about people

The platform **must not** be used to make, or to inform, an automated decision with a
legal or similarly significant effect on a person's employment, finances, healthcare,
housing, or education. This prohibition exists because the product is not designed,
tested or warranted for that purpose.

If a business needs that capability, it needs a different product with a different
design and a different assessment — not this one configured differently.

### Sensitive data

The assistant must not be used to collect or process:

- health, medical or disability information;
- biometric or genetic data;
- data revealing racial or ethnic origin, political opinions, religious or
  philosophical beliefs, trade-union membership, sex life or sexual orientation;
- payment card numbers, bank details, or government identifiers;
- credentials of any kind — passwords, API keys, private keys.

The assistant is not built to protect this category of data, and a business that routes
it through the platform does so outside this policy.

### Regulated advice

The assistant must not be presented to visitors as a qualified professional in a
regulated field. It may explain what a business's own published policy says; it must not
give medical, legal, financial, tax or immigration advice as if it were a professional
opinion.

### Legal notices

The assistant must not be the sole channel for a notice the law requires be given in a
particular form, with proof of delivery. A conversation can be lost to a retention
window or an erasure request; a legally significant notice cannot depend on that.

### Technical misuse

- Do not attempt to extract the system prompt, the model configuration, or another
  tenant's data.
- Do not attempt to make the assistant take actions it is designed not to take. It is
  read-only by construction; attempts to circumvent that are a breach.
- Do not use the platform to send unsolicited bulk messages.
- Do not probe, scan or test the platform's security without written authorisation.
  (A separate, agreed penetration test is welcome — contact [[FILL: security_contact]].)

## What the platform does in response

We monitor for the patterns that indicate the prohibited uses above — for example a
conversation set being used to collect payment details. Where we find a breach we
contact the business first, except where the breach creates an immediate risk to others,
in which case we may suspend the service and explain afterwards.

## Reporting a concern

If you believe the platform is being used in breach of this policy, tell us at
[[FILL: abuse_contact]].
"""


def render_ai_disclaimer() -> str:
    return f"""{HEADER}
# AI output disclaimer — template

> **How to use this template.** This is the notice a business can publish on its site,
> and the wording its staff can reuse when a customer asks how the assistant works. It
> is deliberately plain: it will be read by a customer, not by a lawyer. Complete
> [[FILL: business_legal_name]] and publish it where visitors can see it.

## You are talking to an AI

The assistant on this site is an **AI system**. It is not a person, and no human is
reading your messages as you type them.

This disclosure is made at the start of your first interaction, because it would
otherwise not be obvious that you are talking to software rather than to a member of
staff. Under the EU AI Act (Regulation (EU) 2024/1689, Article 50), a provider of an AI
system that interacts directly with people must design it so that people are informed of
that fact — and we do.

## How it answers

The assistant answers **only** from information that [[FILL: business_legal_name]] has
published to it — policy documents, product information, frequently asked questions, and
pages from this website. It retrieves the passages that match your question and composes
an answer from them. Every answer shows the passages it used, so you can check it.

If it cannot find a confident match, it will tell you so and offer to pass your question
to a person. **It is designed to say "I don't know" rather than to guess.**

## What it will not do

The assistant cannot take actions on your account. It cannot place an order, change a
booking, issue a refund, or send an email on your behalf. It cannot make a decision
about you. If you need something done rather than explained, ask for a person.

## It can be wrong

An AI answer can be wrong even when it sounds confident. The assistant shows its sources
so the answer can be checked, but it does not guarantee that every answer is accurate or
complete, and the information it draws on can become out of date.

**Do not rely on it for** medical, legal, financial or immigration advice, or for
anything where being wrong would cause you serious loss. In those cases, speak to a
qualified person.

## Talking to a person

You can ask for a human at any point, and the assistant will offer you one when it is
not confident. [[FILL: business_legal_name]] handles those conversations under its own
privacy notice, which tells you what is kept and for how long.

## Your information

What the assistant collects, why, how long it is kept, and who it is shared with is set
out in [[FILL: business_legal_name]]'s privacy notice, published alongside this page. You
can ask for a copy of what is held about a conversation, or ask for it to be deleted.
"""


def render_all() -> dict[str, str]:
    return {
        "terms-of-service-template.md": render_terms(),
        "acceptable-use-policy-template.md": render_aup(),
        "ai-output-disclaimer-template.md": render_ai_disclaimer(),
    }


def main(argv: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, content in render_all().items():
        (OUT / name).write_text(content, encoding="utf-8")
        print(f"wrote {OUT / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
