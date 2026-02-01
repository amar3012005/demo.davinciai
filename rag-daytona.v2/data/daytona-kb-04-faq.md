# Daytona FAQ

## General Questions

### Q: What is Daytona?

**A:** Daytona is a secure, elastic infrastructure platform purpose-built for running AI-generated code. It provides sandboxed environments where AI agents can safely execute code, manage files, and interact with tools in isolated, ephemeral containers with sub-90ms startup times.

### Q: Who should use Daytona?

**A:** Daytona is ideal for:
- AI teams building agentic systems
- LLM-powered code generation platforms
- AI-assisted development tools
- Teams running AI-generated code in production
- Anyone needing secure code execution sandboxes

### Q: Is Daytona open source?

**A:** Yes, Daytona has an open-source version available on GitHub. You can self-host it, and there's also a managed cloud version (Daytona Cloud) for easier deployment.

### Q: How is Daytona different from Docker or VMs?

**A:** 
- **Docker**: Great for deployment, but not optimized for rapid AI agent iteration
- **VMs**: Heavier, slower to start, overkill for sandboxed code execution
- **Daytona**: Purpose-built for AI agents with:
  - Sub-90ms sandbox creation
  - Agent-native SDKs
  - Designed for high-concurrency workloads
  - Enterprise security out of the box

## Technical Questions

### Q: How fast is sandbox creation?

**A:** Sub-90 milliseconds. This enables rapid iteration and high-throughput agent workloads.

### Q: How secure are Daytona sandboxes?

**A:** Daytona uses:
- **Process isolation**: Sandboxes can't access the host system
- **Resource limits**: CPU, memory, disk quotas per sandbox
- **Network isolation**: Controlled outbound/inbound access
- **Immutable base**: Sandboxes start from clean snapshots
- **Enterprise compliance**: SOC 2, HIPAA-ready infrastructure

### Q: Can I run Docker inside Daytona?

**A:** Yes. Daytona sandboxes support Docker and Docker Compose, allowing you to run containerized workloads inside sandboxes.

### Q: Can I use my own Docker images?

**A:** Yes. You can:
1. Use custom snapshots from your registry
2. Push images to Daytona's internal registry
3. Reference them when creating sandboxes

### Q: How do I persist data across sandbox runs?

**A:** Daytona supports:
- **S3/MinIO**: Object storage for files
- **Volumes**: Persistent data volumes (when not ephemeral)
- **Database**: Store data in external databases
- **State management**: SDKs handle state between calls

## API & Integration

### Q: Which SDKs does Daytona provide?

**A:** Official SDKs for:
- **Python** (sync and async)
- **TypeScript/JavaScript** (supports Node.js, Deno, Bun, browsers, serverless)

### Q: Can I use Daytona with LangChain?

**A:** Yes. Daytona integrates with LangChain. See LangChain documentation for the Daytona provider.

### Q: How do I authenticate with Daytona?

**A:** Using API keys:
1. Generate in Daytona Dashboard
2. Set as environment variable: `DAYTONA_API_KEY`
3. Or pass to SDK initialization

### Q: What's the rate limit?

**A:** Depends on your plan:
- **Free**: ~100 API calls/hour
- **Pro**: Higher limits, scales with usage
- **Enterprise**: Custom limits

### Q: Can I use Daytona from serverless (AWS Lambda, Cloudflare Workers)?

**A:** Yes. The TypeScript SDK supports serverless runtimes. Python SDK requires a Node.js environment or Python runtime.

## Deployment & Operations

### Q: Can I self-host Daytona?

**A:** Yes. The open-source version can be deployed:
- Locally with Docker Compose
- On Kubernetes
- On any cloud infrastructure

### Q: What are the hardware requirements for self-hosting?

**A:** Minimum:
- 4 CPU cores
- 8 GB RAM
- 50 GB disk

Scales with sandbox concurrency and workload size.

### Q: What's the recommended sandbox concurrency?

**A:** Depends on your infrastructure:
- **Development**: 5-10 concurrent sandboxes
- **Small team**: 20-50 sandboxes
- **Large scale**: 100+ (requires scaling infrastructure)

## Troubleshooting

### Q: My sandbox fails to start. What do I do?

**A:** Check:
1. API key validity
2. Network connectivity to Daytona API
3. API rate limits
4. Snapshot image availability
5. Resource quotas not exceeded

### Q: Preview URL is not accessible

**A:** Verify:
1. Port is actually running in sandbox
2. Firewall/security group allows access
3. DNS/proxy correctly configured
4. Service is listening on `0.0.0.0` not `localhost`

### Q: How do I debug sandbox execution?

**A:** Use:
```python
# Capture detailed output
result = sandbox.execute("command", timeout=30)
print(result.stdout)
print(result.stderr)
print(f"Exit code: {result.exit_code}")
```

## Security & Compliance

### Q: Is Daytona secure for running untrusted code?

**A:** Yes. Each sandbox is:
- Completely isolated from other sandboxes
- Unable to access host filesystem
- Subject to resource limits
- Network-sandboxed

### Q: Does Daytona support compliance requirements?

**A:** Daytona Cloud supports:
- **SOC 2** attestation
- **HIPAA** compliance (with Business Associate Agreement)
- **GDPR** compliance
- **ISO 27001** certified infrastructure

### Q: Can I encrypt data in Daytona?

**A:** Yes:
- **In transit**: All API calls use HTTPS
- **At rest**: Configure S3/storage encryption
- **In sandboxes**: Use application-level encryption

## Support & Community

### Q: Where can I get help?

**A:** 
- **Documentation**: https://www.daytona.io/docs/en/
- **GitHub Issues**: https://github.com/daytonaio/daytona/issues
- **Discord Community**: Community server (link on website)
- **Enterprise Support**: For paid plans

### Q: How do I report a security vulnerability?

**A:** Email: security@daytona.io

### Q: How can I contribute?

**A:** Daytona is open source! Contributions welcome at https://github.com/daytonaio/daytona
