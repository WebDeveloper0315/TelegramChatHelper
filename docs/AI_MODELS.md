# AI_MODELS.md

# Telegram AI Conversation Assistant

AI Model Specification

Version: 1.0

Status: Planning

---

# 1. Purpose

This document defines every AI component used by the project.

Its goals are to:

- Keep AI components modular.
- Allow replacing any model without affecting business logic.
- Document model selection.
- Record prompt engineering strategy.
- Define memory retrieval.
- Define context management.
- Define fallback behavior.
- Track future AI improvements.

No AI model should be tightly coupled to application logic.

---

# 2. AI Architecture

The system is composed of multiple independent AI services.

```
                Conversation

                     │

                     ▼

         Context Builder Service

                     │

     ┌───────────────┼────────────────┐

     ▼               ▼                ▼

Memory Retrieval  Emotion AI   Relationship AI

     │               │                │

     └───────────────┼────────────────┘

                     ▼

             Planning Service

                     ▼

             Reply Generator

                     ▼

         Human Behavior Engine

                     ▼

           Reply Recommendation
```

Each service has a single responsibility.

---

# 3. AI Services

## Conversation Analyzer

Purpose

Understand incoming messages.

Input

Conversation history

Current message

Output

ConversationContext

Tasks

- Topic detection
- Intent detection
- Important facts
- Question detection
- Conversation stage
- Follow-up opportunities

---

## Memory Extractor

Purpose

Determine what should become long-term memory.

Input

Conversation

Output

Memory candidates

Examples

Favorite food

Birthday

Country

Hobbies

Occupation

Travel plans

Preferences

Never store everything.

Only retain information likely to improve future conversations.

---

## Relationship Analyzer

Purpose

Estimate relationship progression.

Outputs

Trust Score

Interaction Frequency

Conversation Depth

Engagement Trend

Friendship Level

Confidence Score

---

## Emotion Analyzer

Purpose

Detect emotional state.

Outputs

Emotion

Confidence

Possible emotions

Happy

Excited

Curious

Neutral

Sad

Angry

Anxious

Stressed

Surprised

---

## Goal Planner

Purpose

Generate conversation strategies.

Inputs

Current Goal

Relationship

Memory

Conversation

Outputs

ConversationPlan

Example

Current topic

↓

Answer question

↓

Ask follow-up

↓

Introduce shared interest

↓

Natural ending

---

## Reply Generator

Purpose

Generate reply suggestions.

Outputs

Primary Reply

Alternative Replies

Confidence

Explanation

Follow-up Questions

---

## Conversation Summarizer

Purpose

Summarize conversations.

Outputs

Summary

Memory Updates

Future Topics

Action Items

---

# 4. Supported AI Providers

The system should support multiple providers.

Examples

Cloud

- OpenAI
- Anthropic
- Google
- Azure OpenAI

Local

- Ollama
- llama.cpp
- vLLM

The application should allow switching providers through configuration.

---

# 5. Embedding Models

Purpose

Semantic memory search.

Possible models

- bge-large
- bge-base
- e5-large
- e5-base

Responsibilities

Store embeddings

Search memories

Retrieve similar conversations

Never expose embeddings directly to users.

---

# 6. Prompt Engineering

Every prompt should contain:

System Prompt

↓

Retrieved Memory

↓

Conversation Summary

↓

Recent Messages

↓

Current Goal

↓

Relationship State

↓

User Preferences

↓

Current Message

↓

Required Output Format

The system prompt should remain stable.

Dynamic information should be inserted separately.

---

# 7. Prompt Management

Prompts should be stored separately.

Example

prompts/

system.md

planner.md

memory.md

summary.md

reply.md

emotion.md

No prompt should be hardcoded inside Python files.

---

# 8. Context Management

The model should not receive the entire conversation.

Instead

Conversation

↓

Recent Messages

↓

Conversation Summary

↓

Relevant Memories

↓

Current Goal

↓

Relationship State

↓

Prompt

↓

Model

This greatly reduces token usage.

---

# 9. Memory Retrieval

The memory engine should retrieve:

Recent memories

Relevant memories

Shared interests

Previous unanswered questions

Important dates

Personal preferences

Relationship milestones

Only the most relevant information should be included.

---

# 10. Confidence Estimation

Every AI response should include:

Confidence

Reason

Alternative

Example

Confidence

96%

Reason

Question matches previous discussion.

Alternative

Ask for clarification.

Low confidence should trigger recommendations rather than certainty.

---

# 11. Hallucination Prevention

Before generating replies:

Check

Recent conversation

Memory consistency

Relationship state

Goal consistency

If information is missing

Prefer

"I don't know."

or

"Ask a clarification."

instead of inventing facts.

---

# 12. Human Behavior Engine

The AI should recommend:

Reply timing

Message length

Typing duration

Conversation pacing

Topic changes

Conversation ending

Natural follow-ups

The purpose is to help conversations feel thoughtful rather than rushed.

---

# 13. Local AI Support

The architecture should support:

Offline execution

GPU acceleration

CPU fallback

Model switching

Quantized models

Streaming generation

---

# 14. Cloud AI Support

Support should include:

Streaming responses

Retries

Rate limiting

Timeouts

Cost tracking

Fallback providers

---

# 15. Model Selection Strategy

Different tasks may use different models.

Examples

Reasoning

↓

Large LLM

Summaries

↓

Medium LLM

Embeddings

↓

Embedding Model

Emotion

↓

Classifier

Never use one model for every task if a specialized model is more suitable.

---

# 16. Future AI Improvements

Potential future additions

Long-term episodic memory

Adaptive personality modeling

Conversation style learning

Tool calling

Multimodal understanding

Voice understanding

Image understanding

Knowledge graph memory

Fine-tuned planners

Custom memory ranking

---

# 17. AI Design Principles

The AI should be:

Helpful

Transparent

Configurable

Replaceable

Efficient

Predictable

Privacy respecting

No AI provider should be mandatory.

Every AI dependency should be replaceable through configuration.

The business logic must never depend on a specific model implementation.