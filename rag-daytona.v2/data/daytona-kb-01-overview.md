# Daytona Overview

## What is Daytona?

Daytona is a secure, elastic infrastructure platform built specifically for running AI-generated code. It provides sandboxed environments where AI agents can safely execute code, manage files, run terminal commands, and interact with tools—all in isolated, ephemeral containers.

## Key Concepts

### Daytona Sandboxes

A Daytona Sandbox is a lightweight, isolated execution environment that:
- Starts in under 90 milliseconds
- Provides full filesystem access, terminal, and Docker support
- Offers strong security through process isolation and resource limits
- Supports stateless or stateful configurations
- Can be spun up per-agent, per-customer, or per-workload

### Agent-Native Infrastructure

Daytona is designed from the ground up for AI agents. It exposes:
- **Official SDKs** in Python and TypeScript for programmatic control
- **High-concurrency APIs** for managing many sandboxes simultaneously
- **Tool integration** allowing agents to spin up environments, execute code, manage Git, and persist state
- **Multi-runtime support** in TypeScript (Node.js, Deno, Bun, browsers, serverless platforms)

## Core Features

1. **Fast Environment Creation**: Sub-90ms sandbox startup time
2. **Secure Code Execution**: Sandboxed environments prevent code from accessing host systems
3. **Multi-language Support**: Python, TypeScript, Node.js, and more
4. **Docker & Compose Support**: Run your full stack inside a sandbox
5. **File & Terminal Access**: Full filesystem and shell capabilities
6. **Stateful Persistence**: Long-lived or ephemeral sandboxes
7. **Scalable**: Supports high-concurrency agent workloads
8. **Enterprise-Grade Security**: Resource limits, isolation, compliance-ready

## Use Cases

- **AI Code Generation**: LLMs generating and executing code safely
- **AI Agents**: Autonomous agents that can run code as part of their workflows
- **Code Review & Analysis**: AI-powered code analysis in isolated environments
- **Development Environments**: AI-assisted development with instant sandbox access
- **Testing & Evaluation**: Safe testing of AI-generated solutions
- **Secure Agent Runtimes**: Production-grade execution layer for agentic workflows

## Architecture at a Glance

```
User/Agent Code
    ↓
Daytona SDK (Python/TypeScript)
    ↓
Daytona API
    ↓
Runner Service
    ↓
Sandboxed Container
    ↓
Code Execution (Isolated)
```

## Getting Started

1. Create an account on Daytona Dashboard
2. Generate an API key
3. Install the appropriate SDK (Python or TypeScript)
4. Write your code using the SDK
5. Deploy and execute in a sandbox

## Why Daytona?

- **Purpose-built for AI agents**, not retrofitted for them
- **Enterprise-grade security** with sandbox isolation
- **Production-ready**, used by leading AI teams
- **Flexible deployment** (cloud, self-hosted, on-premises)
- **Developer-friendly** with simple SDKs and clear documentation
