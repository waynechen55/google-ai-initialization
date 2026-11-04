# Security Policy

## Supported Versions

We provide security updates and vulnerability patches for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| 2.0.x   | :white_check_mark: |
| < 2.0   | :x:                |

## Reporting a Vulnerability

The Google AI & Model Context Protocol (MCP) Initializer handles sensitive authentication tokens, Google Cloud Application Default Credentials (ADC), and enterprise API keys. We take the security of these credentials and host environments extremely seriously.

### How to Report

If you discover a security vulnerability or credential handling defect:
1. **DO NOT** open a public issue or discussion on GitHub.
2. Send an email with complete reproduction details, logs (with all tokens redacted), and an impact assessment directly to:
   - **security@example.com**
3. You will receive an acknowledgment within 24 to 48 hours.

### What to Include
- A clear description of the vulnerability (e.g. secret leakage, unauthenticated command injection, unsafe path resolution).
- Steps to reproduce the issue.
- Any relevant logs (ensure all API keys, PAT tokens, and private hostnames are scrubbed).
- A proposed fix or mitigation if available.

### Responsible Disclosure & Invariants
This project adheres to strict security invariants:
- **Zero Plain-Text Secret Logging**: API keys, PATs, and passwords must never be printed to stdout, stderr, or logged to unencrypted disk logs.
- **Clean Settings Invariant**: Instruction metadata must never be written into `.mcp.json` or Gemini CLI settings.
- **Hermetic Credential Isolation**: Cross-domain credential contamination is strictly rejected by domain-scoped verification filters.
