# Getting Started with Daytona

The Daytona SDK provides official Python and TypeScript interfaces for interacting with Daytona, enabling you to programmatically manage development environments and execute code. The Python SDK supports both sync and async programming models.

## Prerequisites

- A Daytona account
- An API key (generated from Daytona Dashboard)
- Python 3.8+ or Node.js 16+ installed locally

## Step 1: Get Your API Key

1. Go to the **Daytona Dashboard**
2. Navigate to Settings or API Keys section
3. Create a new API key
4. **Save it securely** – it won't be shown again

## Step 2: Install the SDK

### Python Installation

```bash
pip install daytona
```

### TypeScript/JavaScript Installation

```bash
npm install @daytonaio/sdk
```

## Step 3: Run Your First Sandbox

### Python Example

```python
from daytona import Daytona

# Initialize the client
daytona = Daytona(api_key="your_api_key")

# Create a sandbox
sandbox = daytona.create_sandbox()

# Execute code
result = sandbox.execute("python -c 'print(\"Hello from Daytona!\")'")
print(result.stdout)

# Clean up
sandbox.destroy()
```

### TypeScript Example

```typescript
import { Daytona } from "@daytonaio/sdk";

// Initialize the client
const daytona = new Daytona({ apiKey: "your_api_key" });

// Create a sandbox
const sandbox = await daytona.createSandbox();

// Execute code
const result = await sandbox.execute('echo "Hello from Daytona!"');
console.log(result.stdout);

// Clean up
await sandbox.destroy();
```

## What You Just Did ✅

- Installed the Daytona SDK
- Created a secure sandbox environment
- Executed code remotely inside that sandbox
- Retrieved and displayed the output locally
- Cleaned up resources

You're now ready to use Daytona for secure, isolated code execution!

## Next Steps

### Connect to an LLM

Use Claude (Anthropic) to generate and execute code:

```python
import anthropic

sandbox = daytona.create_sandbox()

# Ask Claude to generate code
client = anthropic.Anthropic(api_key="your_anthropic_key")
message = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=1024,
    messages=[
        {"role": "user", "content": "Write Python code to calculate factorial of 25"}
    ],
)

code = message.content[0].text

# Execute in sandbox
result = sandbox.execute(f"python -c '{code}'")
print(result)
```

## Daytona CLI

For command-line management of sandboxes:

```bash
# Install CLI
curl -fsSL https://get.daytona.io/install.sh | bash

# Initialize CLI
daytona init

# Create sandbox
daytona create

# List sandboxes
daytona list

# Connect to sandbox
daytona connect <sandbox-id>
```

## Common Issues & Troubleshooting

### Issue: "API key not found"
**Solution**: Ensure `DAYTONA_API_KEY` environment variable is set or passed to SDK initialization.

### Issue: "Sandbox failed to start"
**Solution**: Check network connectivity and API key validity. Verify your API usage limits.

## Additional Resources

- **Python SDK Examples**: [GitHub Repository](https://github.com/daytonaio/sdk)
- **TypeScript SDK Examples**: [GitHub Repository](https://github.com/daytonaio/sdk)
- **Full Documentation**: https://www.daytona.io/docs/en/
