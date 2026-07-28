# ARCHITECTURE.md

# Telegram AI Conversation Assistant

Architecture Version: 1.0

Status: Planning

---

# 1. Architecture Philosophy

The project follows the principles of:

- Clean Architecture
- Domain-Driven Design (DDD)
- SOLID Principles
- Dependency Injection
- Repository Pattern
- Event-Driven Communication
- Plugin-Oriented Design

Business logic must never depend directly on Telegram, AI providers, databases, or the user interface.

Everything outside the core business logic should be replaceable.

---

# 2. High-Level Architecture

                        +----------------------+
                        |   Desktop UI         |
                        +----------+-----------+
                                   |
                                   |
                        +----------v-----------+
                        |  Application Layer   |
                        +----------+-----------+
                                   |
        ------------------------------------------------------------
        |            |             |            |                  |
        |            |             |            |                  |
+-------v----+ +------v------+ +----v-----+ +----v-----+ +---------v--------+
| Goal       | | Memory      | | Planner  | | Analyzer | | Relationship     |
| Manager    | | Engine      | | Engine   | | Engine   | | Engine           |
+-------+----+ +------+------+-+----------+ +----+-----+ +---------+--------+
        |             |                      |                     |
        ------------------------------------------------------------
                                   |
                         +---------v----------+
                         | Reply Generator    |
                         +---------+----------+
                                   |
                         +---------v----------+
                         | Human Behavior     |
                         | Simulator          |
                         +---------+----------+
                                   |
                        +----------v-----------+
                        | Telegram Gateway     |
                        +----------+-----------+
                                   |
                           Telegram Network

---

# 3. Layer Responsibilities

## Presentation Layer

Responsible for:

- Desktop interface
- User interaction
- Settings
- Notifications
- Conversation viewer
- Goal editor

Never contains business logic.

---

## Application Layer

Coordinates all services.

Responsible for:

- Use cases
- Dependency injection
- Workflow orchestration
- Command handling
- Event handling

---

## Domain Layer

Contains all business rules.

Includes:

- Conversation
- Contact
- Goal
- Memory
- Relationship
- Reply
- Summary

The Domain Layer must not depend on external libraries.

---

## Infrastructure Layer

Responsible for:

- Telegram communication
- AI providers
- SQLite
- File system
- Logging
- Configuration
- Local AI models
- Cloud AI providers

Everything here can be replaced independently.

---

# 4. Core Modules

## Telegram Gateway

Responsibilities:

- Login
- Receive messages
- Send messages
- Read history
- Detect edits
- Typing status
- Connection management

No business logic.

---

## Conversation Engine

Responsibilities:

- Parse conversation
- Maintain context
- Detect active topic
- Detect conversation stage
- Build conversation state

Outputs:

ConversationContext

---

## Memory Engine

Responsibilities:

- Extract memories
- Update memories
- Forget outdated information
- Merge duplicated memories
- Store embeddings

Outputs:

MemoryProfile

---

## Goal Manager

Stores independent goals for every contact.

Example:

Contact A

Goal:
Practice English

Contact B

Goal:
Professional networking

Contact C

Goal:
Friendship

Goals influence planning but do not force responses.

---

## Relationship Engine

Calculates:

- Trust score
- Familiarity
- Interaction frequency
- Conversation depth
- Response quality
- Engagement trend

Produces:

RelationshipProfile

---

## Emotion Analyzer

Detects:

- Happiness
- Sadness
- Anger
- Excitement
- Stress
- Curiosity
- Neutral

Provides confidence scores.

---

## Planner Engine

Receives:

ConversationContext

RelationshipProfile

Goal

Memory

Produces:

ConversationPlan

Example:

Current objective

↓

Answer question

↓

Ask follow-up

↓

Introduce new topic

↓

End naturally

---

## Reply Generator

Generates:

- Primary reply
- Alternative replies
- Confidence
- Explanation

Never sends messages directly.

---

## Human Behavior Simulator

Determines:

- Response timing
- Typing duration
- Message splitting
- Follow-up timing
- Conversation pacing

Produces recommendations rather than directly controlling messaging.

---

# 5. Data Flow

Incoming Telegram Message

↓

Telegram Gateway

↓

Conversation Engine

↓

Memory Update

↓

Relationship Update

↓

Goal Manager

↓

Planner Engine

↓

Reply Generator

↓

Human Behavior Simulator

↓

UI Suggestion

↓

User Review

↓

Message Sent

---

# 6. Storage Architecture

SQLite

Stores:

Contacts

Conversation History

Goals

Settings

Summaries

Memory Profiles

Configuration

Embeddings should be stored separately.

---

# 7. AI Layer

AI should be abstracted.

Never call an AI model directly from business logic.

Create interfaces.

Example:

LLMProvider

EmbeddingProvider

EmotionClassifier

SummaryGenerator

Future providers should plug in without changing core logic.

---

# 8. Event System

Internal communication should use events.

Examples:

MessageReceived

MemoryUpdated

GoalChanged

ReplyGenerated

SummaryCreated

RelationshipUpdated

Advantages:

Loose coupling

Easy plugin integration

Better testing

---

# 9. Dependency Rules

Allowed

UI

↓

Application

↓

Domain

↓

Infrastructure

Forbidden

Infrastructure

↓

Domain

UI

↓

Infrastructure

Telegram

↓

Business Logic

---

# 10. Folder Structure

src/

    app/
        use_cases/
        services/
        commands/
        events/

    domain/
        conversation/
        contact/
        memory/
        goals/
        planner/
        relationship/

    infrastructure/
        telegram/
        database/
        ai/
        logging/
        config/

    presentation/
        desktop/
        widgets/
        dialogs/

    plugins/

    tests/

    docs/

---

# 11. Database Design

Core Tables

contacts

messages

conversation_summary

memory

goals

relationship

settings

logs

Future tables

embeddings

plugin_data

analytics

---

# 12. Plugin System

Plugins may register:

Commands

AI Providers

UI Pages

Background Services

Memory Sources

Conversation Strategies

Plugins should never modify core code directly.

---

# 13. Error Handling

Errors should be categorized.

Recoverable

Retry

User Input

Configuration

AI Provider

Telegram

Unexpected

All errors should include structured logging.

---

# 14. Security

Store secrets in environment variables.

Encrypt sensitive local data where appropriate.

Never hardcode:

API Keys

Passwords

Tokens

Session files

---

# 15. Testing Strategy

Unit Tests

Domain Layer

Application Layer

Integration Tests

Telegram Gateway

Database

AI Providers

End-to-End Tests

Full conversation pipeline

Target coverage:

Core business logic >90%

---

# 16. Future Scalability

The architecture should support:

Multiple Telegram accounts

Discord integration

WhatsApp integration (if supported by applicable APIs and policies)

Email integration

Voice assistant

Cloud synchronization

Web dashboard

Mobile companion application

Because the Domain Layer is platform-independent, new messaging platforms should only require implementing a new gateway.

---

# 17. Architectural Rules

Never place AI logic inside the UI.

Never place database code inside business logic.

Never allow Telegram code inside the Domain Layer.

Every module should have a single responsibility.

Every external dependency should have an interface.

Every feature should be independently testable.

Large features should be developed incrementally.

Documentation must remain synchronized with implementation.