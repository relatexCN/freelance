"""
Tool Execution Engine: Handles tool registration and execution.

Provides:
- Tool registration decorator
- Secure command execution with whitelist
- File operations
- Result formatting for LLM consumption
"""

import asyncio
import json
import logging
import os
import subprocess
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("link.tool_executor")

# Security whitelist for commands
ALLOWED_COMMANDS = {
    "ls", "cat", "echo", "pwd", "whoami", "date", "uptime",
    "head", "tail", "wc", "du", "df", "ps", "top", "openclaw"
}

# Security: Restrict file operations to specific base directories
def _get_allowed_dirs() -> List[str]:
    """Dynamically get allowed base directories."""
    return [
        os.path.normpath(os.path.expanduser("~/.openclaw/workspace")).lower(),
        os.path.normpath(os.path.expanduser("~/workspace")).lower(),
        os.path.normpath(os.getcwd()).lower(),
        os.path.normpath(os.path.dirname(os.path.dirname(__file__))).lower()  # Project root
    ]

def _is_path_allowed(path: str) -> bool:
    """Check if path is within allowed directories."""
    # Use abspath to resolve relative paths like "."
    normalized_path = os.path.normpath(os.path.abspath(os.path.expanduser(path))).lower()
    allowed_dirs = _get_allowed_dirs()
    
    # Check if path starts with any allowed directory
    for base in allowed_dirs:
        if normalized_path.startswith(base) or normalized_path == base:
            return True
    
    # Also check if the path is a subdirectory
    for base in allowed_dirs:
        if normalized_path.startswith(base + os.sep):
            return True
    
    return False


class ToolExecutor:
    """Central registry and executor for tools."""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools = {}
        return cls._instance
    
    def register(self, name: str, description: str, func: Callable):
        """Register a tool with name, description, and implementation."""
        self._tools[name] = {
            "description": description,
            "func": func
        }
        logger.info(f"[TOOL] Registered tool: {name}")
    
    def get_tools(self) -> List[Dict[str, str]]:
        """Get tool definitions for LLM tool calling."""
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": info["description"],
                    "parameters": {}  # Simplified; can be expanded with JSON schema
                }
            }
            for name, info in self._tools.items()
        ]
    
    async def execute(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        """Execute a registered tool with given arguments."""
        if tool_name not in self._tools:
            return {
                "success": False,
                "error": f"Unknown tool: {tool_name}",
                "result": None
            }
        
        try:
            func = self._tools[tool_name]["func"]
            
            # Check if function is async
            if asyncio.iscoroutinefunction(func):
                result = await func(**kwargs)
            else:
                result = func(**kwargs)
            
            return {
                "success": True,
                "error": None,
                "result": result
            }
        except Exception as e:
            logger.error(f"[TOOL] Error executing {tool_name}: {e}")
            return {
                "success": False,
                "error": str(e),
                "result": None
            }


def register_tool(name: str, description: str):
    """Decorator to register a tool."""
    def decorator(func):
        executor = ToolExecutor()
        executor.register(name, description, func)
        return func
    return decorator


# === Built-in Tools ===

@register_tool("exec", "Execute a shell command (only whitelisted commands allowed)")
async def exec_command(command: str, timeout: int = 30, **kwargs) -> Dict[str, Any]:
    """Execute a shell command with security restrictions."""
    
    # Parse command
    cmd_parts = command.strip().split()
    if not cmd_parts:
        return {"error": "Empty command", "output": ""}
    
    cmd_name = cmd_parts[0].lower()
    
    # Security: Check command whitelist
    if cmd_name not in ALLOWED_COMMANDS:
        return {"error": f"Command '{cmd_name}' is not allowed", "output": ""}
    
    try:
        # Execute command
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "command": command
        }
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out", "output": ""}
    except Exception as e:
        return {"error": str(e), "output": ""}


