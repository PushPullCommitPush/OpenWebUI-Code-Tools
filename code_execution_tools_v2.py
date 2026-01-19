"""
Open WebUI Code Execution Tools v2
==================================
Enhanced code execution toolkit with improved safety, features, and reliability.

Features over v1:
- Session persistence (optional stateful execution)
- File I/O within sandbox
- Resource monitoring
- Better error handling and diagnostics
- Package installation (pip)
- Multiple language support hints
- Execution history/context
- Input validation and sanitization
- Configurable security levels

Compatible with Open WebUI Tools system.
"""

import asyncio
import os
import re
import tempfile
import shutil
import ast
import json
import time
import hashlib
import signal
from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict, Tuple, Union
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager


# ============================================================================
# CONFIGURATION - Valves (Open WebUI Settings UI)
# ============================================================================

@dataclass
class Valves:
    """
    Settings visible in the Open WebUI settings drawer.
    
    Open WebUI requires simple types (str, int, bool) for the UI.
    Lists are stored as comma-separated strings.
    """
    
    # Execution limits
    python_cmd: str = "python3"
    max_execution_time: int = 30
    max_output_lines: int = 150
    max_output_chars: int = 50000
    max_file_size_bytes: int = 10485760  # 10MB (use literal for UI clarity)
    max_files_per_session: int = 50
    
    # Feature flags (show as toggles in UI)
    allow_shell: bool = True
    allow_pip_install: bool = True
    allow_file_persistence: bool = True
    
    # Session management
    session_timeout_minutes: int = 30
    max_sessions: int = 10
    
    # Security - comma-separated strings for UI editability
    blocked_imports: str = "subprocess,multiprocessing,ctypes,_thread"
    blocked_shell_patterns: str = r"rm\s+-rf\s+/,mkfs\.,dd\s+if=,:\(\)\{,>\s*/dev/sd,chmod\s+-R\s+777\s+/"


# Alias for backward compatibility within the code
ExecutionConfig = Valves


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def _truncate_output(text: str, max_lines: int, max_chars: int) -> Tuple[str, bool]:
    """Truncate output with clear indicators."""
    if not text:
        return "", False
    
    truncated = False
    result = text
    
    # Character limit first
    if len(result) > max_chars:
        result = result[:max_chars]
        truncated = True
    
    # Then line limit
    lines = result.splitlines()
    if len(lines) > max_lines:
        result = "\n".join(lines[:max_lines])
        truncated = True
    
    if truncated:
        result += f"\n\n... [OUTPUT TRUNCATED - {len(text)} chars, {len(text.splitlines())} lines total]"
    
    return result, truncated


