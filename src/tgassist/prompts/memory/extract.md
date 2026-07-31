---
id: memory_extract
version: 1.0.0
purpose: Propose long-term facts worth remembering about a contact, with quotations
inputs:
  - categories
  - already_proposed
  - transcript
untrusted:
  - already_proposed
  - transcript
output_schema: schemas/memory_proposals.schema.json
last_modified: 2026-07-30
breaking_changes: []
---

# Task

Read the conversation below and identify facts that would be worth remembering
for future conversations with this person.

Each fact you identify becomes a **proposal** shown to the person who owns this
conversation. They accept or reject it. You are not writing to memory; you are
suggesting what might belong there.

# What is worth remembering

A fact is worth proposing when it would still be useful in a conversation weeks
from now: who someone is, where they are, what they do, what they care about,
what they have planned, what they have asked that is still unanswered, and what
they have asked you to avoid.

# What is not

- Anything that will be untrue next week. The weather, a mood, what someone is
  doing right now.
- Trivia with no bearing on future conversations.
- Sensitive details that were not clearly shared to be remembered — health,
  finances, credentials, anything about a third party who is not in this
  conversation.
- Anything you are inferring rather than reading. If the conversation does not
  say it, it is not a fact.

# Rules

1. Every proposal must quote the text you read it from, **verbatim**. A
   proposal whose quotation does not appear in the conversation is discarded
   before the person sees it, so an approximate quotation wastes the proposal.
2. Propose at most one fact per distinct thing. Do not split one fact into
   several proposals, and do not combine several into one.
3. Use one of the categories listed below. If none fits, use `other`.
4. Report your confidence as a number between 0 and 1. Be honest rather than
   generous: a low confidence is useful information, and an inflated one wastes
   the person's attention.
5. If there is nothing worth remembering, return an empty list. That is a
   correct and common answer.
6. Do not propose anything already listed as previously proposed.

# Categories

{{categories}}

# Previously proposed for this conversation

{{already_proposed}}

# Conversation

{{transcript}}

# Required output

Reply with JSON only, in exactly this shape:

```
{
  "proposals": [
    {
      "category": "one of the categories above",
      "value": "the fact, as one short sentence",
      "confidence": 0.0,
      "evidence": "the verbatim text you read it from"
    }
  ]
}
```

# Before you answer

- Does every proposal quote text that appears in the conversation exactly?
- Is every fact still going to be true next week?
- Is every category from the list?
- Have you invented anything? Remove it.
