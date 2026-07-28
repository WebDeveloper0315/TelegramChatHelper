# PROMPTS.md

# Telegram AI Conversation Assistant

Prompt Engineering Specification

Version: 1.0

Status: Planning

---

# 1. Purpose

This document defines every prompt used by the AI system.

Goals:

- Standardize prompt engineering.
- Prevent prompt duplication.
- Improve maintainability.
- Enable prompt versioning.
- Keep prompts independent from application code.
- Make prompts easy to test and improve.

Prompt files should never be hardcoded inside Python source files.

---

# 2. Prompt Directory

```
prompts/

    system.md

    planner.md

    reply.md

    memory.md

    summary.md

    emotion.md

    relationship.md

    uncertainty.md

    timing.md

    followup.md
```

Each prompt should have a single responsibility.

---

# 3. Prompt Architecture

Every prompt consists of:

```
System Prompt

↓

Task Description

↓

Rules

↓

Context

↓

Required Output Format

↓

Validation Checklist
```

Never mix unrelated tasks in one prompt.

---

# 4. Master System Prompt

The system prompt should remain as stable as possible.

Responsibilities:

- Define AI behavior.
- Define communication principles.
- Define reasoning expectations.
- Define safety boundaries.
- Define output quality expectations.

It should not contain conversation-specific information.

Dynamic context belongs elsewhere.

---

# 5. Context Builder

The application constructs context before calling the model.

Example order:

```
System Prompt

↓

Conversation Goal

↓

Relationship Profile

↓

Relevant Memories

↓

Conversation Summary

↓

Recent Messages

↓

Current Message

↓

Task Prompt
```

The AI should never receive unnecessary information.

---

# 6. Planner Prompt

Purpose

Generate a conversation strategy.

Input

ConversationContext

Goal

RelationshipProfile

MemoryProfile

Output

ConversationPlan

Required output

Current objective

Suggested direction

Topics to introduce

Topics to avoid

Reasoning

Confidence

---

# 7. Reply Prompt

Purpose

Generate reply suggestions.

Input

Conversation

Goal

Relationship

Memory

ConversationPlan

Output

Primary reply

Alternative replies

Explanation

Confidence

The reply should:

- Sound natural.
- Match the user's preferred tone.
- Be consistent with prior conversation.
- Avoid inventing facts.

---

# 8. Memory Prompt

Purpose

Extract long-term memories.

Input

Conversation

Output

Memory candidates

Example

Store:

Favorite food

Occupation

Birthday

Country

Interests

Travel plans

Avoid storing:

Temporary details

Unimportant facts

Sensitive information unless explicitly intended for long-term use

---

# 9. Summary Prompt

Purpose

Summarize conversations.

Output

Summary

Key topics

Important facts

Future follow-ups

Memory updates

Summaries should remain concise.

---

# 10. Emotion Prompt

Purpose

Estimate emotional state.

Output

Emotion

Confidence

Evidence

Possible emotions

Happy

Excited

Neutral

Sad

Angry

Confused

Anxious

Curious

---

# 11. Relationship Prompt

Purpose

Estimate relationship progression.

Outputs

Trust Score

Conversation Depth

Engagement

Interaction Trend

Suggested relationship stage

---

# 12. Timing Prompt

Purpose

Recommend reply timing.

Inputs

Relationship

Conversation Pace

Message Length

Current Time

Urgency

Outputs

Suggested wait time

Reason

Confidence

Example

Reply now

Wait 5 minutes

Reply tomorrow

Ask user to decide

---

# 13. Uncertainty Prompt

Purpose

Determine whether the AI has enough information.

Output

Confidence

Reason

Missing information

Recommendation

If confidence is low:

Recommend asking a clarifying question instead of guessing.

---

# 14. Follow-Up Prompt

Purpose

Suggest natural follow-up questions.

Examples

Ask about hobbies.

Continue previous topic.

Introduce shared interests.

Check on previous plans.

Avoid repeating recently asked questions.

---

# 15. Prompt Rules

Every prompt must:

Have one responsibility.

Produce structured output.

Avoid unnecessary verbosity.

Never invent facts.

Respect conversation history.

Respect stored memories.

Respect conversation goals.

---

# 16. Output Format

Every AI response should follow structured JSON.

Example

```json
{
  "confidence": 0.94,
  "reasoning": "...",
  "reply": "...",
  "alternatives": [
    "...",
    "..."
  ],
  "memory_updates": [],
  "warnings": []
}
```

Never return free-form text when structured output is expected.

---

# 17. Prompt Versioning

Every prompt should include:

Version

Purpose

Inputs

Outputs

Dependencies

Last Modified

Breaking Changes

Prompt updates should be tracked just like source code.

---

# 18. Prompt Testing

Every prompt should have test cases.

Example

Input

Conversation

↓

Expected Reply

↓

Expected Confidence

↓

Expected Memory Update

↓

Expected Validation

Prompt quality should improve through testing rather than intuition.

---

# 19. Token Budget Strategy

Prioritize information in this order:

1. Current message

2. Recent conversation

3. Conversation summary

4. Relevant memories

5. Relationship profile

6. Conversation goal

7. System prompt

Never send the full chat history if a summary plus retrieved memories provide sufficient context.

---

# 20. Prompt Design Principles

Prompts should be:

Simple

Modular

Predictable

Reusable

Testable

Versioned

Documented

No prompt should depend on hidden assumptions.

Every prompt should be understandable by another engineer without additional explanation.