def _coerce_to_string(value: Any) -> str:
    """Extract string from various input formats LLMs might send."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # Try common keys
        for key in ("code", "command", "text", "input", "script", "content", "source"):
            if key in value and isinstance(value[key], str):
                return value[key]
        # Try to serialize if nothing found
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value)
    return str(value) if value is not None else ""


def _extract_code_block(text: str, lang: str = "python") -> str:
    """Extract code from markdown fenced blocks."""
    if not text:
        return ""
    
    text = text.strip()
    
    # No code blocks
    if "```" not in text:
        return text
    
    # Try language-specific block first
    pattern = rf"```{lang}\s*\n(.*?)```"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    
    # Try generic code block
    match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    # Try inline code
    match = re.search(r"`([^`]+)`", text)
    if match and "\n" not in match.group(1):
        return match.group(1).strip()
    
    return text


def _sanitize_filename(name: str) -> str:
    """Sanitize filename to prevent path traversal."""
    # Remove path components
    name = os.path.basename(name)
    # Remove dangerous characters
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    # Limit length
    if len(name) > 255:
        name = name[:255]
    return name or "unnamed"


def _hash_code(code: str) -> str:
    """Generate short hash for code identification."""
    return hashlib.md5(code.encode()).hexdigest()[:8]


def _check_blocked_patterns(text: str, patterns: List[str]) -> Optional[str]:
    """Check if text matches any blocked patterns."""
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern
    return None


def _check_blocked_imports(code: str, blocked: List[str]) -> List[str]:
    """Check for blocked imports using AST."""
    found = []
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split(".")[0]
                    if module in blocked:
                        found.append(module)
            elif isinstance(node, ast.ImportFrom) and node.module:
                module = node.module.split(".")[0]
                if module in blocked:
                    found.append(module)
    except SyntaxError:
        pass  # Will be caught during execution
    return found


# ============================================================================
# SESSION MANAGEMENT
# ============================================================================

class ExecutionSession:
    """Persistent execution session with isolated filesystem."""
    
    def __init__(self, session_id: str, config: ExecutionConfig):
        self.session_id = session_id
        self.config = config
        self.created_at = time.time()
        self.last_accessed = time.time()
        self.execution_count = 0
        self.temp_dir = tempfile.mkdtemp(prefix=f"owui_session_{session_id}_")
        self.files: Dict[str, str] = {}  # filename -> path mapping
        self.variables: Dict[str, Any] = {}  # Stored context (limited use)
        self.history: List[Dict] = []
        
    def touch(self):
        """Update last access time."""
        self.last_accessed = time.time()
        
    def is_expired(self) -> bool:
        """Check if session has expired."""
        age_minutes = (time.time() - self.last_accessed) / 60
        return age_minutes > self.config.session_timeout_minutes
    
    def add_file(self, filename: str, content: str) -> str:
        """Add a file to the session workspace."""
        safe_name = _sanitize_filename(filename)
        filepath = os.path.join(self.temp_dir, safe_name)
        
        if len(self.files) >= self.config.max_files_per_session:
            raise ValueError(f"Max files ({self.config.max_files_per_session}) reached")
        
        if len(content.encode()) > self.config.max_file_size_bytes:
            raise ValueError(f"File too large (max {self.config.max_file_size_bytes} bytes)")
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        
        self.files[safe_name] = filepath
        return safe_name
    
    def get_file(self, filename: str) -> Optional[str]:
        """Read a file from the session workspace."""
        safe_name = _sanitize_filename(filename)
        filepath = self.files.get(safe_name)
        if filepath and os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                return f.read()
        return None
    
    def list_files(self) -> List[str]:
        """List all files in session workspace."""
        return list(self.files.keys())
    
    def add_history(self, tool: str, input_summary: str, success: bool, duration: float):
        """Record execution in history."""
        self.history.append({
            "tool": tool,
            "input": input_summary[:200],  # Truncate
            "success": success,
            "duration": round(duration, 2),
            "timestamp": datetime.now().isoformat()
        })
        # Keep last 50 entries
        if len(self.history) > 50:
            self.history = self.history[-50:]
    
    def cleanup(self):
        """Remove session directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)


class SessionManager:
    """Manages multiple execution sessions."""
    
    def __init__(self, config: ExecutionConfig):
        self.config = config
        self.sessions: Dict[str, ExecutionSession] = {}
        self._cleanup_counter = 0
    
    def get_or_create(self, session_id: Optional[str] = None) -> ExecutionSession:
        """Get existing session or create new one."""
        self._maybe_cleanup()
        
        if session_id and session_id in self.sessions:
            session = self.sessions[session_id]
            if not session.is_expired():
                session.touch()
                return session
            else:
                session.cleanup()
                del self.sessions[session_id]
        
        # Create new session
        new_id = session_id or hashlib.md5(str(time.time()).encode()).hexdigest()[:12]
        
        # Enforce max sessions
        if len(self.sessions) >= self.config.max_sessions:
            # Remove oldest
            oldest_id = min(self.sessions, key=lambda k: self.sessions[k].last_accessed)
            self.sessions[oldest_id].cleanup()
            del self.sessions[oldest_id]
        
        session = ExecutionSession(new_id, self.config)
        self.sessions[new_id] = session
        return session
    
    def _maybe_cleanup(self):
        """Periodically clean up expired sessions."""
        self._cleanup_counter += 1
        if self._cleanup_counter % 10 == 0:  # Every 10 operations
            expired = [sid for sid, s in self.sessions.items() if s.is_expired()]
            for sid in expired:
                self.sessions[sid].cleanup()
                del self.sessions[sid]


# ============================================================================
# EXECUTION ENGINE
# ============================================================================

