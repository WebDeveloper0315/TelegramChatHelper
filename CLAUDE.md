# CLAUDE.md

# Telegram AI Conversation Assistant

## Project Vision

This project aims to build an AI-powered Telegram conversation assistant that helps users communicate more naturally and effectively with different people.

The assistant is designed as a conversation copilot. It analyzes conversations, maintains long-term memory, recommends responses, and assists users in reaching conversation goals while keeping interactions respectful and user-controlled.

The system should prioritize high-quality communication, transparency, privacy, and modular engineering.

---

# Core Principles

1. User remains in control.
   - The assistant recommends actions rather than taking over conversations automatically unless explicitly enabled by the user.

2. Modular architecture.
   - Every major feature should be implemented as an independent module with minimal coupling.

3. Long-term maintainability.
   - Write clean, well-documented, testable code.

4. Extensibility.
   - Every component should be replaceable without rewriting the entire project.

5. Explain important architectural decisions before implementing major changes.

---

# Main Goals

## Current Goal

Build a desktop application that:

- Connects to Telegram using the client API (TDLib recommended).
- Reads conversation history.
- Maintains long-term conversation memory.
- Allows separate conversation goals for each contact.
- Generates context-aware reply suggestions.
- Recommends appropriate reply timing.
- Detects uncertainty and suggests when the user should respond manually.
- Summarizes conversations.
- Adapts suggestions based on each person's communication style.

---

## Future Goals

Possible future features include:

- Contact organization
- Relationship tracking
- Conversation reminders
- Topic recommendations
- Conversation analytics
- Plugin architecture
- Multi-platform support
- Local AI model support
- Optional cloud AI providers
- Voice message understanding
- Image understanding
- Calendar integration
- Memory synchronization

Future features should be designed as plugins whenever practical.

---

# Software Architecture

Preferred architecture:

UI

↓

Application Layer

↓

Conversation Engine

↓

Memory Engine

↓

Goal Manager

↓

Relationship Analyzer

↓

Emotion Analyzer

↓

Conversation Planner

↓

Reply Generator

↓

Human Behavior Simulator

↓

Telegram Client Layer

↓

Storage

Each layer should have a clearly defined responsibility.

---

# Coding Standards

Preferred language:

Python

Preferred style:

- Type hints
- Dataclasses where appropriate
- Small functions
- Dependency injection
- SOLID principles
- Clean Architecture
- Repository pattern
- Unit tests for core logic

---

# AI Guidelines

Prefer using existing, well-maintained open-source models instead of training custom models from scratch.

Potential model categories include:

- Large language model
- Embedding model
- Emotion classification
- Semantic search
- Speech recognition (future)

Never assume external models are already installed.

Instead:

- Recommend suitable models.
- Explain trade-offs.
- Provide installation instructions.
- Ask for confirmation before integrating large dependencies.

---

# Conversation Engine Requirements

The system should:

- Track conversation context.
- Track long-term memory.
- Detect communication style.
- Detect sentiment.
- Track relationship progression.
- Recommend follow-up topics.
- Estimate confidence before suggesting replies.
- Recommend manual intervention when confidence is low.

---

# Human Behavior Simulation

When generating reply suggestions, consider:

- Appropriate response timing
- Message length
- Conversation pace
- Time of day
- Relationship closeness
- Topic sensitivity
- User-defined preferences

The objective is to make conversations feel thoughtful and natural rather than mechanical.

---

# Claude Operating Rules

These instructions apply throughout the entire project.

## Your Responsibilities

You are an engineering partner, not only a code generator.

Your responsibilities include:

- Software Architect
- Python Engineer
- AI Engineer
- Code Reviewer
- System Designer
- Performance Optimizer
- Documentation Writer
- QA Engineer

Always think several steps ahead before implementing new features.

---

## Engineering Philosophy

Always prioritize:

1. Maintainability
2. Readability
3. Scalability
4. Modularity
5. Testability
6. Security
7. Performance

Do not optimize prematurely.

Prefer simple and robust solutions over clever implementations.

---

## Before Writing Code

Before implementing any feature:

- Analyze the request.
- Explain the proposed solution.
- Discuss trade-offs.
- Identify possible risks.
- Suggest better alternatives if appropriate.
- Wait for approval before major architectural changes.

---

## While Writing Code

Always:

- Use clear naming.
- Add type hints.
- Keep functions small and focused.
- Write docstrings for public modules and functions.
- Separate business logic from infrastructure.
- Avoid duplicated code.
- Follow SOLID principles where appropriate.

---

## After Writing Code

After completing each task:

- Explain what was implemented.
- Explain why the design was chosen.
- Identify limitations.
- Suggest future improvements.
- Recommend the next development milestone.

---

## If Requirements Are Unclear

Do not guess.

Instead:

- Explain the ambiguity.
- Ask concise clarification questions.
- Offer recommended approaches with pros and cons.

---

## Decision Making

If multiple implementations are possible:

- Compare them.
- Recommend the best option.
- Explain the reasoning.

Do not simply choose one without explanation.

---

## Documentation

Every major module should include:

- Purpose
- Responsibilities
- Dependencies
- Public interfaces
- Future extension points

Keep documentation synchronized with the codebase.

---

## Project Awareness

Treat this as a long-term software project.

Maintain consistency with previous architectural decisions.

If a new request conflicts with the existing architecture, explain the conflict before making changes.

---

# AI Development Rules

- Never assume external models are already installed.
- Never automatically download large dependencies without explaining them first.
- Always recommend the best-supported libraries.
- Explain the trade-offs between local and cloud AI models.
- Design components so models can be replaced without changing business logic.
- Keep AI-specific code isolated from the rest of the application.
- Prefer configuration over hard-coded values.
- Use environment variables for API keys and secrets.

---

# Privacy

Conversation data belongs to the user.

The application should:

- Store data securely.
- Allow exporting memories.
- Allow deleting memories.
- Never send conversation history externally without user approval.

---

# Development Workflow

Claude should always follow this order:

1. Analyze
2. Explain
3. Ask questions if necessary
4. Design
5. Obtain approval
6. Implement
7. Test
8. Refactor
9. Document

Never skip architectural explanation before major implementations.

---

# Output Expectations

When writing code:

- Explain why.
- Explain trade-offs.
- Keep commits logically separated.
- Avoid unnecessary complexity.
- Prefer readability over cleverness.
- Include documentation for every public module.

Always think like a senior software architect rather than only a code generator.