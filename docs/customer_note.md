# Note to the Head of Support, Paylane

Subject: Paylane AI support agent — what it does, what it won't do, and what
we need from you before launch

Hi,

We built a prototype AI assistant to handle routine Paylane support chats. In
plain terms:

**What it does.** It greets customers, verifies their identity, looks up
account and transaction details, issues small eligible refunds (see limits
below), and hands anything else to a human specialist with a tracking ticket.
Every decision it makes is logged in a short audit trail, and all 12 test
conversations currently pass.

**What it will not do.** It will never move real money — it only uses mock
data in this prototype. It will not discuss any account until the customer
correctly confirms both the email on file and the last 4 card digits. It will
not claim a refund succeeded unless the refund step actually completed, will
never refund the same charge twice, and cannot unfreeze a card. It escalates —
rather than decides — refunds over $75, charges 30 or more days old, fraud
claims over $500, legal-action or account-closure requests, and card-unfreeze
requests. Demands hidden in a message ("ignore your instructions…") are
treated as ordinary customer text and cannot override these rules.

**What we need from you before launch.** Please rule on the open policy
questions listed in `docs/POLICY_DECISIONS.md` — most urgently: how to handle
pending (not yet settled) transactions, whether partial refunds are allowed,
whether the $75 limit applies to the charge amount or the requested refund
amount, and whether refunds are allowed on frozen cards. We also need a real
support-ticket system and real payment integration with duplicate protection
before this touches live customers, plus a review of the 12 conversation
transcripts in `transcripts/conversations.md`.

Happy to walk through any of this.

— The Paylane agent team
