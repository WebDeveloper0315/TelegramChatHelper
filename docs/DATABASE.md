# DATABASE.md

# Telegram AI Conversation Assistant

Database Design Specification

Version: 1.0

Status: Planning

Database Engine: SQLite (MVP)

Future Support:

- PostgreSQL
- MySQL (Optional)

---

# 1. Design Principles

The database should be:

- Normalized where practical
- Easy to migrate
- Modular
- Extensible
- Fast to query
- Easy to back up
- Easy to encrypt

Business logic should never depend on SQL implementation details.

Always access the database through repositories.

---

# 2. Database Overview

Database Modules

Contacts

↓

Messages

↓

Conversation Summaries

↓

Long-Term Memory

↓

Conversation Goals

↓

Relationship Analytics

↓

AI Metadata

↓

Embeddings

↓

Settings

↓

Plugins

---

# 3. Entity Relationship Overview

Contact

│

├── Messages

├── Memories

├── Goals

├── Relationship

├── Summaries

├── AI Analysis

└── Embeddings

Each contact owns its own conversation history.

---

# 4. Contacts Table

Purpose

Store one record for every Telegram contact.

Fields

id

telegram_user_id

username

display_name

first_name

last_name

phone_number

language

country

timezone

created_at

updated_at

last_seen

is_active

Indexes

telegram_user_id

username

---

# 5. Messages Table

Purpose

Store every conversation message.

Fields

id

contact_id

telegram_message_id

sender

message_type

text

reply_to

forwarded

edited

deleted

created_at

Indexes

contact_id

created_at

telegram_message_id

---

# 6. Conversation Summary Table

Purpose

Store AI-generated summaries.

Fields

id

contact_id

summary

summary_version

start_message

end_message

created_at

---

# 7. Memory Table

Purpose

Store important long-term memories.

Fields

id

contact_id

category

key

value

confidence

importance

source_message

created_at

updated_at

Examples

Favorite Food

Country

Birthday

Occupation

Pet

Travel Plans

Relationship Status

Preferences

---

# 8. Goals Table

Purpose

Store conversation objectives.

Fields

id

contact_id

goal_name

goal_description

priority

status

created_at

updated_at

Examples

Friendship

Professional Networking

Language Practice

Reconnect

---

# 9. Relationship Table

Purpose

Track relationship progression.

Fields

id

contact_id

trust_score

engagement_score

conversation_depth

friendship_level

last_interaction

total_messages

average_response_time

updated_at

---

# 10. AI Analysis Table

Purpose

Store AI-generated metadata.

Fields

id

message_id

emotion

intent

topic

confidence

analysis_version

created_at

This prevents repeated AI processing.

---

# 11. Embeddings Table

Purpose

Store vector references.

Fields

id

memory_id

embedding_provider

embedding_model

vector_id

created_at

Actual vectors may be stored externally if required.

---

# 12. Settings Table

Purpose

Store application settings.

Fields

key

value

updated_at

Examples

Theme

Language

Preferred AI Provider

Reply Delay

Automation Enabled

---

# 13. Plugin Data Table

Purpose

Store plugin-specific information.

Fields

id

plugin_name

key

value

updated_at

Plugins should never modify core tables directly.

---

# 14. Logs Table

Purpose

Store application logs.

Fields

id

level

component

message

timestamp

Logs should be rotated periodically.

---

# 15. File Attachments Table

Purpose

Store metadata for media.

Fields

id

message_id

type

filename

mime_type

size

storage_path

created_at

Supports

Images

Voice

Documents

Video

Stickers

---

# 16. Database Relationships

Contact

↓

Messages

↓

Summaries

↓

Memory

↓

Goals

↓

Relationship

↓

AI Analysis

↓

Embeddings

One Contact

↓

Many Messages

↓

Many Memories

↓

Many Goals

↓

One Relationship Profile

---

# 17. Repository Pattern

Every table should have a repository.

Examples

ContactRepository

MessageRepository

MemoryRepository

GoalRepository

SummaryRepository

RelationshipRepository

SettingsRepository

Repositories isolate SQL from business logic.

---

# 18. Migration Strategy

Never modify tables manually.

Use versioned migrations.

Example

001_create_contacts

002_create_messages

003_create_memory

004_create_goals

005_relationship

Database upgrades should be reversible whenever possible.

---

# 19. Indexing Strategy

Index frequently queried columns.

Examples

telegram_user_id

contact_id

created_at

message_id

goal_name

Avoid unnecessary indexes.

---

# 20. Backup Strategy

Support

Manual backup

Automatic backup

Incremental backup

Export

Import

Database Version Check

Backups should include schema version information.

---

# 21. Security

Sensitive data should be encrypted where appropriate.

Never store

Passwords

API Keys

Authentication Tokens

Store secrets outside the database.

---

# 22. Future Expansion

Future tables may include

Conversation Analytics

Reminder System

Voice Metadata

Image Metadata

Knowledge Graph

Shared Memories

Cloud Sync

User Profiles

Conversation Templates

Plugin Registry

---

# 23. Database Rules

Every table must have:

Primary Key

Created Timestamp

Updated Timestamp (where applicable)

Indexes when necessary

Foreign Keys where appropriate

Soft delete support where practical

No duplicated data unless justified.

---

# 24. Performance Guidelines

Batch inserts for large imports.

Lazy-load large conversation histories.

Cache frequently accessed records.

Paginate message queries.

Optimize indexes after profiling.

Avoid premature optimization.

---

# 25. Database Philosophy

The database is the single source of truth for persistent application data.

Business rules belong in the Domain Layer, not in SQL.

Every schema change should be documented and accompanied by a migration.