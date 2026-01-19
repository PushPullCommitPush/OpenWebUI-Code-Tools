# 🛠️ Code Execution Tools v2

**Enhanced code execution toolkit for Open WebUI with sessions, file I/O, and security controls.**

---

## Features

| Feature | Description |
|---------|-------------|
| 🐍 **Python Execution** | Run scripts with full stdout/stderr capture |
| 💻 **Shell Commands** | Execute system commands safely |
| 📦 **Package Management** | Check dependencies, install via pip |
| 📁 **File Persistence** | Read/write files across session calls |
| 🔒 **Security Controls** | Blocked imports, pattern filtering, timeouts |
| ⚙️ **Configurable** | All settings exposed via Valves UI |

---

## Tools (10 total)

All tools use `exec_` prefix to avoid collision with Open WebUI's native Code Interpreter:

```
exec_python       - Execute Python code
exec_shell        - Execute shell commands  
exec_lint         - Syntax + Ruff style check
exec_check_deps   - Verify package availability
exec_pip_install  - Install packages
exec_write_file   - Save to session workspace
exec_read_file    - Read from session workspace
exec_list_files   - List session files
exec_session_info - Session details + history
exec_env_info     - Environment information
```

---

## Quick Start

1. **Upload** `code_execution_tools_v2.py` to Workspace → Tools
2. **Add** the system prompt to your model configuration
3. **Test** with: *"Test your code execution tools"*

---

## Session Example

```python
# All calls with same session_id share files
exec_write_file(filename="data.txt", content="hello", session_id="demo")
exec_python(code="print(open('data.txt').read())", session_id="demo")
# Output: hello
```

---

## Valves (Settings UI)

| Setting | Default | Description |
|---------|---------|-------------|
| `max_execution_time` | 30 | Timeout in seconds |
| `allow_shell` | ✅ | Enable shell commands |
| `allow_pip_install` | ✅ | Enable package installation |
| `allow_file_persistence` | ✅ | Enable session files |
| `blocked_imports` | subprocess,... | Comma-separated blocklist |
| `blocked_shell_patterns` | rm -rf /,... | Dangerous command patterns |

---

## Requirements

- **Python 3.7+** (standard library only)
- **Optional:** `ruff` for style checking

---

## Security

- ⛔ Blocked imports: `subprocess`, `multiprocessing`, `ctypes`, `_thread`
- ⛔ Blocked shell: `rm -rf /`, fork bombs, device writes
- ⏱️ 30s timeout per execution
- 📄 10MB max file size
- 🗂️ Isolated temp directories per session

---

## Links

- **System Prompt:** Included in download
- **Issues/Feedback:** [your link here]

---

*No external dependencies. Just upload and go.* 🚀
