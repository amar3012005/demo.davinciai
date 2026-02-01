# Daytona SDK Reference

## Python SDK

### Daytona Client

Initialize the Daytona client to interact with the platform.

#### Initialization

```python
from daytona import Daytona

# Basic initialization
daytona = Daytona(api_key="your_api_key")

# With custom endpoint
daytona = Daytona(
    api_key="your_api_key",
    api_url="https://custom-endpoint.daytona.io"
)
```

#### Core Methods

##### `create_sandbox()`

Create a new sandbox environment.

```python
sandbox = daytona.create_sandbox()
# Returns a Sandbox object

# With optional configuration
sandbox = daytona.create_sandbox(
    snapshot="daytonaio/sandbox:0.4.3",
    metadata={"customer_id": "123", "project": "tara"}
)
```

##### `list_sandboxes()`

List all active sandboxes.

```python
sandboxes = daytona.list_sandboxes()
for sandbox in sandboxes:
    print(f"Sandbox ID: {sandbox.id}, Status: {sandbox.status}")
```

##### `get_sandbox(sandbox_id)`

Retrieve a specific sandbox.

```python
sandbox = daytona.get_sandbox("sandbox_id_here")
```

### Sandbox Object

A Sandbox is your isolated execution environment.

#### Methods

##### `execute(command, timeout=None)`

Execute a shell command inside the sandbox.

```python
result = sandbox.execute("ls -la")
print(result.stdout)  # Command output
print(result.stderr)  # Error output (if any)
print(result.exit_code)  # Exit code
```

##### `run_code(code, language="python")`

Execute code in a specific language.

```python
result = sandbox.run_code("print('Hello from Daytona')", language="python")
```

##### `upload_file(path, content)`

Upload a file to the sandbox.

```python
sandbox.upload_file("app.py", """
def hello():
    return 'Hello World'
""")
```

##### `download_file(remote_path)`

Download a file from the sandbox.

```python
content = sandbox.download_file("/home/daytona/results.txt")
```

##### `list_files(path="/")`

List files in a directory.

```python
files = sandbox.list_files("/home/daytona")
for file in files:
    print(file.name, file.size)
```

##### `remove_file(path)`

Delete a file from the sandbox.

```python
sandbox.remove_file("/home/daytona/temp.txt")
```

##### `create_directory(path)`

Create a directory in the sandbox.

```python
sandbox.create_directory("/home/daytona/project")
```

##### `get_preview_url(port)`

Get a publicly accessible URL for a running service inside the sandbox.

```python
preview_url = sandbox.get_preview_url(port=3000)
# Access your web app at: https://123abc-sandbox.daytona.io:3000
```

##### `destroy()`

Delete the sandbox and all its resources.

```python
sandbox.destroy()
```

## TypeScript/JavaScript SDK

### Daytona Client

```typescript
import { Daytona } from "@daytonaio/sdk";

// Initialize
const daytona = new Daytona({ apiKey: "your_api_key" });
```

#### Core Methods

##### `createSandbox()`

```typescript
const sandbox = await daytona.createSandbox();

// With configuration
const sandbox = await daytona.createSandbox({
  snapshot: "daytonaio/sandbox:0.4.3",
  metadata: { customerId: "123", project: "tara" }
});
```

##### `listSandboxes()`

```typescript
const sandboxes = await daytona.listSandboxes();
sandboxes.forEach(sandbox => {
  console.log(`Sandbox: ${sandbox.id}, Status: ${sandbox.status}`);
});
```

##### `getSandbox(sandboxId)`

```typescript
const sandbox = await daytona.getSandbox("sandbox_id_here");
```

### Sandbox Object

#### Methods

##### `execute(command, timeout?)`

```typescript
const result = await sandbox.execute("ls -la");
console.log(result.stdout);
console.log(result.stderr);
console.log(result.exitCode);
```

##### `runCode(code, language?)`

```typescript
const result = await sandbox.runCode(
  "console.log('Hello from Daytona')",
  "javascript"
);
```

##### `uploadFile(remotePath, content)`

```typescript
await sandbox.uploadFile("app.ts", `
export function hello() {
  return 'Hello World';
}
`);
```

##### `downloadFile(remotePath)`

```typescript
const content = await sandbox.downloadFile("/home/daytona/results.txt");
```

##### `listFiles(path?)`

```typescript
const files = await sandbox.listFiles("/home/daytona");
files.forEach(file => {
  console.log(`${file.name} (${file.size} bytes)`);
});
```

##### `removeFile(path)`

```typescript
await sandbox.removeFile("/home/daytona/temp.txt");
```

##### `createDirectory(path)`

```typescript
await sandbox.createDirectory("/home/daytona/project");
```

##### `getPreviewUrl(port)`

```typescript
const previewUrl = await sandbox.getPreviewUrl(3000);
console.log(`App at: ${previewUrl}`);
```

##### `destroy()`

```typescript
await sandbox.destroy();
```

## Error Handling

### Python

```python
from daytona import DaytonaException, SandboxError

try:
    sandbox = daytona.create_sandbox()
    result = sandbox.execute("failing_command")
except SandboxError as e:
    print(f"Sandbox error: {e}")
except DaytonaException as e:
    print(f"Daytona error: {e}")
```

### TypeScript

```typescript
try {
  const sandbox = await daytona.createSandbox();
  const result = await sandbox.execute("failing_command");
} catch (error) {
  if (error instanceof SandboxError) {
    console.error("Sandbox error:", error);
  } else {
    console.error("Daytona error:", error);
  }
}
```

## Environment Variables

| Variable | Type | Description |
|----------|------|-------------|
| `DAYTONA_API_KEY` | string | API key for authentication |
| `DAYTONA_API_URL` | string | Custom API endpoint URL |
| `DAYTONA_ENVIRONMENT` | string | `production` or `staging` |
| `DAYTONA_REQUEST_TIMEOUT` | number | Request timeout in milliseconds |

## Rate Limits

Daytona enforces rate limits based on your plan:

- **Free Tier**: 100 API calls per hour, 5 concurrent sandboxes
- **Pro**: Unlimited API calls, 50 concurrent sandboxes
- **Enterprise**: Custom limits based on agreement

## Best Practices

1. **Always clean up**: Call `destroy()` on sandboxes you no longer need
2. **Handle errors gracefully**: Wrap API calls in try-catch
3. **Reuse sandboxes**: Keep long-lived sandboxes if doing multiple operations
4. **Metadata**: Add metadata for tracking and monitoring
5. **Timeouts**: Set reasonable timeouts for long-running commands