@register_tool("read_file", "Read content from a file")
async def read_file(path: str) -> Dict[str, Any]:
    """Read file content with path validation."""
    
    # Security: Normalize and validate path
    try:
        normalized_path = os.path.normpath(os.path.expanduser(path))
        
        # Check if path is within allowed directories
        if not _is_path_allowed(path):
            return {"error": f"Access denied: Path '{path}' is not in allowed directories", "content": ""}
        
        # Check file exists
        if not os.path.exists(normalized_path):
            return {"error": f"File not found: {path}", "content": ""}
        
        if not os.path.isfile(normalized_path):
            return {"error": f"Not a file: {path}", "content": ""}
        
        # Read file
        with open(normalized_path, 'r', encoding='utf-8') as f:
            content = f.read(100000)  # Limit to 100KB
        
        return {"content": content, "path": normalized_path}
    
    except Exception as e:
        return {"error": str(e), "content": ""}


@register_tool("list_dir", "List directory contents")
async def list_dir(path: str = ".") -> Dict[str, Any]:
    """List directory contents with path validation."""
    
    try:
        normalized_path = os.path.normpath(os.path.expanduser(path))
        
        # Check if path is within allowed directories
        if not _is_path_allowed(path):
            return {"error": f"Access denied: Path '{path}' is not in allowed directories", "files": []}
        
        if not os.path.exists(normalized_path):
            return {"error": f"Directory not found: {path}", "files": []}
        
        if not os.path.isdir(normalized_path):
            return {"error": f"Not a directory: {path}", "files": []}
        
        # List contents
        entries = []
        for entry in os.listdir(normalized_path):
            entry_path = os.path.join(normalized_path, entry)
            is_dir = os.path.isdir(entry_path)
            entries.append({
                "name": entry,
                "type": "directory" if is_dir else "file",
                "size": os.path.getsize(entry_path) if not is_dir else None
            })
        
        return {"files": entries, "path": normalized_path}
    
    except Exception as e:
        return {"error": str(e), "files": []}


@register_tool("write_file", "Write content to a file")
async def write_file(path: str, content: str, append: bool = False) -> Dict[str, Any]:
    """Write content to a file with path validation."""
    
    try:
        normalized_path = os.path.normpath(os.path.expanduser(path))
        
        # Check if path is within allowed directories
        if not _is_path_allowed(path):
            return {"error": f"Access denied: Path '{path}' is not in allowed directories", "success": False}
        
        # Ensure parent directory exists
        parent_dir = os.path.dirname(normalized_path)
        if parent_dir and not os.path.exists(parent_dir):
            return {"error": f"Parent directory does not exist: {parent_dir}", "success": False}
        
        # Write file
        mode = 'a' if append else 'w'
        with open(normalized_path, mode, encoding='utf-8') as f:
            f.write(content)
        
        return {"success": True, "path": normalized_path, "bytes_written": len(content)}
    
    except Exception as e:
        return {"error": str(e), "success": False}


@register_tool("edit", "Edit a file (write content to path)")
async def edit_file(path: str, content: str) -> Dict[str, Any]:
    """Alias for write_file to support 'edit' tool calls."""
    return await write_file(path, content)


@register_tool("create_dir", "Create a directory")
async def create_dir(path: str) -> Dict[str, Any]:
    """Create a directory with path validation."""
    
    try:
        normalized_path = os.path.normpath(os.path.expanduser(path))
        
        # Check if path is within allowed directories
        if not _is_path_allowed(path):
            return {"error": f"Access denied: Path '{path}' is not in allowed directories", "success": False}
        
        # Create directory
        os.makedirs(normalized_path, exist_ok=True)
        
        return {"success": True, "path": normalized_path}
    
    except Exception as e:
        return {"error": str(e), "success": False}


def format_tool_result(tool_call_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Format tool execution result as OpenAI tool message format."""
    return {
        "tool_call_id": tool_call_id,
        "role": "tool",
        "content": json.dumps(result, ensure_ascii=False)
    }


def build_tool_calls_response(tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build OpenAI-compatible tool_calls response from parsed XML."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(asyncio.get_event_loop().time()),
        "model": "tool-executor",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": tool_calls
            },
            "finish_reason": "tool_calls"
        }]
    }