async def _run_subprocess(
    cmd: Union[List[str], str],
    cwd: str,
    timeout: int,
    is_shell: bool = False,
    env: Optional[Dict[str, str]] = None
) -> Tuple[int, str, str]:
    """Execute subprocess with timeout and resource awareness."""
    
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    
    try:
        if is_shell:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
            )
        
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
            return proc.returncode or 0, stdout, stderr
            
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except:
                pass
            return -1, "", f"⏱️ Execution timed out after {timeout}s"
            
    except FileNotFoundError as e:
        return -1, "", f"Command not found: {e}"
    except PermissionError as e:
        return -1, "", f"Permission denied: {e}"
    except Exception as e:
        return -1, "", f"Execution error: {type(e).__name__}: {e}"


# ============================================================================
# OUTPUT FORMATTING
# ============================================================================

class OutputFormatter:
    """Formats tool output consistently."""
    
    def __init__(self, config: ExecutionConfig):
        self.config = config
    
    def format_result(
        self,
        exit_code: int,
        stdout: str,
        stderr: str,
        execution_time: float,
        extra_info: Optional[Dict] = None
    ) -> str:
        """Format execution result for LLM consumption."""
        
        parts = []
        
        # Status indicator
        if exit_code == 0:
            parts.append("✅ **Status:** Success")
        elif exit_code == -1:
            parts.append("⚠️ **Status:** Error/Timeout")
        else:
            parts.append(f"❌ **Status:** Failed (exit code {exit_code})")
        
        # Timing
        parts.append(f"⏱️ **Duration:** {execution_time:.2f}s")
        
        # Extra info
        if extra_info:
            for key, value in extra_info.items():
                parts.append(f"📋 **{key}:** {value}")
        
        # Output
        if stdout:
            truncated_stdout, was_truncated = _truncate_output(
                stdout, self.config.max_output_lines, self.config.max_output_chars
            )
            parts.append(f"**stdout:**\n```\n{truncated_stdout}\n```")
        
        if stderr:
            truncated_stderr, _ = _truncate_output(
                stderr, self.config.max_output_lines // 2, self.config.max_output_chars // 2
            )
            parts.append(f"**stderr:**\n```\n{truncated_stderr}\n```")
        
        # Synthesis prompt
        parts.append("\n---\n**💡 SYNTHESIS:** Summarize the goal, key results, interpretation, and next steps.")
        
        return "\n\n".join(parts)
    
    def format_error(self, error_type: str, message: str, suggestion: Optional[str] = None) -> str:
        """Format error message."""
        parts = [f"❌ **{error_type}:** {message}"]
        if suggestion:
            parts.append(f"💡 **Suggestion:** {suggestion}")
        return "\n\n".join(parts)
    
    def format_info(self, title: str, items: Dict[str, Any]) -> str:
        """Format informational output."""
        parts = [f"📋 **{title}**\n"]
        for key, value in items.items():
            if isinstance(value, list):
                parts.append(f"- **{key}:**")
                for item in value:
                    parts.append(f"  - {item}")
            else:
                parts.append(f"- **{key}:** {value}")
        return "\n".join(parts)


# ============================================================================
# MAIN TOOLS CLASS
# ============================================================================

