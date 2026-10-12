# coordinator/support_knowledge.py: Support knowledge corpus for the
# customer-service agent's KB-gap auto-drafter.
#
# Mirrors the prose returned by web/src/services/support-knowledge.ts:
# generateKnowledge(). Keep in sync. The public-facing support chat is
# the canonical consumer; this Python copy exists only so the coordinator
# customer_service agent can ground Haiku answers when it auto-drafts a
# support_kb row to close a detected KB gap.

from __future__ import annotations

import re

# Public support address. It appears in the corpus below and is the only email
# address a drafted answer may contain (see kb_rejection_reason).
SUPPORT_EMAIL = "support@craftandship.io"

# Harbor pricing constants. Mirror values rendered by /pricing
# (harborSubscriptionPrice / harborSlidingScalePrice in web/src/app.ts).
HARBOR_DEFAULT_PRICE = "$39"
HARBOR_FLOOR_PRICE = "$19"


def generate_knowledge() -> str:
    """Return the same factual corpus the support chat uses.

    Plain prose; no markdown rendering required. The support agent passes
    this verbatim into Haiku's system prompt so drafted answers stay
    grounded in current pricing and process facts.
    """
    return (
        "## What is Craft & Ship?\n"
        "Craft & Ship builds custom websites for healers, therapists, "
        "breathwork facilitators, and event producers. You describe what you "
        "do in plain English through a conversation with our AI companion "
        "(Haven), your site is built from a library of tested building "
        "blocks matched to what you describe, and every website needs a "
        "recorded review approval before it goes live.\n\n"
        "## Pricing\n\n"
        "One subscription covers everything: a custom website, hosting, an "
        "AI companion who helps you find the words, unlimited revisions and "
        "rebuilds, and a recorded review approval for every website before it goes live.\n\n"
        f"- Choose any amount from {HARBOR_DEFAULT_PRICE} a month down to "
        f"{HARBOR_FLOOR_PRICE} a month, whatever fits your practice.\n"
        "- The first build is free in preview while you walk around it. You "
        "only pay when you say \"make it public.\"\n"
        "- Your website stays up if you ever pause or cancel.\n\n"
        "## What is Included\n"
        "- A custom website, shaped with you in conversation and built from "
        "a library of tested building blocks\n"
        "- Every website needs a recorded review approval before it goes live\n"
        "- Hosting and care included, with uptime watched day and night\n"
        "- Unlimited revisions and rebuilds, kept across the years\n"
        "- An AI companion (Haven) who helps you find the words, whenever "
        "you need her\n"
        "- Yours: if you ever leave, we hand the whole site over intact\n\n"
        "## How the Process Works\n"
        "1. Begin a conversation at /lighthouse/table with Haven, the AI "
        "companion\n"
        "2. Talk in plain English about your work, offerings, clients, and "
        "the rhythm of your week\n"
        "3. When the spec is clear, your website is built (free, in preview)\n"
        "4. Walk around your preview\n"
        "5. When you are happy, click \"Make it public\" and pick the "
        "monthly amount that fits your practice\n"
        "6. Your site goes live; Haven leaves a blessing turn on your "
        "conversation\n\n"
        "## After Deployment\n"
        "- Your website is live, hosted by us\n"
        "- Request changes anytime; unlimited revisions and rebuilds\n"
        "- Each site runs on its own isolated infrastructure with its own "
        "database\n"
        "- Data is secure: parameterized queries, CSRF protection, secure "
        "sessions\n\n"
        "## Build Failures and Retries\n"
        "- If a build fails, you can retry it from the dashboard or status "
        "page at no extra cost.\n"
        "- The platform automatically retries transient errors up to 3 times.\n"
        "- Rebuilds are free; they reuse your existing spec and run the "
        "full build pipeline again.\n\n"
        "## Preview and Revisions\n"
        "- After your website is built, we email you its preview address and "
        "it enters \"preview\" status while it needs a recorded review approval before it goes "
        "public.\n"
        "- You can request unlimited free revisions during preview and "
        "across the years afterward.\n"
        "- Every website needs a recorded review approval before it goes public.\n"
        "- Click \"Make it public\" when the website feels right.\n\n"
        "## Account Management\n"
        "- You can export all your data (profile, builds, conversations) "
        "as JSON from the dashboard.\n"
        "- To delete your account, visit the account settings page.\n"
        "- Password reset is available via the \"Forgot Password\" link "
        "on the login page.\n\n"
        "## Support\n"
        f"- Email: {SUPPORT_EMAIL}\n"
        "- The support chat is available on every page"
    )


def normalize_question(text: str) -> str:
    """Mirror normalizeText() in web/src/routes/support.ts.

    Lower-case, strip punctuation, collapse whitespace. Used to dedupe
    support_kb rows on question_normalized so repeated KB-gap detections
    for the same question don't pile up duplicate rows.
    """
    import re

    if not text:
        return ""
    s = text.lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


MAX_KB_QUESTION_CHARS = 500
MAX_KB_ANSWER_CHARS = 1200

_URL_RE = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HTML_RE = re.compile(r"<[^>]*>")


def kb_rejection_reason(question: str, answer: str) -> str | None:
    """Return why a drafted Q&A pair must not be cached in support_kb, or None.

    support_kb answers are served verbatim to other customers by the support
    chat, so a model draft that a hostile question steered into carrying a
    link, an address or markup is dropped rather than stored.
    """
    if len(question) > MAX_KB_QUESTION_CHARS:
        return "question too long"
    if len(answer) > MAX_KB_ANSWER_CHARS:
        return "answer too long"
    if _URL_RE.search(answer) or "](" in answer:
        return "answer contains a link"
    if _HTML_RE.search(answer) or "```" in answer:
        return "answer contains markup"
    for address in _EMAIL_RE.findall(answer):
        if address.lower() != SUPPORT_EMAIL:
            return "answer contains an email address other than the support address"
    return None
