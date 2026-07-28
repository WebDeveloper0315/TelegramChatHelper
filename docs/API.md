# API.md

# Telegram AI Conversation Assistant

Internal API & Interface Specification

Version: 1.0

Status: Planning

---

# 1. Purpose

This document defines the internal APIs between modules.

Goals:

- Decouple components
- Enable dependency injection
- Allow provider replacement
- Standardize interfaces
- Improve testability
- Support future plugins

This document does **not** define HTTP or REST APIs.

---

# 2. Design Principles

Every module should communicate through interfaces.

Rules:

- No module accesses another module's database directly.
- No module depends on implementation details.
- Interfaces should be small and focused.
- Prefer composition over inheritance.
- Return domain objects instead of raw SQL records.

---

# 3. Layer Communication

```
Presentation

↓

Application

↓

Domain Interfaces

↓

Infrastructure Implementations
```

Only the infrastructure layer knows about Telegram, databases, AI providers, and the filesystem.

---

# 4. Telegram Gateway Interface

Purpose

Provide a unified interface to Telegram.

Responsibilities

- Connect
- Authenticate
- Receive messages
- Send messages
- Read history
- Download media
- Monitor updates

Example methods

```
connect()

disconnect()

login()

logout()

send_message()

edit_message()

delete_message()

get_history()

listen()
```

No business logic belongs here.

---

# 5. AI Provider Interface

Purpose

Abstract all language models.

Required methods

```
generate()

stream_generate()

count_tokens()

health_check()

provider_name()
```

Supported implementations

- Anthropic
- OpenAI
- Google
- Ollama
- llama.cpp
- vLLM

The application should not know which provider is active.

---

# 6. Embedding Provider Interface

Responsibilities

```
embed_text()

embed_batch()

similarity_search()
```

Used by

Memory Engine

Conversation Search

Knowledge Retrieval

---

# 7. Memory Repository Interface

Responsibilities

```
save_memory()

update_memory()

delete_memory()

find_by_contact()

semantic_search()

find_recent()
```

Only repositories communicate with storage.

---

# 8. Message Repository Interface

Responsibilities

```
save()

update()

delete()

find()

find_recent()

search()
```

Supports

Pagination

Filtering

Sorting

---

# 9. Contact Repository Interface

Responsibilities

```
create()

update()

delete()

find()

find_by_username()

find_by_telegram_id()
```

---

# 10. Goal Repository Interface

Responsibilities

```
save_goal()

update_goal()

delete_goal()

get_goal()

list_goals()
```

Each contact can have multiple goals if future versions require them.

---

# 11. Summary Repository Interface

Responsibilities

```
save_summary()

latest_summary()

history()

delete()
```

---

# 12. Relationship Repository Interface

Responsibilities

```
save_profile()

update_profile()

get_profile()
```

---

# 13. Conversation Engine Interface

Input

Conversation

Output

ConversationContext

Methods

```
analyze()

build_context()

extract_topic()

detect_stage()
```

---

# 14. Memory Engine Interface

Methods

```
extract()

update()

forget()

merge()

retrieve()
```

---

# 15. Planner Interface

Methods

```
plan()

evaluate()

suggest_topics()

recommend_next_action()
```

Produces

ConversationPlan

---

# 16. Reply Generator Interface

Methods

```
generate()

alternatives()

explain()

estimate_confidence()
```

Produces

ReplySuggestion

---

# 17. Emotion Analyzer Interface

Methods

```
detect()

confidence()

emotion_scores()
```

Produces

EmotionProfile

---

# 18. Human Behavior Engine Interface

Methods

```
reply_delay()

typing_duration()

message_split()

conversation_pacing()

recommend_send_time()
```

Produces

BehaviorRecommendation

---

# 19. Event Bus Interface

Purpose

Loose coupling between modules.

Methods

```
publish()

subscribe()

unsubscribe()
```

Example events

MessageReceived

ReplyGenerated

GoalUpdated

MemoryUpdated

SummaryCreated

RelationshipChanged

---

# 20. Plugin Interface

Every plugin should expose

```
initialize()

shutdown()

metadata()

register_services()

register_events()

register_commands()
```

Plugins should never modify core application code.

---

# 21. Configuration Interface

Methods

```
load()

save()

reload()

validate()
```

Supports

YAML

JSON

Environment Variables

---

# 22. Logging Interface

Methods

```
debug()

info()

warning()

error()

critical()
```

All logs should include

Timestamp

Component

Level

Message

Context

---

# 23. Background Task Interface

Methods

```
start()

stop()

pause()

resume()

status()
```

Examples

Memory indexing

Conversation summarization

Backup

Plugin services

---

# 24. Error Handling

Every interface should define

Expected Exceptions

Validation Rules

Retry Policy

Timeout Policy

No implementation should expose provider-specific exceptions directly.

---

# 25. Return Objects

Interfaces should return domain models.

Examples

ConversationContext

MemoryProfile

ReplySuggestion

ConversationPlan

RelationshipProfile

EmotionProfile

Avoid returning dictionaries where typed domain objects are more appropriate.

---

# 26. Versioning

Interfaces should remain backward compatible whenever practical.

Breaking changes require

Version update

Migration notes

Implementation updates

Documentation updates

---

# 27. Testing

Every interface should support

Mock implementation

Fake implementation

Real implementation

Dependency injection should allow replacing every implementation during testing.

---

# 28. API Design Rules

Interfaces define behavior, not implementation.

Business logic must never depend on external SDKs.

Infrastructure implements interfaces.

Application coordinates interfaces.

Domain defines contracts.

This separation ensures that Telegram, AI providers, storage engines, and plugins can all be replaced independently without changing the application's core behavior.