class Tools:
    """
    Enhanced Code Execution Tools for Open WebUI.
    
    Provides safe, feature-rich code execution with:
    - Python execution with import validation
    - Shell command execution with pattern blocking  
    - Syntax checking and linting
    - Dependency verification
    - Package installation
    - Session-based file persistence
    - Execution history tracking
    """
    
    def __init__(self):
        # Open WebUI looks for self.valves specifically
        self.valves = Valves()
        self.session_manager = SessionManager(self.valves)
        self.formatter = OutputFormatter(self.valves)
        self._run_count = 0
    
    def _get_blocked_imports(self) -> List[str]:
        """Parse blocked imports from comma-separated string."""
        return [i.strip() for i in self.valves.blocked_imports.split(",") if i.strip()]
    
    def _get_blocked_shell_patterns(self) -> List[str]:
        """Parse blocked shell patterns from comma-separated string."""
        return [p.strip() for p in self.valves.blocked_shell_patterns.split(",") if p.strip()]
    
    def _get_input(self, code: str = "", text: str = "", **kwargs) -> Optional[str]:
        """Extract and clean input from various formats."""
        raw = _coerce_to_string(code) or _coerce_to_string(text) or _coerce_to_string(kwargs)
        if not raw.strip():
            return None
        return _extract_code_block(raw)

    # -------------------------------------------------------------------------
    # PYTHON EXECUTION
    # -------------------------------------------------------------------------
    
    async def exec_python(
        self,
        code: str = "",
        text: str = "",
        session_id: str = "",
        save_as: str = "",
        **kwargs
    ) -> str:
        """
        Execute Python code safely.
        
        Args:
            code/text: Python code to execute (accepts markdown code blocks)
            session_id: Optional session ID for file persistence
            save_as: Optional filename to save the script in session
        
        Returns:
            Formatted execution result with stdout, stderr, and status.
        
        Example:
            run_python(code="print('Hello, World!')")
            run_python(text="```python\nimport math\nprint(math.pi)\n```")
        """
        start_time = time.time()
        
        python_code = self._get_input(code, text, **kwargs)
        if not python_code:
            return self.formatter.format_error(
                "Input Error", 
                "No code provided",
                "Pass code as `code` or `text` parameter, optionally in a markdown code block"
            )
        
        # Security check - blocked imports
        blocked = _check_blocked_imports(python_code, self._get_blocked_imports())
        if blocked:
            return self.formatter.format_error(
                "Security Error",
                f"Blocked imports detected: {', '.join(blocked)}",
                "Use run_shell for subprocess operations"
            )
        
        # Get or create session
        session = self.session_manager.get_or_create(session_id or None)
        session.execution_count += 1
        self._run_count += 1
        
        # Create script file
        script_name = save_as or f"script_{self._run_count}.py"
        script_path = os.path.join(session.temp_dir, _sanitize_filename(script_name))
        
        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(python_code)
            
            if save_as:
                session.files[_sanitize_filename(save_as)] = script_path
            
            # Execute
            exit_code, stdout, stderr = await _run_subprocess(
                [self.valves.python_cmd, script_path],
                session.temp_dir,
                self.valves.max_execution_time
            )
            
            duration = time.time() - start_time
            success = exit_code == 0
            
            session.add_history("exec_python", python_code[:100], success, duration)
            
            extra = {"Session": session.session_id}
            if save_as:
                extra["Saved as"] = save_as
            
            return self.formatter.format_result(exit_code, stdout, stderr, duration, extra)
            
        except Exception as e:
            duration = time.time() - start_time
            session.add_history("exec_python", python_code[:100], False, duration)
            return self.formatter.format_error("Execution Error", str(e))

    # -------------------------------------------------------------------------
    # SHELL EXECUTION
    # -------------------------------------------------------------------------
    
    async def exec_shell(
        self,
        command: str = "",
        text: str = "",
        session_id: str = "",
        **kwargs
    ) -> str:
        """
        Execute shell commands.
        
        Args:
            command/text: Shell command(s) to execute
            session_id: Optional session ID for working directory persistence
        
        Returns:
            Formatted execution result.
        
        Example:
            run_shell(command="echo 'Hello' && pwd")
            run_shell(text="ls -la")
        """
        start_time = time.time()
        
        if not self.valves.allow_shell:
            return self.formatter.format_error(
                "Disabled",
                "Shell execution is disabled in configuration"
            )
        
        cmd = _coerce_to_string(command) or _coerce_to_string(text)
        if not cmd.strip():
            return self.formatter.format_error("Input Error", "No command provided")
        
        # Security check - blocked patterns
        blocked = _check_blocked_patterns(cmd, self._get_blocked_shell_patterns())
        if blocked:
            return self.formatter.format_error(
                "Security Error",
                f"Blocked pattern detected: {blocked}",
                "This command pattern is not allowed for safety reasons"
            )
        
        session = self.session_manager.get_or_create(session_id or None)
        
        exit_code, stdout, stderr = await _run_subprocess(
            cmd,
            session.temp_dir,
            self.valves.max_execution_time,
            is_shell=True
        )
        
        duration = time.time() - start_time
        session.add_history("exec_shell", cmd[:100], exit_code == 0, duration)
        
        return self.formatter.format_result(
            exit_code, stdout, stderr, duration,
            {"Session": session.session_id, "CWD": session.temp_dir}
        )

    # -------------------------------------------------------------------------
    # LINTING
    # -------------------------------------------------------------------------
    
    async def exec_lint(
        self,
        code: str = "",
        text: str = "",
        **kwargs
    ) -> str:
        """
        Check Python code for syntax errors and style issues.
        
        Uses py_compile for syntax and ruff for style (if available).
        
        Args:
            code/text: Python code to lint
        
        Returns:
            Lint results with syntax and style feedback.
        """
        start_time = time.time()
        
        python_code = self._get_input(code, text, **kwargs)
        if not python_code:
            return self.formatter.format_error("Input Error", "No code provided")
        
        temp_dir = tempfile.mkdtemp(prefix="lint_")
        results = []
        
        try:
            filepath = os.path.join(temp_dir, "check.py")
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(python_code)
            
            # Syntax check
            exit_code, _, stderr = await _run_subprocess(
                [self.valves.python_cmd, "-m", "py_compile", filepath],
                temp_dir, 10
            )
            
            if exit_code == 0:
                results.append("✅ **Syntax:** Valid")
            else:
                # Clean up error message
                error_msg = stderr.replace(filepath, "<code>")
                results.append(f"❌ **Syntax Error:**\n```\n{error_msg}\n```")
            
            # AST analysis for additional insights
            try:
                tree = ast.parse(python_code)
                stats = {
                    "functions": len([n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]),
                    "classes": len([n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]),
                    "imports": len([n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]),
                    "lines": len(python_code.splitlines())
                }
                results.append(f"📊 **Stats:** {stats['lines']} lines, {stats['functions']} functions, {stats['classes']} classes, {stats['imports']} imports")
            except:
                pass
            
            # Ruff check (if available)
            ruff_check, _, _ = await _run_subprocess(
                "command -v ruff", temp_dir, 5, is_shell=True
            )
            
            if ruff_check == 0:
                rc, rout, rerr = await _run_subprocess(
                    ["ruff", "check", "--output-format=text", filepath],
                    temp_dir, 15
                )
                if rc == 0:
                    results.append("✅ **Ruff:** No issues")
                else:
                    # Clean paths from output
                    issues = (rout or rerr).replace(filepath, "<code>")
                    truncated, _ = _truncate_output(issues, 30, 2000)
                    results.append(f"⚠️ **Ruff Issues:**\n```\n{truncated}\n```")
            else:
                results.append("ℹ️ **Ruff:** Not installed (style check skipped)")
            
            duration = time.time() - start_time
            results.append(f"\n⏱️ Completed in {duration:.2f}s")
            results.append("\n---\n**💡 SYNTHESIS:** Review findings and suggest fixes.")
            
            return "\n\n".join(results)
            
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # DEPENDENCY CHECKING
    # -------------------------------------------------------------------------
    
    async def exec_check_deps(
        self,
        code: str = "",
        text: str = "",
        **kwargs
    ) -> str:
        """
        Check if Python dependencies are available.
        
        Parses imports from code using AST and verifies each can be imported.
        
        Args:
            code/text: Python code with import statements
        
        Returns:
            List of dependencies with availability status and versions.
        """
        start_time = time.time()
        
        python_code = self._get_input(code, text, **kwargs)
        if not python_code:
            return self.formatter.format_error("Input Error", "No code provided")
        
        # Parse imports
        imports = set()
        try:
            tree = ast.parse(python_code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
        except SyntaxError as e:
            return self.formatter.format_error(
                "Syntax Error",
                f"Cannot parse imports: {e}",
                "Fix syntax errors first using lint_python"
            )
        
        if not imports:
            return "ℹ️ No imports found in the provided code."
        
        # Check each import
        results = ["📦 **Dependency Check**\n"]
        available = []
        missing = []
        
        for imp in sorted(imports):
            # Try to import and get version
            check_script = f"""
import sys
try:
    import {imp}
    version = getattr({imp}, '__version__', getattr({imp}, 'VERSION', 'unknown'))
    print(f"OK|{{version}}")
except ImportError as e:
    print(f"FAIL|{{e}}")
"""
            exit_code, stdout, _ = await _run_subprocess(
                [self.valves.python_cmd, "-c", check_script],
                ".", 5
            )
            
            output = stdout.strip()
            if output.startswith("OK|"):
                version = output.split("|", 1)[1]
                available.append(f"✅ `{imp}` (v{version})")
            else:
                error = output.split("|", 1)[1] if "|" in output else "not found"
                missing.append(f"❌ `{imp}` - {error}")
        
        if available:
            results.append("**Available:**")
            results.extend(available)
        
        if missing:
            results.append("\n**Missing:**")
            results.extend(missing)
            results.append(f"\n💡 Install missing: `pip install {' '.join(m.split('`')[1] for m in missing)}`")
        
        duration = time.time() - start_time
        results.append(f"\n⏱️ Checked {len(imports)} packages in {duration:.2f}s")
        results.append("\n---\n**💡 SYNTHESIS:** Note availability and suggest installations if needed.")
        
        return "\n".join(results)

    # -------------------------------------------------------------------------
    # PACKAGE INSTALLATION
    # -------------------------------------------------------------------------
    
    async def exec_pip_install(
        self,
        packages: str = "",
        text: str = "",
        **kwargs
    ) -> str:
        """
        Install Python packages using pip.
        
        Args:
            packages/text: Space or comma separated package names
        
        Returns:
            Installation result.
        
        Example:
            pip_install(packages="requests pandas")
            pip_install(text="numpy, scipy")
        """
        if not self.valves.allow_pip_install:
            return self.formatter.format_error(
                "Disabled",
                "Package installation is disabled in configuration"
            )
        
        raw = _coerce_to_string(packages) or _coerce_to_string(text)
        if not raw.strip():
            return self.formatter.format_error("Input Error", "No packages specified")
        
        # Parse package names
        pkg_list = [p.strip() for p in re.split(r'[,\s]+', raw) if p.strip()]
        
        # Basic validation
        invalid = [p for p in pkg_list if not re.match(r'^[a-zA-Z0-9_\-\[\]<>=!.]+$', p)]
        if invalid:
            return self.formatter.format_error(
                "Invalid Package Names",
                f"Invalid characters in: {', '.join(invalid)}"
            )
        
        start_time = time.time()
        
        # Install
        cmd = [self.valves.python_cmd, "-m", "pip", "install", "--user", "--quiet"] + pkg_list
        exit_code, stdout, stderr = await _run_subprocess(
            cmd, ".", min(60, self.valves.max_execution_time * 2)
        )
        
        duration = time.time() - start_time
        
        if exit_code == 0:
            return self.formatter.format_result(
                exit_code, 
                f"Successfully installed: {', '.join(pkg_list)}", 
                stderr,
                duration,
                {"Packages": len(pkg_list)}
            )
        else:
            return self.formatter.format_result(exit_code, stdout, stderr, duration)

    # -------------------------------------------------------------------------
    # FILE OPERATIONS
    # -------------------------------------------------------------------------
    
    async def exec_write_file(
        self,
        filename: str = "",
        content: str = "",
        text: str = "",
        session_id: str = "",
        **kwargs
    ) -> str:
        """
        Write content to a file in the session workspace.
        
        Args:
            filename: Name of file to create
            content/text: Content to write
            session_id: Session ID for persistence
        
        Returns:
            Confirmation with file details.
        """
        if not self.valves.allow_file_persistence:
            return self.formatter.format_error("Disabled", "File persistence is disabled")
        
        fname = _coerce_to_string(filename) or _coerce_to_string(kwargs.get("name", ""))
        file_content = _coerce_to_string(content) or _coerce_to_string(text)
        
        if not fname:
            return self.formatter.format_error("Input Error", "No filename provided")
        if not file_content:
            return self.formatter.format_error("Input Error", "No content provided")
        
        session = self.session_manager.get_or_create(session_id or None)
        
        try:
            saved_name = session.add_file(fname, file_content)
            return self.formatter.format_info("File Written", {
                "Filename": saved_name,
                "Size": f"{len(file_content)} chars, {len(file_content.encode())} bytes",
                "Session": session.session_id,
                "Total files": len(session.files)
            })
        except ValueError as e:
            return self.formatter.format_error("File Error", str(e))
    
    async def exec_read_file(
        self,
        filename: str = "",
        text: str = "",
        session_id: str = "",
        **kwargs
    ) -> str:
        """
        Read a file from the session workspace.
        
        Args:
            filename/text: Name of file to read
            session_id: Session ID
        
        Returns:
            File contents or error.
        """
        fname = _coerce_to_string(filename) or _coerce_to_string(text)
        if not fname:
            return self.formatter.format_error("Input Error", "No filename provided")
        
        session = self.session_manager.get_or_create(session_id or None)
        content = session.get_file(fname)
        
        if content is None:
            available = session.list_files()
            return self.formatter.format_error(
                "File Not Found",
                f"'{fname}' not found in session",
                f"Available files: {', '.join(available) or 'none'}"
            )
        
        truncated, was_truncated = _truncate_output(
            content, self.valves.max_output_lines, self.valves.max_output_chars
        )
        
        result = [f"📄 **File:** {fname}\n```\n{truncated}\n```"]
        if was_truncated:
            result.append("⚠️ Content was truncated")
        
        return "\n".join(result)
    
    async def exec_list_files(
        self,
        session_id: str = "",
        **kwargs
    ) -> str:
        """
        List all files in the session workspace.
        
        Args:
            session_id: Session ID
        
        Returns:
            List of files with details.
        """
        session = self.session_manager.get_or_create(session_id or None)
        files = session.list_files()
        
        if not files:
            return f"ℹ️ No files in session `{session.session_id}`"
        
        file_info = []
        for fname in sorted(files):
            filepath = session.files[fname]
            if os.path.exists(filepath):
                size = os.path.getsize(filepath)
                file_info.append(f"- `{fname}` ({size} bytes)")
            else:
                file_info.append(f"- `{fname}` (missing)")
        
        return self.formatter.format_info(f"Session Files ({session.session_id})", {
            "Files": file_info,
            "Total": len(files),
            "Working directory": session.temp_dir
        })

    # -------------------------------------------------------------------------
    # SESSION INFO
    # -------------------------------------------------------------------------
    
    async def exec_session_info(
        self,
        session_id: str = "",
        **kwargs
    ) -> str:
        """
        Get information about the current session.
        
        Args:
            session_id: Session ID (creates new if not found)
        
        Returns:
            Session details including history and files.
        """
        session = self.session_manager.get_or_create(session_id or None)
        
        age_minutes = (time.time() - session.created_at) / 60
        idle_minutes = (time.time() - session.last_accessed) / 60
        
        info = {
            "Session ID": session.session_id,
            "Created": f"{age_minutes:.1f} minutes ago",
            "Last active": f"{idle_minutes:.1f} minutes ago",
            "Executions": session.execution_count,
            "Files": len(session.files),
            "Working directory": session.temp_dir,
        }
        
        if session.history:
            recent = session.history[-5:]
            info["Recent history"] = [
                f"{h['tool']}: {'✅' if h['success'] else '❌'} ({h['duration']}s)"
                for h in recent
            ]
        
        return self.formatter.format_info("Session Info", info)

    # -------------------------------------------------------------------------
    # ENVIRONMENT INFO
    # -------------------------------------------------------------------------
    
    async def exec_env_info(self, **kwargs) -> str:
        """
        Get information about the execution environment.
        
        Returns:
            Python version, available tools, and system info.
        """
        info = {}
        
        # Python version
        exit_code, stdout, _ = await _run_subprocess(
            [self.valves.python_cmd, "--version"], ".", 5
        )
        info["Python"] = stdout.strip() if exit_code == 0 else "unknown"
        
        # Pip version
        exit_code, stdout, _ = await _run_subprocess(
            [self.valves.python_cmd, "-m", "pip", "--version"], ".", 5
        )
        if exit_code == 0:
            info["Pip"] = stdout.split()[1] if stdout else "unknown"
        
        # Check for common tools
        tools_to_check = ["ruff", "black", "mypy", "git", "node", "npm"]
        available_tools = []
        for tool in tools_to_check:
            exit_code, _, _ = await _run_subprocess(
                f"command -v {tool}", ".", 3, is_shell=True
            )
            if exit_code == 0:
                available_tools.append(tool)
        
        info["Available tools"] = available_tools or ["none detected"]
        
        # Configuration
        info["Config"] = {
            "Max execution time": f"{self.valves.max_execution_time}s",
            "Shell enabled": self.valves.allow_shell,
            "Pip install enabled": self.valves.allow_pip_install,
            "File persistence": self.valves.allow_file_persistence,
        }
        
        # Active sessions
        info["Active sessions"] = len(self.session_manager.sessions)
        
        return self.formatter.format_info("Environment Info", info)
